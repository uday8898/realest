"""Conversation engine: LLM understands the message, deterministic code applies it and writes the reply.

Replies are composed in code (not by the LLM) so numbers shown to the broker always come from the
normalised database values.
"""
from datetime import datetime, timezone
from typing import Callable, Optional

from . import db, fastpath, llm, matching as M, normalize as N, vocab

# Injectable for tests / evals.
Understander = Callable[[str, list, str], dict]
_understand: Understander = lambda text, images, ctx: llm.understand(text, images, ctx)


def set_understander(fn: Understander) -> None:
    global _understand
    _understand = fn


Reply = dict  # {"text": str, "images": [url]}


def R(text: str, images: Optional[list] = None) -> Reply:
    return {"text": text, "images": images or []}


# ---------------------------------------------------------------- context for the LLM
def _line(l: dict) -> str:
    return (f'#{l["id"]} {l["type"]} | {l.get("micro_market") or "?"} | '
            f'{l.get("size_gaj") or l.get("bedrooms") or "?"} | {N.fmt_money(l.get("price_total"), l.get("purpose") == "rent")}'
            f' | {l["status"]} | {l.get("society") or l.get("owner_name") or ""}')


def build_context(broker_id: str, sess: dict) -> str:
    ls = db.list_listings(broker_id)[:50]
    rs = db.list_requirements(broker_id, only_open=True)[:20]
    out = ["LISTINGS (id | type | market | size/BHK | price | status | extra):"]
    out += [_line(l) for l in ls] or ["(none)"]
    out.append("OPEN REQUIREMENTS:")
    out += [f'#{r["id"]} {r.get("client_name") or "client"}: {M.req_label(r)}' for r in rs] or ["(none)"]
    out.append("OPEN FOLLOW-UP TASKS:")
    out += [f'#{t["id"]} {t["text"]} (due {t["due"]})' for t in db.list_tasks(broker_id, only_open=True)[:15]] or ["(none)"]
    aw = db.get_settings(broker_id).get("awaiting")
    if aw:
        hint = {"activity": "the 3-hour check-in 'What happened in the last 3 hours?' -> IF the message reports what they did, answer with ONE activity_log action (eod=false) — not note/task; 'skip' => skipped=true; if the message is about something else (a search, a new listing...), IGNORE this and handle it normally",
                "eod": "the end-of-day prompt -> you MUST answer with ONE activity_log action with eod=true",
                "alert": "a group alert ending in 'View?' -> if the broker says yes/view/show/dikhao emit {\"intent\":\"view_alert\"}; otherwise treat the message normally",
                "task": f'a reminder for follow-up task #{aw.get("id")} -> IF the message reports how that follow-up went, emit task_done (target_id {aw.get("id")}); if it is about something else, IGNORE this and handle the message normally'}[aw["kind"]]
        out.append(f"AWAITING: the bot just asked the broker for {hint}")
    out.append(f'LAST_SAVED listing id: {sess.get("last_listing_id")}')
    if sess.get("last_search"):
        s = sess["last_search"]
        out.append(f'LAST_SEARCH_CRITERIA: {s["criteria"]}')
        out.append("LAST_SEARCH_RESULTS (index -> listing id): " +
                   ", ".join(f"{i + 1}->#{lid}" for i, lid in enumerate(s["ids"])))
    if sess.get("pending_confirm"):
        out.append(f'PENDING_CONFIRM (bot asked broker to confirm values on listings): {list(sess["pending_confirm"])}')
    return "\n".join(out)


# ---------------------------------------------------------------- entry point
_current = {"text": "", "check": False}  # the message being processed (for number grounding)


