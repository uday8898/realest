"""Phase 3 actions: activity logging, end-of-day report, notes recall, follow-ups, reminder settings."""
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import db, llm, matching as M, normalize as N, scheduler
from .engine import R, Reply

# Injectable for tests (plain-text LLM) and embeddings
text_llm = lambda system, user, max_tokens=500: llm.chat_text(system, user, max_tokens)
embed_fn = lambda texts: llm.embed(texts)

STOP = set("what did the a an of to is was for and or with about me my i on in at he she they it that this "
           "have has had do does say said told discuss discussed last week owner client mr mrs ji ka ki ke se ne hai".split())


# ---------------------------------------------------------------- notes
def store_note(b: str, kind: str, text: str, listing_id=None, requirement_id=None, client_name=None) -> int:
    vec = None
    try:
        out = embed_fn([text])
        vec = out[0] if out else None
    except Exception:
        pass
    return db.add_note(b, kind, text, listing_id, requirement_id, client_name, vec)


def _resolve_req(b: str, rid, client) -> tuple[Optional[int], Optional[str]]:
    reqs = db.list_requirements(b)
    if rid and any(r["id"] == int(rid) for r in reqs):
        r = next(r for r in reqs if r["id"] == int(rid))
        return r["id"], r.get("client_name") or client
    if client:
        c = client.lower().replace("mr.", "").replace("mr ", "").replace("mrs.", "").replace("ji", "").strip()
        for r in reqs:
            if r.get("client_name") and c and c in r["client_name"].lower():
                return r["id"], r["client_name"]
    return None, client


def _label_of(b: str, lid) -> str:
    l = db.get_listing(b, int(lid)) if lid else None
    return f'#{l["id"]} {M.label(l)}' if l else ""


# ---------------------------------------------------------------- activity log (3-hour replies, ad-hoc visit notes)
def act_activity_log(b: str, a: dict, sess: dict) -> list[Reply]:
    entries = a.get("entries") or []
    eod = bool(a.get("eod"))
    out: list[Reply] = []
    if not entries:
        if a.get("skipped"):
            return [R("No problem — skipped. I'll check in again later.")]
        if not eod:
            return [R("Nothing to log. I'll check in again later")]
    lines, followups = [], []
    for e in entries:
        summary = (e.get("summary") or "").strip()
        if not summary:
            continue
        lid = e.get("listing_id")
        if lid and not db.get_listing(b, int(lid)):
            lid = None
        rid, client = _resolve_req(b, e.get("requirement_id"), e.get("client_name"))
        kind = e.get("kind") if e.get("kind") in ("visit", "call", "discussion", "note") else "note"
        store_note(b, "eod" if eod else kind, summary, lid, rid, client)
        if lid:
            db.log(b, "listing", int(lid), kind, summary)
        if rid:
            db.log(b, "requirement", rid, kind, summary)
        links = [x for x in (_label_of(b, lid), f"{client}" if client else "") if x]
        lines.append(f"• {summary}" + (f"\n   ↳ linked to {' + '.join(links)}" if links else ""))
        for f in e.get("follow_ups") or []:
            if f.get("text"):
                tid = db.add_task(b, f["text"], f.get("due"), int(lid) if lid else None, rid)
                followups.append(f'#{tid} {f["text"]}' + (f' — due {f["due"]}' if f.get("due") else ""))
    if lines:
        text = f'Logged {len(lines)} item{"s" if len(lines) > 1 else ""}:\n' + "\n".join(lines)
        if followups:
            text += "\n\nFollow-ups I'll remind you about:\n" + "\n".join(followups)
        out.append(R(text))
    if eod:
        out.append(R(build_eod(b)))
    return out


# ---------------------------------------------------------------- follow-up outcome
def act_task_done(b: str, a: dict, sess: dict) -> list[Reply]:
    t = db.get_task(b, int(a.get("target_id") or 0))
    if not t:
        return [R("Which follow-up is that for? Say its number, e.g. “#3 done”.")]
    outcome = (a.get("outcome") or "").strip()
    db.update_task(b, t["id"], done=1, outcome=outcome or None)
    if outcome:
        store_note(b, "outcome", f'Follow-up "{t["text"]}": {outcome}', t.get("listing_id"), t.get("requirement_id"))
    out = [R(f'Done: {t["text"]}.' + (f' Noted: {outcome}' if outcome else ""))]
    for f in a.get("follow_ups") or []:
        if f.get("text"):
            tid = db.add_task(b, f["text"], f.get("due"), t.get("listing_id"), t.get("requirement_id"))
            out.append(R(f'Next follow-up #{tid}: {f["text"]}' + (f' — due {f["due"]}' if f.get("due") else "")))
    return out


