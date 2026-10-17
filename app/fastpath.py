"""Fast path: answer unambiguous, high-frequency messages locally, with NO model call (milliseconds, not seconds).

Only exact, anchored patterns qualify. Anything ambiguous falls through to the model, so this can make the app
faster but never make it dumber. A fast reply is only produced when the state it depends on exists (e.g. "ok"
confirms something only if a confirmation is actually pending).
"""
import re
from typing import Optional

ORD = {"1st": 1, "first": 1, "2nd": 2, "second": 2, "3rd": 3, "third": 3, "4th": 4, "fourth": 4, "5th": 5, "fifth": 5,
       "1": 1, "2": 2, "3": 3, "4": 4, "5": 5}
_N = "|".join(sorted(ORD, key=len, reverse=True))

YES = {"ok", "okay", "yes", "y", "yep", "yeah", "haan", "ha", "han", "hanji", "sahi hai", "correct", "confirm",
       "confirmed", "thik hai", "theek hai", "done", "sure"}
VIEW = {"view", "show", "show me", "yes show me", "yes view", "dikhao", "dikha do", "show matches", "yes show"}
SKIP = {"skip", "nothing", "nothing happened", "no update", "no updates", "nahi", "nope", "pass", "later", "kuch nahi"}
STALE = {"stale", "stale listings", "stale check", "which listings need confirming", "what needs confirming",
         "which listings need confirmation", "what is stale", "listings to confirm", "freshness"}
SUMMARY = {"summary", "todays summary", "today summary", "give me todays summary", "eod", "eod report",
           "end of day report", "day summary", "my day", "daily report"}
TASKS = {"follow ups", "followups", "my follow ups", "my tasks", "tasks", "pending", "whats pending",
         "what is pending", "pending tasks", "to do", "todo", "my todo"}
HELLO = {"hi", "hello", "hey", "hii", "namaste", "hi there"}
HELP = {"help", "what can you do", "commands", "how does this work"}

HELP_TEXT = (
    "Here's what I can do:\n"
    "• *Add inventory*: forward any listing (text, photo, PDF or voice note)\n"
    "• *Save a client need*: “Client wants a 4 BHK in Sushant Lok, 7–8 cr, park facing”\n"
    "• *Find matches*: “find me a 3 BHK floor under 8 cr”, then “anything cheaper?”\n"
    "• *Fix things*: “change the price of #4 to 7.2 cr”\n"
    "• *Remember*: “what did the Golf Course Road owner say about price?”\n"
    "• *Stay on top*: “which listings need confirming?”, “my follow-ups”, “today's summary”\n"
    "• *Share*: tap ↗ on any answer to send it to a client on WhatsApp")


def norm(text: str) -> str:
    t = (text or "").lower().replace("'", "").replace("’", "")
    t = re.sub(r"[^a-z0-9ऀ-ॿ ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def route(text: str, sess: dict, settings: dict) -> Optional[list[dict]]:
    """Return a list of actions (same shape the model returns) or None to fall through to the model."""
    t = norm(text)
    if not t or len(t) > 60:
        return None
    aw = (settings or {}).get("awaiting") or {}

    if aw.get("kind") == "alert" and (t in VIEW or t in YES):
        return [{"intent": "view_alert"}]
    if t in YES and sess.get("pending_confirm"):
        return [{"intent": "confirm"}]
    if t in SKIP and aw.get("kind") in ("activity", "eod"):
        return [{"intent": "activity_log", "skipped": True, "eod": False, "entries": []}]

    if sess.get("last_search", {}).get("ids"):
        m = re.fullmatch(rf"(?:show|open|see|dikhao)(?: me)?(?: the)? ({_N})(?: one| result| listing)?(?: (details?|photos?|pics?|pictures?))?", t) \
            or re.fullmatch(rf"(photos?|pics?|pictures?|details?)(?: of| for)?(?: the)? ({_N})(?: one| result| listing)?", t)
        if m:
            g = m.groups()
            if g[0] in ORD:
                n, what = ORD[g[0]], (g[1] or "details")
            else:
                n, what = ORD[g[1]], g[0]
            return [{"intent": "show", "result_index": n, "what": "photos" if re.match(r"(photo|pic)", what or "") else "details"}]

    if t in STALE or re.fullmatch(r"(which|what) (listings? )?(need|needs) (re )?confirm\w*", t):
        return [{"intent": "stale_check"}]
    if t in SUMMARY:
        return [{"intent": "eod_report"}]
    if t in TASKS:
        return [{"intent": "list_tasks"}]
    if t in HELLO:
        return [{"intent": "chat", "reply": "Hi! Forward me a listing, tell me what a client wants, or ask me to find something. Say *help* for ideas."}]
    if t in HELP:
        return [{"intent": "chat", "reply": HELP_TEXT}]
    return None