def handle(broker_id: str, text: str, images: Optional[list] = None) -> list[Reply]:
    sess = db.get_session(broker_id)
    # numbers read from an image/PDF-less message can be checked against the text; image content cannot
    _current.update(text=text or "", check=not images)
    fast = None if images else fastpath.route(text, sess, db.get_settings(broker_id))
    result = {"actions": fast} if fast is not None else _understand(text, images or [], build_context(broker_id, sess))
    replies: list[Reply] = []
    for a in result.get("actions", []):
        try:
            replies += run_action(broker_id, a, sess, has_image=bool(images))
        except Exception as e:  # never crash the chat on one bad action
            replies.append(R(f"I couldn't process one part of that ({type(e).__name__}: {e}). Please resend it."))
    sess["_meta"] = {"fast": fast is not None, "suggestions": suggest(broker_id, [a.get("intent") for a in result.get("actions", [])], sess)}
    db.set_session(broker_id, sess)
    if any(a.get("intent") in ("activity_log", "task_done", "eod_report", "view_alert") for a in result.get("actions", [])):
        db.update_settings(broker_id, awaiting=None)
    return replies or [R("I didn't catch that. Forward a listing, a client requirement, or ask me to find something.")]


def run_action(b: str, a: dict, sess: dict, has_image: bool = False) -> list[Reply]:
    from . import phase3 as P3
    fn = {
        "activity_log": P3.act_activity_log, "task_done": P3.act_task_done, "settings": P3.act_settings,
        "eod_report": P3.act_eod_report, "recall": P3.act_recall,
        "save_listing": act_save_listing, "save_requirement": act_save_requirement,
        "update_listing": act_update_listing, "update_requirement": act_update_requirement,
        "set_status": act_set_status, "close_requirement": act_close_requirement,
        "search": act_search, "show": act_show, "attach_photo": act_attach_photo,
        "share": act_share, "view_alert": act_view_alert, "list_tasks": act_list_tasks, "note": act_note, "task": act_task, "confirm": act_confirm, "stale_check": act_stale,
        "chat": lambda b, a, s: [R(a.get("reply") or "How can I help?")],
    }.get(a.get("intent"))
    if not fn:
        return [R("I'm not sure what you want me to do with that. Try forwarding a listing or asking me to find something.")]
    return fn(b, a, sess)


# ---------------------------------------------------------------- helpers
def _missing(l: dict) -> list[str]:
    need = vocab.REQUIRED_FIELDS.get(l.get("type"), [])
    out = [f for f in need if l.get(f) is None]
    if l.get("type") == "apartment" and l.get("society"):
        out = [f for f in out if f != "micro_market"] if l.get("micro_market") else out
    return out


def _ask_text(l: dict) -> str:
    """Ask only about missing fields we haven't already asked about; cap at 3."""
    asked = set(l.get("asked", []))
    ask = [f for f in _missing(l) if f not in asked][:3]
    l["asked"] = sorted(asked | set(ask))
    if not ask:
        return ""
    return "Missing: " + ", ".join(vocab.FIELD_LABELS.get(f, f) for f in ask) + ". Send those when you can."


def _price_str(l: dict) -> str:
    rent = l.get("purpose") == "rent"
    if l.get("price_total") is None:
        return "price not given"
    s = f'asking {N.fmt_money(l["price_total"], rent)}'
    if l.get("type") == "plot" and l.get("price_per_gaj") and not rent:
        s = f'asking {N.fmt_per_gaj(l["price_per_gaj"])} ({N.fmt_money(l["price_total"])} total)'
    return s


def _summary(l: dict) -> str:
    s = f'{M.label(l)}, {_price_str(l)}'
    if l.get("road_width_ft"):
        s += f', {int(l["road_width_ft"])} ft road'
    if l.get("price_flexibility"):
        s += f', owner {l["price_flexibility"]}'
    return s


def _flag_text(l: dict) -> str:
    low = l.get("low_confidence") or []
    if not low:
        return ""
    names = []
    for f in low:
        if f == "price_total":
            names.append(f'price → {_price_str(l).replace("asking ", "")}')
        else:
            names.append(f'{vocab.FIELD_LABELS.get(f, f)} → {l.get(f)}')
    return "Not sure about: " + "; ".join(names) + ". Reply *ok* to confirm or correct me."


