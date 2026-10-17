"""WhatsApp group message handling.

Official WhatsApp Business API numbers cannot be added to ordinary groups, so there are two routes in:
  1. Phase-1 route (safe, supported): the broker FORWARDS a group message to the bot. Forwarding is
     intentional, so it goes through normal intake with no filtering.
  2. Relay route (optional, ban risk): a separate listener number/device reads groups and POSTs each message
     to `/api/group/ingest`. Because it is unsolicited, everything here is filtered and only genuine
     requirements/listings are surfaced as a short alert. Everything else is ignored.

Privacy: ignored messages are never stored (only a one-way fingerprint for de-duplication and a counter).
"""
import hashlib
import re
from datetime import datetime, timezone
from typing import Optional

from . import db, engine, matching as M, normalize as N, scheduler

GROUP_PROMPT = (
    "GROUP_MODE: this message was posted by '{sender}' in the WhatsApp group '{group}'. It is NOT addressed to you. "
    "Emit ONLY save_listing / save_requirement actions, and only for a GENUINE listing (a specific unit someone is "
    "offering) or a GENUINE client requirement (someone is looking for a specific kind of property). Return "
    '{{"actions": []}} for greetings, festival wishes, jokes, news, promotions/ads (loans, trading, courses), '
    "'sold'/'thanks'/'ok' replies, general questions, market chatter and anything too vague to act on. "
    "Set listing.source to the group name."
)


def _fingerprint(text: str) -> str:
    return hashlib.sha1(re.sub(r"[^a-z0-9]+", "", text.lower()).encode()).hexdigest()


_PROPERTY_WORDS = re.compile(r"bhk|br|gaj|gz|yard|plot|floor|flat|apartment|villa|kothi|builder|cr|crore|lakh|"
                             r"lac|rent|lease|sale|requirement|budget|sector|phase|available|chahiye|"
                             r"inventory|sq ?ft|society|tower", re.I)


def _obviously_chatter(text: str) -> bool:
    """Free pre-filter so greetings/jokes/news never cost an LLM call: a listing or requirement always
    has a number or a property word. Everything ambiguous still goes to the model."""
    return not re.search(r"\d", text) and not _PROPERTY_WORDS.search(text)


def ingest(b: str, group: str, sender: str, text: str, images: Optional[list] = None,
           now: Optional[datetime] = None) -> dict:
    """Process one group message. Returns {"outcome": ..., "alert": str|None}."""
    now = now or datetime.now(timezone.utc)
    g = db.get_group(b, group)
    if g and g["muted"]:
        db.group_touch(b, group, "muted")
        return {"outcome": "muted", "alert": None}
    if _obviously_chatter(text):
        db.group_touch(b, group, "ignored")
        return {"outcome": "ignored", "alert": None}
    if db.seen_before(b, _fingerprint(text)):
        db.group_touch(b, group, "duplicate")
        return {"outcome": "duplicate", "alert": None}

    sess = db.get_session(b)
    # No inventory context here: group posts are never edits, and numbers in the context can leak into extraction.
    ctx = GROUP_PROMPT.format(sender=sender, group=group)
    result = engine._understand(text, images or [], ctx)
    acts = [a for a in result.get("actions", []) if a.get("intent") in ("save_listing", "save_requirement")]
    if not acts:
        db.group_touch(b, group, "ignored")
        return {"outcome": "ignored", "alert": None}

    lines: list[str] = []
    first: Optional[dict] = None
    for a in acts:
        if a["intent"] == "save_requirement":
            line, ref = _save_requirement(b, group, sender, a.get("requirement") or {})
        else:
            line, ref = _save_listing(b, group, a.get("listing") or {}, now, text, bool(images))
        if line:
            lines.append(line)
            first = first or ref
    if not lines:  # everything was a duplicate of known inventory
        db.group_touch(b, group, "duplicate")
        return {"outcome": "duplicate", "alert": None}
    db.group_touch(b, group, "surfaced")
    db.set_session(b, sess)
    alert = "\n".join(lines)
    scheduler.send(b, alert, "group_alert", now,
                   awaiting={"kind": "alert", "entity": first["entity"], "id": first["id"]} if first else None)
    return {"outcome": "surfaced", "alert": alert}


def _save_requirement(b: str, group: str, sender: str, raw: dict) -> tuple[str, dict]:
    req = N.normalize_requirement(raw)
    req["client_name"] = req.get("client_name") or f"{sender} ({group})"
    req["source"] = f"group: {group}"
    rid = db.add_requirement(b, req)
    db.log(b, "requirement", rid, "created", f"from group {group}: {M.req_label(req)}")
    n = len(M.find_matches(req, db.visible_listings(b), 5))
    tail = f"You have {n} match{'es' if n != 1 else ''}. View?" if n else "No matches yet — I'll alert you when something fits."
    return f"New requirement in {group}: {M.req_label(req)}. {tail}", {"entity": "requirement", "id": rid}


def _save_listing(b: str, group: str, raw: dict, now: datetime, text: str = "",
                  has_images: bool = False) -> tuple[Optional[str], Optional[dict]]:
    if text and not has_images:
        N.repair_price(raw, text)
    ch, low, ver = N.normalize_listing(raw)
    if text and not has_images:
        low = sorted(set(low) | {f for f in N.ungrounded(raw, text) if f in ch})
    if not ch.get("type"):
        return None, None
    ch.setdefault("purpose", "sale")
    ch["source"] = f"group: {group}"
    dup = engine._find_dup(b, ch)
    if dup and engine._same_price(ch, dup) is not False:
        srcs = set(dup.get("sources", []))
        srcs.add(ch["source"])
        dup["sources"] = sorted(srcs)
        dup["last_confirmed"] = db.now()  # a fresh sighting counts as re-confirmation
        for k, v in ch.items():
            if dup.get(k) is None and k not in ("verified",):
                dup[k] = v
        db.save_listing(b, dup)
        db.log(b, "listing", dup["id"], "duplicate_merged", f"seen again in group {group}")
        return None, None
    ch.update(verified={f: True for f in ver}, low_confidence=low, asked=[], sources=[ch["source"]],
              status="available", last_confirmed=db.now())
    lid = db.add_listing(b, ch)
    db.log(b, "listing", lid, "created", f"from group {group}")
    l = db.get_listing(b, lid)
    line = f"New listing in {group}: {engine._summary(l)}."
    if low:
        line += " Check the price before relying on it."
    for r in db.list_requirements(b, only_open=True):
        m = M.match_one(r, l)
        if m:
            line += f'\n   Fits {r.get("client_name") or "requirement #" + str(r["id"])} ({", ".join(m["matches"][:3])}).'
    return line, {"entity": "listing", "id": lid}