# ---------------------------------------------------------------- reminder settings
def act_settings(b: str, a: dict, sess: dict) -> list[Reply]:
    ch, notes = a.get("changes") or {}, []
    upd = {}
    now = datetime.now(timezone.utc)
    if ch.get("interval_hours"):
        h = max(0.5, min(float(ch["interval_hours"]), 12))
        upd["interval_hours"] = h
        notes.append(f"check-ins every {int(h) if h.is_integer() else h} hours")
    for k, label in (("business_start", "business hours start"), ("business_end", "business hours end"),
                     ("eod_time", "end-of-day prompt")):
        if re.fullmatch(r"\d{1,2}:\d{2}", str(ch.get(k) or "")):
            upd[k] = ch[k].zfill(5)
            notes.append(f"{label} {upd[k]}")
    if re.fullmatch(r"\d{1,2}:\d{2}", str(ch.get("snooze_until") or "")):
        until = scheduler.at(scheduler.ist(now), ch["snooze_until"])
        if until <= scheduler.ist(now):
            until += timedelta(days=1)
        upd["snooze_until"] = until.astimezone(timezone.utc).isoformat(timespec="seconds")
        notes.append(f"quiet until {scheduler._t12(until)}")
    if ch.get("reminders") in ("on", "off"):
        upd["enabled"] = ch["reminders"] == "on"
        notes.append("reminders " + ch["reminders"])
    if not upd:
        return [R("What should I change? e.g. “every 2 hours”, “don't ping me till 4 pm”, “EOD at 7:30”.")]
    db.update_settings(b, **upd)
    return [R("Done — " + ", ".join(notes) + ".")]


# ---------------------------------------------------------------- end-of-day report
def _day_bounds(now: Optional[datetime] = None) -> tuple[str, str]:
    local = scheduler.ist(now)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat(), (start + timedelta(days=1)).astimezone(timezone.utc).isoformat()


def eod_facts(b: str, now: Optional[datetime] = None) -> dict:
    lo, hi = _day_bounds(now)
    today = lambda ts: bool(ts) and lo <= ts < hi
    listings = db.list_listings(b)
    notes = [n for n in db.list_notes(b) if today(n["ts"])]
    hist = [h for h in db.history(b, 400) if today(h["ts"])]
    reqs = db.list_requirements(b, only_open=True)
    tasks = db.list_tasks(b, only_open=True)
    today_s = scheduler.ist(now).date().isoformat()
    live = [l for l in listings if l["status"] in ("available", "under_negotiation", "stale")]
    return {
        "new_inventory": [M.label(l) for l in listings if today(l.get("created_at"))],
        "visits": [n["text"] for n in notes if n["kind"] == "visit"],
        "calls_discussions": [n["text"] for n in notes if n["kind"] in ("call", "discussion")],
        "other_notes": [n["text"] for n in notes if n["kind"] in ("note", "eod", "outcome")],
        "progress": [f'#{h["entity_id"]} {h["event"].replace("_", " ")}: {h["detail"][:60]}' for h in hist
                    if h["event"] in ("status_changed", "price_changed")],
        "open_followups": [f'#{t["id"]} {t["text"]}' + (f' (due {t["due"]})' if t["due"] else "") +
                           (" — OVERDUE" if t["due"] and t["due"][:10] < today_s else "") for t in tasks],
        "requirements_with_matches": [f'{r.get("client_name") or "#" + str(r["id"])}: {M.req_label(r)} '
                                      f'→ {len(M.find_matches(r, listings, 5))} match(es)' for r in reqs],
        "stale_count": len([l for l in live if (M.days_since(l.get("last_confirmed")) or 0) > 30]),
    }