def _find_dup(b: str, new: dict) -> Optional[dict]:
    for l in db.list_listings(b):
        if l["type"] != new.get("type") or l["purpose"] != new.get("purpose") or l["status"] in ("sold", "rented"):
            continue
        if not N.market_match(new.get("micro_market") or "", l.get("micro_market")) or \
                not N.market_match(l.get("micro_market") or "", new.get("micro_market")):
            continue
        a, c = new.get("size_gaj"), l.get("size_gaj")
        if a and c and abs(a - c) / max(a, c) > 0.02:
            continue
        if new.get("floor_number") is not None and l.get("floor_number") is not None \
                and new["floor_number"] != l["floor_number"]:
            continue
        if new.get("society") and l.get("society") and new["society"].lower() != l["society"].lower():
            continue
        if new.get("tower_unit") and l.get("tower_unit") and new["tower_unit"].lower() != l["tower_unit"].lower():
            continue
        if new.get("type") != "plot" and new.get("bedrooms") and l.get("bedrooms") and new["bedrooms"] != l["bedrooms"]:
            continue
        if (new.get("owner_contact") and l.get("owner_contact")) and new["owner_contact"] != l["owner_contact"]:
            continue
        if not (a and c) and new.get("type") == "plot":
            continue  # can't tell plots apart without size
        return l
    return None


def _same_price(a: dict, b: dict) -> Optional[bool]:
    p, q = a.get("price_total"), b.get("price_total")
    if not p or not q:
        return None
    return abs(p - q) / max(p, q) <= 0.05


def _ungrounded(raw: dict, changes: dict) -> set:
    if not _current["check"] or not _current["text"]:
        return set()
    return {f for f in N.ungrounded(raw, _current["text"]) if f in changes}


# ---------------------------------------------------------------- listing actions
def act_save_listing(b: str, a: dict, sess: dict) -> list[Reply]:
    if _current["check"]:
        N.repair_price(a.get("listing") or {}, _current["text"])
    ch, low, ver = N.normalize_listing(a.get("listing") or {})
    low = sorted(set(low) | _ungrounded(a.get("listing") or {}, ch))
    if not ch.get("type"):
        return [R("I couldn't tell if that's a plot, builder floor or apartment. Which one is it?")]
    ch.setdefault("purpose", "sale")
    ch["verified"] = {f: True for f in ver}
    dup = _find_dup(b, ch)
    replies: list[Reply] = []
    if dup and _same_price(ch, dup) is not False:
        old_price = dup.get("price_total")
        for k, v in ch.items():
            if k in ("verified",):
                dup["verified"] = {**dup.get("verified", {}), **v}
            elif k == "notes":
                if v not in (dup.get("notes") or ""):
                    dup["notes"] = f'{dup["notes"]}; {v}' if dup.get("notes") else v
            elif dup.get(k) is None:
                dup[k] = v
        srcs = set(dup.get("sources", [dup["source"]] if dup.get("source") else []))
        if ch.get("source"):
            srcs.add(ch["source"])
        dup["sources"] = sorted(srcs)
        dup["last_confirmed"] = db.now()
        if dup["status"] == "stale":
            dup["status"] = "available"
        db.save_listing(b, dup)
        db.log(b, "listing", dup["id"], "duplicate_merged", f"re-received from {ch.get('source') or 'unknown'}")
        sess["last_listing_id"] = dup["id"]
        msg = f'Already had this one (#{dup["id"]}): {_summary(dup)}. Merged and refreshed "last confirmed".'
        if old_price and ch.get("price_total") and old_price != ch["price_total"]:
            msg += f' (New message said {N.fmt_money(ch["price_total"])}, kept {N.fmt_money(old_price)}.)'
        return [R(msg)]
    note = ""
    if dup:
        note = (f'\nPossible duplicate of #{dup["id"]} ({N.fmt_money(dup.get("price_total"))}) '
                f'but the price differs. Saved separately — say "merge" or update the price if it is the same unit.')
    ch["low_confidence"] = low
    ch["asked"] = []
    if ch.get("source"):
        ch["sources"] = [ch["source"]]
    ch["status"], ch["last_confirmed"] = "available", db.now()
    lid = db.add_listing(b, ch)
    db.log(b, "listing", lid, "created", _summary(ch))
    sess["last_listing_id"] = lid
    if low:
        sess.setdefault("pending_confirm", {})[str(lid)] = low
    l = db.get_listing(b, lid)
    ask = _ask_text(l)
    db.save_listing(b, l)
    text = f"Saved #{lid}: {_summary(l)}."
    if ask:
        text += "\n" + ask
    if low:
        text += "\n" + _flag_text(l)
    replies.append(R(text + note))
    replies += _proactive_alerts(b, l)
    return replies


