"""WhatsApp Cloud API (Meta) webhook: routes each sender's phone number to their own broker account.

This is what makes the system multi-broker in production: identity = the WhatsApp number that wrote in.
Parsing and routing are unit-tested offline. Media download and sending need a real Meta app
(WHATSAPP_TOKEN + WHATSAPP_PHONE_ID) and have NOT been exercised against Meta's servers yet.
"""
import os
import re
from typing import Optional

from . import db, service

GRAPH = "https://graph.facebook.com/v21.0"


def parse_webhook(payload: dict) -> list[dict]:
    """Meta payload -> [{"from", "id", "type", "text", "media_id", "mime", "filename"}] (status updates are skipped)."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for m in (change.get("value") or {}).get("messages", []) or []:
                t = m.get("type")
                item = {"from": m.get("from"), "id": m.get("id"), "type": t, "text": "", "media_id": None,
                        "mime": None, "filename": None}
                if t == "text":
                    item["text"] = (m.get("text") or {}).get("body", "")
                elif t in ("image", "audio", "document", "video"):
                    blob = m.get(t) or {}
                    item.update(media_id=blob.get("id"), mime=blob.get("mime_type"), text=blob.get("caption", ""),
                                filename=blob.get("filename"))
                out.append(item)
    return out


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ.get('WHATSAPP_TOKEN', '')}"}


def download_media(media_id: str) -> Optional[bytes]:
    import httpx
    if not os.environ.get("WHATSAPP_TOKEN"):
        return None
    meta = httpx.get(f"{GRAPH}/{media_id}", headers=_headers(), timeout=30).json()
    return httpx.get(meta["url"], headers=_headers(), timeout=60).content if meta.get("url") else None


def send_text(to: str, text: str) -> bool:
    """Send a free-form reply (valid inside the 24h window). Returns False if not configured."""
    import httpx
    if not (os.environ.get("WHATSAPP_TOKEN") and os.environ.get("WHATSAPP_PHONE_ID")):
        print(f"[whatsapp stub] -> {to}: {ascii(text[:80])}")  # ascii(): never crash a console on emoji
        return False
    r = httpx.post(f"{GRAPH}/{os.environ['WHATSAPP_PHONE_ID']}/messages", headers=_headers(), timeout=30,
                   json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4000]}})
    return r.status_code < 300


class SendError(Exception):
    """Raised with a broker-readable reason when a message can't be sent."""


def normalize_phone(raw: str) -> Optional[str]:
    """'+91 98100-11111' / '9810011111' -> '919810011111' (E.164 digits, India assumed for 10 digits)."""
    d = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(d) == 10 and d[0] in "6789":
        return "91" + d
    if 11 <= len(d) <= 15 and not d.startswith("0"):
        return d
    return None


def api_configured() -> bool:
    return bool(os.environ.get("WHATSAPP_TOKEN") and os.environ.get("WHATSAPP_PHONE_ID"))


def window_open(phone: str) -> bool:
    from datetime import datetime, timedelta, timezone
    ts = db.last_inbound(phone)
    return bool(ts and datetime.now(timezone.utc) - datetime.fromisoformat(ts) < timedelta(hours=24))


def _template_param(text: str) -> str:
    # Meta rejects newlines, tabs and 4+ consecutive spaces inside a template parameter; cap at 1024 chars.
    t = " | ".join(p.strip() for p in text.splitlines() if p.strip())
    return re.sub(r" {2,}", " ", t)[:1024]


def send_to(phone: str, text: str, post=None) -> dict:
    """Send `text` to `phone` from the business number.

    Inside the 24h window (the recipient messaged us recently) -> free-form text.
    Outside it WhatsApp only allows an approved template, so we use WHATSAPP_SHARE_TEMPLATE with the text as its
    single body variable ({{1}}). Raises SendError with a human-readable reason on any failure."""
    import httpx
    post = post or httpx.post
    if not api_configured():
        raise SendError("The business WhatsApp number isn't connected yet. Use “Open in WhatsApp” instead, or add "
                        "WHATSAPP_TOKEN and WHATSAPP_PHONE_ID to .env.")
    if window_open(phone):
        body, mode = {"messaging_product": "whatsapp", "to": phone, "type": "text",
                      "text": {"body": text[:4000], "preview_url": False}}, "text"
    else:
        name = os.environ.get("WHATSAPP_SHARE_TEMPLATE")
        if not name:
            raise SendError("This person hasn't messaged the business number in the last 24 hours, so WhatsApp only "
                            "allows an approved template. Set WHATSAPP_SHARE_TEMPLATE (a template with one {{1}} body "
                            "variable), or use “Open in WhatsApp”.")
        body, mode = {"messaging_product": "whatsapp", "to": phone, "type": "template", "template": {
            "name": name, "language": {"code": os.environ.get("WHATSAPP_SHARE_TEMPLATE_LANG", "en")},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": _template_param(text)}]}]}}, "template"
    r = post(f"{GRAPH}/{os.environ['WHATSAPP_PHONE_ID']}/messages", headers=_headers(), json=body, timeout=30)
    if r.status_code >= 300:
        try:
            err = r.json().get("error", {})
            msg = f'{err.get("message", "unknown error")} (code {err.get("code")})'
        except Exception:
            msg = f"HTTP {r.status_code}"
        raise SendError(f"WhatsApp refused the message: {msg}")
    wid = ((r.json().get("messages") or [{}])[0]).get("id")
    return {"mode": mode, "id": wid}


def handle_incoming(payload: dict, send=send_text, fetch=download_media) -> list[dict]:
    """Route each message to the broker that owns the sending number. Unknown numbers get a polite refusal
    and are never processed: no data is created for them."""
    results = []
    for m in parse_webhook(payload):
        db.touch_inbound(m["from"] or "")  # opens the 24h window for replies/shares to this number
        broker = db.broker_by_phone(m["from"] or "")
        if not broker:
            send(m["from"], "Sorry, this number isn't registered. Ask your brokerage owner to add you.")
            results.append({"from": m["from"], "routed_to": None})
            continue
        if m["id"] and db.seen_before(broker["id"], "wamid:" + m["id"]):
            results.append({"from": m["from"], "routed_to": broker["id"], "duplicate": True})
            continue  # Meta retries on any 5xx/timeout: processing a message twice would duplicate listings
        files = []
        if m["media_id"]:
            data = fetch(m["media_id"])
            if data:
                files.append((m["filename"] or f'{m["type"]}-{m["id"]}', m["mime"] or "", data))
        res = service.process_message(broker["id"], m["text"], files)
        for r in res["replies"]:
            try:
                send(m["from"], r["text"])
            except Exception as e:  # a delivery failure must not fail the webhook (Meta would retry)
                print("whatsapp send failed:", ascii(str(e)))
        results.append({"from": m["from"], "routed_to": broker["id"], "replies": len(res["replies"])})
    return results