def build_eod(b: str, now: Optional[datetime] = None) -> str:
    f = eod_facts(b, now)
    sec = lambda title, items: f"*{title}* ({len(items)})\n" + "\n".join(f"• {i}" for i in items[:6]) if items else \
        f"*{title}*: none"
    body = "\n\n".join([
        sec("Deals progressed", f["progress"]), sec("Visits done", f["visits"]),
        sec("Calls & discussions", f["calls_discussions"]), sec("Other notes from today", f["other_notes"]),
        sec("New inventory added", f["new_inventory"]),
        sec("Open follow-ups", f["open_followups"])])
    pri = _priorities(f)
    return f"*Today's summary*\n\n{body}\n\n*Suggested priorities for tomorrow*\n{pri}"


def _priorities(f: dict) -> str:
    try:
        txt = text_llm(
            "You are a real-estate broker's assistant. From the facts, write 3 to 4 short, concrete priorities for "
            "tomorrow as '1. ...' lines. Overdue follow-ups first, then clients with matching inventory, then stale "
            "listings to re-confirm. No preamble.", str(f), 250)
        if txt:
            return txt
    except Exception:
        pass
    items = [f"Follow up: {x}" for x in f["open_followups"][:2]] + \
            [f"Share matches with {x}" for x in f["requirements_with_matches"][:2]]
    if f["stale_count"]:
        items.append(f'Re-confirm {f["stale_count"]} stale listing(s)')
    return "\n".join(f"{i}. {x}" for i, x in enumerate(items or ["Nothing pending — keep prospecting"], 1))


def act_eod_report(b: str, a: dict, sess: dict) -> list[Reply]:
    return [R(build_eod(b))]


# ---------------------------------------------------------------- recall
def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2 and w not in STOP}


def _cos(a: list, b: list) -> float:
    d = sum(x * y for x, y in zip(a, b))
    return d / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1)


def recall_candidates(b: str, a: dict) -> list[dict]:
    notes = db.list_notes(b)
    for l in db.list_listings(b):  # soft data stored on a listing is also recallable
        if l.get("notes"):
            notes.append({"id": f'L{l["id"]}', "kind": "listing_note", "text": l["notes"], "listing_id": l["id"],
                          "requirement_id": None, "client_name": None, "embedding": None, "ts": l["last_confirmed"]})
    lid, client = a.get("listing_id"), (a.get("client_name") or "").lower().replace("mr.", "").replace("mrs.", "").strip()
    since, until = a.get("since"), a.get("until")
    if since:
        notes = [n for n in notes if n["ts"][:10] >= since]
    if until:
        notes = [n for n in notes if n["ts"][:10] <= until]
    scoped = [n for n in notes if (lid and n["listing_id"] == int(lid)) or
              (client and (client in (n["client_name"] or "").lower() or client in n["text"].lower()))]
    pool = scoped or notes  # entity filter if it matches anything, else search everything
    q = a.get("question") or ""
    qtok, qvec = _tokens(q), None
    try:
        v = embed_fn([q])
        qvec = v[0] if v else None
    except Exception:
        pass
    scored = []
    for n in pool:
        lex = len(qtok & _tokens(n["text"])) / (len(qtok) or 1)
        sem = _cos(qvec, n["embedding"]) if qvec and n.get("embedding") else 0.0
        scored.append((0.6 * sem + 0.4 * lex if qvec else lex, n))
    scored.sort(key=lambda x: (-x[0], x[1]["ts"]), reverse=False)
    return [n for s, n in scored[:12]]


def act_recall(b: str, a: dict, sess: dict) -> list[Reply]:
    cands = recall_candidates(b, a)
    if not cands:
        return [R("I don't have any notes about that yet.")]
    ctx = "\n".join(
        f'[{n["ts"][:10]}] ({n["kind"]}{", " + _label_of(b, n["listing_id"]) if n["listing_id"] else ""}'
        f'{", client " + n["client_name"] if n["client_name"] else ""}) {n["text"]}' for n in cands)
    try:
        ans = text_llm(
            "You answer a Gurgaon real-estate broker's question using ONLY the notes provided. Be brief (2-4 lines). "
            "Answer ONLY about the specific property, owner or client asked about; ignore notes about anything else. "
            "Mention the date of what you cite. If the notes don't answer it, say you have no note about that — "
            "never invent details.", f"NOTES:\n{ctx}\n\nQUESTION: {a.get('question')}", 300)
    except Exception as e:
        ans = f"(AI unavailable: {type(e).__name__}) Closest notes:\n" + "\n".join(f"• {n['ts'][:10]}: {n['text']}" for n in cands[:3])
    return [R(f"{ans}\n\n_Based on {min(len(cands), 5)} stored note(s)._")]