def _proactive_alerts(b: str, l: dict) -> list[Reply]:
    out = []
    for r in db.list_requirements(b, only_open=True):
        m = M.match_one(r, l)
        if m:
            who = r.get("client_name") or f'requirement #{r["id"]}'
            lacks = f' Lacks: {"; ".join(m["lacks"])}.' if m["lacks"] else ""
            out.append(R(f'New listing #{l["id"]} matches {who} ({M.req_label(r)}): '
                         f'{M.label(l)}, {N.fmt_money(l.get("price_total"), l.get("purpose") == "rent")}. '
                         f'Fits: {", ".join(m["matches"][:4])}.{lacks}'))
    return out


def act_update_listing(b: str, a: dict, sess: dict) -> list[Reply]:
    l = db.get_listing(b, int(a.get("target_id") or 0))
    if not l:
        return [R("I couldn't tell which listing you mean. Say its number, e.g. “change #12 price to 4.2 cr”.")]
    if _current["check"]:
        N.repair_price(a.get("updates") or {}, _current["text"])
    ch, low, ver = N.normalize_listing(a.get("updates") or {}, base=l)
    low = sorted(set(low) | _ungrounded(a.get("updates") or {}, ch))
    if not ch:
        return [R("I didn't find anything to change. What should I update?")]
    diffs = []
    for k, v in ch.items():
        if k in ("price_said", "notes") or l.get(k) == v:
            continue
        diffs.append((k, l.get(k), v))
        l[k] = v
    if "notes" in ch:
        l["notes"] = ch["notes"]
    for f in ver:
        l.setdefault("verified", {})[f] = True
    l["last_confirmed"] = db.now()
    if l["status"] == "stale":
        l["status"] = "available"
    l["low_confidence"] = [f for f in l.get("low_confidence", []) if f not in ch] + \
        [f for f in low if f not in l.get("low_confidence", [])]
    for k, old, new in diffs:
        event = "price_changed" if k == "price_total" else "updated"
        db.log(b, "listing", l["id"], event, f"{k}: {old} → {new}")
    if "notes" in ch:
        db.log(b, "listing", l["id"], "note", ch["notes"])
    ask = _ask_text(l)
    db.save_listing(b, l)
    sess["last_listing_id"] = l["id"]
    text = f'Updated #{l["id"]}: {_summary(l)}.'
    if ask:
        text += "\n" + ask
    if l["low_confidence"]:
        sess.setdefault("pending_confirm", {})[str(l["id"])] = l["low_confidence"]
        text += "\n" + _flag_text(l)
    out = [R(text)]
    if any(k in ("price_total", "facing", "park_view", "micro_market") for k, _, _ in diffs):
        out += _proactive_alerts(b, l)
    return out


def act_set_status(b: str, a: dict, sess: dict) -> list[Reply]:
    l = db.get_listing(b, int(a.get("target_id") or 0))
    st = N.norm_enum("status", a.get("status"))
    if not l or not st:
        return [R("Which listing, and what status? e.g. “#12 sold”.")]
    old = l["status"]
    l["status"] = st
    if st in ("available", "under_negotiation"):
        l["last_confirmed"] = db.now()
    db.save_listing(b, l)
    db.log(b, "listing", l["id"], "status_changed", f"{old} → {st}")
    return [R(f'#{l["id"]} ({M.label(l)}) is now *{st.replace("_", " ")}*.')]


def act_confirm(b: str, a: dict, sess: dict) -> list[Reply]:
    pend = sess.pop("pending_confirm", {})
    if not pend:
        return [R("Nothing waiting for confirmation")]
    for lid in pend:
        l = db.get_listing(b, int(lid))
        if l:
            l["low_confidence"] = []
            l["last_confirmed"] = db.now()
            db.save_listing(b, l)
            db.log(b, "listing", l["id"], "confirmed", "broker confirmed flagged values")
    return [R(f'Confirmed {len(pend)} listing(s).')]


