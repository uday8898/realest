"""Offline Phase 3 tests: scheduler timing, 24h window, activity log, outcomes, EOD summary, recall."""
import os, sys, tempfile
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["WA_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t3.db")
from app import db, engine, phase3, scheduler as S, seed, matching as M  # noqa: E402

seed.run(reset=True)
phase3.embed_fn = lambda texts: None                       # lexical path (no network)
phase3.text_llm = lambda system, user, max_tokens=500: "STUB: " + user[:60].replace("\n", " ")
IST = S.IST
today = datetime.now(timezone.utc).astimezone(IST).replace(second=0, microsecond=0)
at = lambda h, m=0: today.replace(hour=h, minute=m).astimezone(timezone.utc)


def say(actions, broker="rohit"):
    engine.set_understander(lambda t, i, c: {"actions": actions})
    import json
    return "\n".join(r["text"] for r in engine.handle(broker, json.dumps(actions), []))


# --- due task reminder fires in business hours (once); 3-hour ping only after an interval has passed
first = S.tick(at(10), only="rohit")
assert [m["kind"] for m in first] == ["task_reminder"], first                # seeded task is due today
assert first[0]["template"], "no broker message in last 24h -> must be a template"
assert db.get_settings("rohit")["awaiting"]["kind"] == "task"
sent = S.tick(at(13), only="rohit")                                          # 09:00 anchor + 3h passed
assert [m["kind"] for m in sent] == ["activity_ping"] and "last 3 hours" in sent[0]["text"], sent
assert S.tick(at(13, 5), only="rohit") == [], "no duplicate ping"
assert [m["kind"] for m in S.tick(at(16), only="rohit")] == ["activity_ping"]
assert S.tick(at(7), only="rohit") == [], "outside business hours"

# --- 24h window: after the broker writes, proactive messages are free-form (not templates)
db.add_message("rohit", "broker", "hi")
db.update_settings("rohit", last_ping=None)
assert S.tick(at(13, 30), only="rohit")[0]["template"] is False

# --- end-of-day prompt once per day, and not blocked by the 3h ping
eod = S.tick(at(19, 5), only="rohit")
assert [m["kind"] for m in eod] == ["eod_prompt"] and S.tick(at(19, 30), only="rohit") == []

# --- settings: snooze + interval via chat
db.update_settings("rohit", last_ping=None, awaiting=None, last_eod_date=None)
out = say([{"intent": "settings", "changes": {"interval_hours": 2, "snooze_until": "23:59"}}])
assert "every 2 hours" in out and "quiet until" in out, out
assert S.tick(at(14), only="rohit") == [], "snoozed"
say([{"intent": "settings", "changes": {"reminders": "off"}}])
assert S.tick(at(15), only="rohit") == []
say([{"intent": "settings", "changes": {"reminders": "on", "snooze_until": None}}])
db.update_settings("rohit", snooze_until=None)

# --- activity log: links to listing + client, extracts follow-up, clears AWAITING
gcr = next(l for l in db.list_listings("rohit") if l["micro_market"] == "Golf Course Road" and l["type"] == "builder_floor")
kap = next(r for r in db.list_requirements("rohit") if r["client_name"] == "Mr. Kapoor")
db.update_settings("rohit", awaiting={"kind": "activity"})
out = say([{"intent": "activity_log", "eod": False, "entries": [{
    "kind": "visit", "summary": "Visited the Golf Course Road floor with Mr. Kapoor; he wants a lower price.",
    "listing_id": gcr["id"], "client_name": "Mr Kapoor",
    "follow_ups": [{"text": "Negotiate price with the owner", "due": "2099-01-02"}]}]}])
assert "Logged 1 item" in out and f'#{gcr["id"]}' in out and "Mr. Kapoor" in out and "Negotiate price" in out, out
n = [x for x in db.list_notes("rohit") if x["kind"] == "visit" and x["requirement_id"] == kap["id"]]
assert len(n) == 2  # seeded visit + the new one, both linked to Mr. Kapoor's requirement
assert db.get_settings("rohit")["awaiting"] is None
assert "skipped" in say([{"intent": "activity_log", "skipped": True, "entries": []}])

# --- follow-up outcome closes the task and is stored as a note
t = next(t for t in db.list_tasks("rohit", only_open=True) if "2% discount" in t["text"])
out = say([{"intent": "task_done", "target_id": t["id"], "outcome": "Owner agreed to 2% off if token paid by Friday",
            "follow_ups": [{"text": "Collect token from Mr. Kapoor", "due": "2099-01-05"}]}])
assert "Done" in out and "Collect token" in out and db.get_task("rohit", t["id"])["done"] == 1

# --- end-of-day report
eod_txt = phase3.build_eod("rohit")
assert "Visits done" in eod_txt and "Open follow-ups" in eod_txt and "STUB" in eod_txt, eod_txt

# --- recall: entity scoping + date filter + isolation
c = phase3.recall_candidates("rohit", {"question": "what did the owner say about price", "listing_id": gcr["id"]})
assert c and all(x["listing_id"] == gcr["id"] for x in c) and any("2%" in x["text"] for x in c)
c = phase3.recall_candidates("rohit", {"question": "what did I discuss", "client_name": "Mr. Gupta"})
assert c and "vastu" in c[0]["text"], c[0]["text"]
c = phase3.recall_candidates("rohit", {"question": "power backup", "since": (today - timedelta(days=7)).date().isoformat()})
assert "power backup" in c[0]["text"] and all(x["ts"][:10] >= (today - timedelta(days=7)).date().isoformat() for x in c)
assert "Based on" in say([{"intent": "recall", "question": "power backup Sushant Lok"}])
anita_ids = {l["id"] for l in db.list_listings("anita")}
got = phase3.recall_candidates("anita", {"question": "price owner Golf Course Road"})
assert all(n["listing_id"] in anita_ids for n in got), "isolation: only Anita's own notes"
assert not any("Kapoor" in n["text"] or "Gupta" in n["text"] for n in got)
print("all phase 3 tests passed")