def act_attach_photo(b: str, a: dict, sess: dict) -> list[Reply]:
    lid = a.get("target_id") or sess.get("last_listing_id")
    l = db.get_listing(b, int(lid)) if lid else None
    if not l:
        return [R("Got the photo, but which listing is it for? Say its number.")]
    sess["pending_photo_listing"] = l["id"]  # main.py stores the file against this listing
    return [R(f'Photo attached to #{l["id"]} ({M.label(l)}).')]


# ---------------------------------------------------------------- requirements & matching
def _fmt_results(matches: list[dict]) -> str:
    lines = []
    for i, m in enumerate(matches, 1):
        l = m["listing"]
        rent = l.get("purpose") == "rent"
        head = f'{i}) #{l["id"]} {M.label(l)} — {N.fmt_money(l.get("price_total"), rent)}'
        if l.get("price_flexibility"):
            head += f', owner {l["price_flexibility"]}'
        if l.get("shared_by"):
            head += f' team pool ({l["shared_by"]})'
        lines.append(head)
        fit = "Fits: " + ", ".join(m["matches"][:5]) if m["matches"] else ""
        lack = "Lacks: " + "; ".join(m["lacks"]) if m["lacks"] else ""
        lines.append("   " + "  ".join(x for x in (fit, lack) if x))
    return "\n".join(lines)


def act_save_requirement(b: str, a: dict, sess: dict) -> list[Reply]:
    req = N.normalize_requirement(a.get("requirement") or {})
    rid = db.add_requirement(b, req)
    db.log(b, "requirement", rid, "created", M.req_label(req))
    who = f'{req["client_name"]}: ' if req.get("client_name") else ""
    out = [R(f'Saved requirement #{rid} — {who}{M.req_label(req)}.')]
    out += _run_search(b, req, sess, intro="Checking existing inventory…", save_as_search=True)
    return out


def act_update_requirement(b: str, a: dict, sess: dict) -> list[Reply]:
    r = db.get_requirement(b, int(a.get("target_id") or 0))
    if not r:
        return [R("Which client requirement is that for?")]
    upd = N.normalize_requirement(a.get("updates") or {})
    for k, v in upd.items():
        if k in ("must_haves", "nice_to_haves"):
            r[k] = {**r.get(k, {}), **v}
            if k == "must_haves":  # a must overrides the same key in nice-to-have
                for kk in v:
                    r.get("nice_to_haves", {}).pop(kk, None)
        elif k == "micro_markets":
            if v:
                r[k] = v
        elif v not in (None, [], {}) and not (k == "purpose" and "purpose" not in (a.get("updates") or {})):
            r[k] = v
    db.save_requirement(b, r)
    db.log(b, "requirement", r["id"], "updated", a.get("updates"))
    out = [R(f'Updated requirement #{r["id"]}: {M.req_label(r)}.')]
    top = M.find_matches(r, db.visible_listings(b), 3)
    if top:
        out.append(R(f'Current best matches:\n{_fmt_results(top)}'))
    return out


def act_close_requirement(b: str, a: dict, sess: dict) -> list[Reply]:
    r = db.get_requirement(b, int(a.get("target_id") or 0))
    if not r:
        return [R("Which requirement should I close?")]
    r["status"] = "closed"
    db.save_requirement(b, r)
    db.log(b, "requirement", r["id"], "closed", "")
    return [R(f'Closed requirement #{r["id"]}.')]


def _run_search(b: str, req: dict, sess: dict, intro: str = "", save_as_search: bool = True) -> list[Reply]:
    matches = M.find_matches(req, db.visible_listings(b), 5)
    if not matches:
        kept = " (Your previous results are still available — say “show 1st”.)" if sess.get("last_search", {}).get("ids") else ""
        return [R("No matching listings for that right now. I'll alert you when something new fits." + kept)]
    if save_as_search:
        sess["last_search"] = {"criteria": req, "ids": [m["listing"]["id"] for m in matches]}
    n = len(matches)
    return [R(f'{n} match{"es" if n > 1 else ""}:\n{_fmt_results(matches)}\n\nWant details or photos? Say “show 2nd”.')]


def act_search(b: str, a: dict, sess: dict) -> list[Reply]:
    req = N.normalize_requirement(a.get("criteria") or {})
    return _run_search(b, req, sess)


def act_show(b: str, a: dict, sess: dict) -> list[Reply]:
    lid = a.get("listing_id")
    idx = a.get("result_index")
    if not lid and idx and sess.get("last_search"):
        ids = sess["last_search"]["ids"]
        lid = ids[int(idx) - 1] if 0 < int(idx) <= len(ids) else None
    l = db.get_listing_any(b, int(lid)) if lid else None
    if not l:
        return [R("Which one? Run a search first, then say “show 2nd”.")]
    if a.get("what") == "photos":
        photos = l.get("photos") or []
        if not photos:
            return [R(f'No photos saved for #{l["id"]} yet. Send one and I’ll attach it.')]
        return [R(f'Photos of #{l["id"]} ({M.label(l)}):', photos)]
    return [R(_details(l, b))]


def _details(l: dict, b: str) -> str:
    rows = [f'*#{l["id"]} {M.label(l)}*', f'Status: {l["status"].replace("_", " ")}  ·  last confirmed {M.days_since(l.get("last_confirmed"))}d ago',
            f'Price: {_price_str(l).replace("asking ", "")}']
    show = ["facing", "road_width_ft", "park_view", "corner", "shape", "vacant_or_built", "dimensions",
            "construction_grade", "furnishing", "parking_type", "lift", "plot_share_included",
            "society", "tower_unit", "super_sqft", "carpet_sqft", "maintenance", "owner_name", "owner_contact",
            "price_flexibility", "source"]
    ver = l.get("verified", {})
    for f in show:
        if l.get(f) is not None:
            v = "yes" if l[f] is True else "no" if l[f] is False else l[f]
            rows.append(f'{vocab.FIELD_LABELS.get(f, f.replace("_", " ")).capitalize()}: {v}' +
                        (" (verified)" if ver.get(f) else ""))
    if l.get("notes"):
        rows.append(f'Notes: {l["notes"]}')
    hist = [] if l.get("shared_by") else db.history(b, 5, "listing", l["id"])
    if l.get("shared_by"):
        rows.append(f'Shared by {l["shared_by"]} (team pool)')
    if hist:
        rows.append("History: " + " | ".join(f'{h["event"]}' + (f' ({h["detail"][:40]})' if h["detail"] else "") for h in hist))
    return "\n".join(rows)


# ---------------------------------------------------------------- notes, tasks, freshness
def act_note(b: str, a: dict, sess: dict) -> list[Reply]:
    from . import phase3 as P3
    text = (a.get("text") or "").strip()
    lid, rid = a.get("listing_id"), a.get("requirement_id")
    if text:
        rid, client = P3._resolve_req(b, rid, a.get("client_name"))
        P3.store_note(b, "note", text, int(lid) if lid and db.get_listing(b, int(lid)) else None, rid, client)
    if lid:
        l = db.get_listing(b, int(lid))
        if l:
            l["notes"] = f'{l["notes"]}; {text}' if l.get("notes") else text
            db.save_listing(b, l)
            db.log(b, "listing", l["id"], "note", text)
            return [R(f'Noted against #{l["id"]} ({M.label(l)}).')]
    if rid:
        db.log(b, "requirement", int(rid), "note", text)
        return [R(f'Noted against requirement #{rid}.')]
    db.log(b, "general", None, "note", text)
    return [R("Noted.")]


def act_task(b: str, a: dict, sess: dict) -> list[Reply]:
    tid = db.add_task(b, a.get("text") or "", a.get("due"), a.get("listing_id") if a.get("listing_id") and db.get_listing(b, int(a["listing_id"])) else None)
    due = f' (due {a["due"]})' if a.get("due") else ""
    return [R(f'Follow-up #{tid} added: {a.get("text")}{due}.')]


def act_stale(b: str, a: dict, sess: dict) -> list[Reply]:
    live = [l for l in db.list_listings(b) if l["status"] in ("available", "under_negotiation", "stale")]
    stale = [l for l in live if (M.days_since(l.get("last_confirmed")) or 0) > 30]
    fresh_pct = round(100 * (len(live) - len(stale)) / len(live)) if live else 100
    for l in stale:
        if l["status"] == "available":
            l["status"] = "stale"
            db.save_listing(b, l)
            db.log(b, "listing", l["id"], "status_changed", "available → stale (not confirmed in 30 days)")
    if not stale:
        return [R(f"All {len(live)} live listings were confirmed in the last 30 days")]
    lines = [f'#{l["id"]} {M.label(l)} — {N.fmt_money(l.get("price_total"), l.get("purpose") == "rent")} '
             f'({M.days_since(l.get("last_confirmed"))}d)' for l in stale[:12]]
    return [R(f'{len(stale)} of {len(live)} live listings are not confirmed in 30+ days ({fresh_pct}% fresh):\n' +
              "\n".join(lines) + '\n\nReply like “#12 still available”, “#14 sold”, or “#15 price now 4 cr”.')]


# ---------------------------------------------------------------- suggestions, tasks, briefing
def suggest(b: str, intents: list, sess: dict) -> list[dict]:
    """Next best actions as tap-able chips: {"label", and one of "send" (send text) | "fill" (prefill input) | "share" (ids)}."""
    out: list[dict] = []
    ids = (sess.get("last_search") or {}).get("ids", [])
    if "search" in intents or "save_requirement" in intents or "view_alert" in intents:
        if ids:
            out += [{"label": "Show the first one", "send": "show 1st"},
                    {"label": "Send top matches to client", "share": ids[:3]},
                    {"label": "Anything cheaper?", "send": "anything cheaper?"}]
        else:
            out += [{"label": "Widen budget by 10%", "send": "increase the budget by 10 percent"}]
    elif "save_listing" in intents or "update_listing" in intents:
        l = db.get_listing(b, int(sess["last_listing_id"])) if sess.get("last_listing_id") else None
        if l:
            if l.get("owner_contact") is None:
                out.append({"label": "Add owner contact", "fill": f'Owner of #{l["id"]} is '})
            if l.get("low_confidence"):
                out.append({"label": "Looks correct", "send": "ok"})
            out += [{"label": "Send to client", "share": [l["id"]]},
                    {"label": "Mark as in talks", "send": f'#{l["id"]} under negotiation'}]
    elif "stale_check" in intents:
        out += [{"label": "Some are sold", "fill": "These are sold: #"}, {"label": "Today's summary", "send": "today's summary"}]
    elif "eod_report" in intents or "activity_log" in intents:
        out += [{"label": "My follow-ups", "send": "my follow-ups"}, {"label": "Which properties need a check?", "send": "which listings need confirming?"}]
    elif "list_tasks" in intents:
        out += [{"label": "Today's summary", "send": "today's summary"}]
    return out[:3]


def act_list_tasks(b: str, a: dict, sess: dict) -> list[Reply]:
    tasks = db.list_tasks(b, only_open=True)
    if not tasks:
        return [R("No open follow-ups")]
    today = datetime.now(timezone.utc).date().isoformat()
    lines = []
    for t in sorted(tasks, key=lambda t: t["due"] or "9999"):
        flag = " — *OVERDUE*" if t["due"] and t["due"][:10] < today else (" — due today" if t["due"] and t["due"][:10] == today else "")
        lines.append(f'#{t["id"]} {t["text"]}' + (f' (due {t["due"][:10]})' if t["due"] else "") + flag)
    return [R(f'{len(tasks)} open follow-up{"s" if len(tasks) > 1 else ""}:\n' + "\n".join(lines) + "\n\nSay “#3 done” when one is finished.")]


def briefing(b: str) -> dict:
    """What deserves attention right now: computed in code (instant, no model call)."""
    from . import scheduler
    ls = db.list_listings(b)
    live = [l for l in ls if l["status"] in ("available", "under_negotiation", "stale")]
    stale = [l for l in live if (M.days_since(l.get("last_confirmed")) or 0) > 30]
    flagged = [l for l in live if l.get("low_confidence")]
    vis = db.visible_listings(b)
    matched = [(r, M.find_matches(r, vis, 3)) for r in db.list_requirements(b, only_open=True)]
    matched = [(r, m) for r, m in matched if m]
    today = scheduler.ist().date().isoformat()
    due = [t for t in db.list_tasks(b, only_open=True) if t["due"] and t["due"][:10] <= today]
    items = []
    if due:
        items.append({"text": f'{len(due)} follow-up{"s are" if len(due) > 1 else " is"} due', "send": "my follow-ups"})
    if matched:
        names = ", ".join((r.get("client_name") or f'#{r["id"]}') for r, _ in matched[:2])
        items.append({"text": f'{len(matched)} client{"s have" if len(matched) > 1 else " has"} matching properties ({names})',
                      "send": f'find matches for {(matched[0][0].get("client_name") or "the first client")}'})
    if stale:
        items.append({"text": f'{len(stale)} propert{"ies need" if len(stale) > 1 else "y needs"} a quick check',
                      "send": "which listings need confirming?"})
    if flagged:
        items.append({"text": f'{len(flagged)} propert{"ies have" if len(flagged) > 1 else "y has"} details to confirm', "send": "ok"})
    hr = scheduler.ist().hour
    name = ((db.get_broker(b) or {}).get("name") or "").replace(" (demo)", "").split(" ")[0]
    greet = "Good morning" if hr < 12 else "Good afternoon" if hr < 17 else "Good evening"
    return {"greeting": f"{greet}, {name}" if name else greet, "items": items,
            "stats": {"live": len(live), "fresh_pct": round(100 * (len(live) - len(stale)) / len(live)) if live else 100,
                      "open_requirements": len(db.list_requirements(b, only_open=True))}}


# ---------------------------------------------------------------- team pool (Phase 4)
def set_shared(b: str, ids: list[int], shared: bool) -> list[dict]:
    """Share/unshare own listings with the brokerage team and alert teammates whose open requirements fit."""
    from . import scheduler
    done = []
    for lid in ids:
        l = db.get_listing(b, int(lid))
        if not l or bool(l.get("shared")) == shared:
            continue
        l["shared"] = shared
        db.save_listing(b, l)
        db.log(b, "listing", l["id"], "shared" if shared else "unshared", "team pool")
        done.append(l)
    if shared:
        me = (db.get_broker(b) or {}).get("name", b)
        for t in db.team_ids(b):
            if t == b:
                continue
            for l in done:
                for r in db.list_requirements(t, only_open=True):
                    m = M.match_one(r, l)
                    if m:
                        who = r.get("client_name") or f'requirement #{r["id"]}'
                        scheduler.send(t, f'{me} shared a listing that matches {who}: {M.label(l)}, '
                                          f'{N.fmt_money(l.get("price_total"), l.get("purpose") == "rent")}. '
                                          f'Fits: {", ".join(m["matches"][:4])}.', "team_match",
                                       datetime.now(timezone.utc))
    return done


def act_share(b: str, a: dict, sess: dict) -> list[Reply]:
    if len(db.team_ids(b)) < 2:
        return [R("You're not part of a team, so there is nobody to share with. Your inventory stays private.")]
    ids = [int(x) for x in a.get("target_ids") or []]
    if a.get("all"):
        ids = [l["id"] for l in db.list_listings(b) if l["status"] in ("available", "under_negotiation")]
    shared = a.get("shared", True) is not False
    done = set_shared(b, ids, shared)
    if not done:
        return [R("Nothing changed — say which listing(s), e.g. “share #4 with the team”.")]
    names = ", ".join(f'#{l["id"]}' for l in done)
    return [R(f'{"Shared" if shared else "Unshared"} {names} {"with" if shared else "from"} your team pool.' +
              (" Teammates see price, specs and owner contact; they can’t edit it." if shared else ""))]


def act_view_alert(b: str, a: dict, sess: dict) -> list[Reply]:
    aw = db.get_settings(b).get("awaiting")
    if not aw or aw.get("kind") != "alert":
        return [R("Nothing to view right now.")]
    if aw.get("entity") == "requirement":
        r = db.get_requirement(b, int(aw["id"]))
        if not r:
            return [R("That requirement is gone.")]
        return _run_search(b, r, sess)
    l = db.get_listing_any(b, int(aw["id"]))
    return [R(_details(l, b))] if l else [R("That listing is gone.")]
