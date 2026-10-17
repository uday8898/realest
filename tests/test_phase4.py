"""Offline Phase 4 tests: team pool + isolation, group filtering/alerts, admin access, WhatsApp routing."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["WA_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t4.db")
from app import admin, db, engine, groups, llm, normalize as N, phase3, seed, whatsapp  # noqa: E402

seed.run(reset=True)
llm.configured = lambda: True
phase3.embed_fn = lambda texts: None
phase3.text_llm = lambda *a, **k: "stub"
calls = []


def stub(actions):
    def f(text, images, ctx):
        calls.append(ctx)
        return {"actions": actions}
    engine.set_understander(f)


def say(actions, broker="yuvraj"):
    stub(actions)
    import json
    return "\n".join(r["text"] for r in engine.handle(broker, json.dumps(actions), []))


def msgs(b):
    return [m["text"] for m in db.messages(b) if m["role"] == "bot"]


# ============================ team pool & isolation
assert set(db.team_ids("yuvraj")) == {"yuvraj", "anita"} and db.team_ids("vikram") == ["vikram"]
vis = db.visible_listings("yuvraj")
shared_by = {l["id"]: l["shared_by"] for l in vis if l.get("shared_by")}
assert len(shared_by) == 2 and all("Anita" in v for v in shared_by.values()), shared_by
private = next(l for l in db.list_listings("anita") if "PRIVATE" in (l.get("notes") or ""))
assert private["id"] not in [l["id"] for l in vis], "teammate's private listing must stay private"
assert not any(l["broker_id"] == "vikram" for l in vis), "other brokerage never visible"
assert all(l["broker_id"] == "vikram" for l in db.visible_listings("vikram")), "vikram sees only his own"

# shared listings take part in matching and are labelled; they are read-only for the teammate
out = say([{"intent": "search", "criteria": dict(type="builder_floor", micro_markets=["Sushant Lok 1"], bedrooms=4,
                                                  budget_max={"value": 8, "unit": "cr"}, must_haves={"park_view": True})}])
assert "team pool (Anita" in out, out
team_id = next(i for i, v in shared_by.items() if db.get_listing_any("yuvraj", i)["type"] == "builder_floor")
assert "Shared by Anita" in say([{"intent": "show", "listing_id": team_id, "what": "details"}])
assert "couldn't tell which listing" in say([{"intent": "update_listing", "target_id": team_id,
                                              "updates": {"price": {"value": 1, "unit": "cr"}}}])
assert db.get_listing("yuvraj", team_id) is None

# sharing: a new match alerts the teammate's open requirement
raw = dict(type="builder_floor", micro_market="Sushant Lok 1", bedrooms=4, floor_number=2, park_view=True,
           size={"value": 260, "unit": "gaj"}, price={"value": 7.6, "unit": "cr"})
ch, _, _ = N.normalize_listing(raw)
ch.update(purpose="sale", verified={}, low_confidence=[], asked=[])
lid = db.add_listing("anita", ch)
before = len(msgs("yuvraj"))
assert "Shared" in say([{"intent": "share", "target_ids": [lid], "shared": True}], broker="anita")
new = msgs("yuvraj")[before:]
assert any("Anita" in m and "Mr. Sharma" in m for m in new), new
assert db.get_listing("anita", lid)["shared"] is True
assert "Unshared" in say([{"intent": "share", "target_ids": [lid], "shared": False}], broker="anita")
assert lid not in [l["id"] for l in db.visible_listings("yuvraj")]
assert "not part of a team" in say([{"intent": "share", "all": True}], broker="vikram")

# ============================ group handling
def must_not_call(*a):
    raise AssertionError("LLM must not be called for obvious chatter")


engine.set_understander(must_not_call)
assert groups.ingest("yuvraj", "Sushant Lok Brokers", "Deepak", "Good morning all 🙏")["outcome"] == "ignored"

n_req = len(db.list_requirements("yuvraj"))
stub([{"intent": "save_requirement", "requirement": dict(
    type="builder_floor", micro_markets=["Sushant Lok"], bedrooms=3,
    budget_min={"value": 6, "unit": "cr"}, budget_max={"value": 8, "unit": "cr"}, must_haves={"park_view": True})}])
txt = "Urgent requirement: 3 BHK floor Sushant Lok 6 to 7 cr park facing must"
r = groups.ingest("yuvraj", "Sushant Lok Brokers", "Mukesh", txt)
assert r["outcome"] == "surfaced" and "New requirement in Sushant Lok Brokers" in r["alert"], r
assert "match" in r["alert"] and "View?" in r["alert"], r["alert"]
assert len(db.list_requirements("yuvraj")) == n_req + 1
assert db.get_settings("yuvraj")["awaiting"]["kind"] == "alert"
assert any("New requirement in" in m for m in msgs("yuvraj")), "alert delivered to the broker's chat"
assert "GROUP_MODE" in calls[-1]

# "View?" -> yes
out = say([{"intent": "view_alert"}])
assert "match" in out and db.get_settings("yuvraj")["awaiting"] is None, out

# same broadcast re-posted: dropped by fingerprint, no second LLM call
engine.set_understander(must_not_call)
assert groups.ingest("yuvraj", "Sushant Lok Brokers", "Mukesh", txt)["outcome"] == "duplicate"

# model says "not a listing/requirement" -> ignored and NOTHING is stored
stub([])
n_notes, n_l = len(db.list_notes("yuvraj")), len(db.list_listings("yuvraj"))
assert groups.ingest("yuvraj", "GGN Plots & Floors", "Ravi", "Gold price is up again today, anyone investing?")["outcome"] == "ignored"
assert groups.ingest("yuvraj", "GGN Plots & Floors", "Neha", "Free trading signals 300% returns join now click link")["outcome"] == "ignored"
assert len(db.list_notes("yuvraj")) == n_notes and len(db.list_listings("yuvraj")) == n_l, "ignored messages are not stored"

# group posts are extracted WITHOUT inventory context (numbers in context can leak into extraction)
assert "LISTINGS (id" not in calls[-1] and "GROUP_MODE" in calls[-1]

# broadcast with two new listings -> ONE alert; listing source = group; fits an open requirement
stub([{"intent": "save_listing", "listing": dict(type="builder_floor", micro_market="Sushant Lok 3", bedrooms=4, floor_number=3,
                                                 park_view=True, price={"value": 7.2, "unit": "cr"}, size={"value": 250, "unit": "gaj"})},
      {"intent": "save_listing", "listing": dict(type="plot", micro_market="DLF Phase 5", size={"value": 350, "unit": "gaj"},
                                                 price={"value": 6, "unit": "lakh", "basis": "per_gaj"})}])
before = len(msgs("yuvraj"))
r = groups.ingest("yuvraj", "Sushant Lok Brokers", "Pooja", "Fresh inventory: 1. SL3 4BHK TF 7.2cr 2. DLF 5 350 gaj plot 6L/gaj")
assert r["outcome"] == "surfaced" and r["alert"].count("New listing in") == 2 and "   Fits " in r["alert"], r["alert"]
assert len(msgs("yuvraj")) == before + 1, "one alert per message"
newl = [l for l in db.list_listings("yuvraj") if l.get("source") == "group: Sushant Lok Brokers"]
assert len(newl) == 2

# a listing we already hold (seen in another group) is merged silently
stub([{"intent": "save_listing", "listing": dict(type="plot", micro_market="DLF Phase 1", size={"value": 250, "unit": "gaj"},
                                                 corner=True, facing="north", price={"value": 15, "unit": "cr"})}])
before_alerts = len(msgs("yuvraj"))
r = groups.ingest("yuvraj", "GGN Plots & Floors", "Sanjay", "250 gaj corner plot DLF 1 north facing 15 cr available")
assert r["outcome"] == "duplicate" and len(msgs("yuvraj")) == before_alerts

# muted group: nothing processed
db.set_group_muted("yuvraj", "Noisy Group", True)
engine.set_understander(must_not_call)
assert groups.ingest("yuvraj", "Noisy Group", "X", "3 BHK Sushant Lok 7 cr available call me")["outcome"] == "muted"
stats = {g["name"]: g for g in db.list_groups("yuvraj")}
assert stats["Sushant Lok Brokers"]["surfaced"] >= 2 and stats["GGN Plots & Floors"]["ignored"] >= 2

# group data is per broker (Anita's group traffic never touches Yuvraj's data)
engine.set_understander(lambda *a: {"actions": []})
groups.ingest("anita", "Anita's Group", "Z", "Some longer message that is not a requirement at all, just chat")
assert db.list_groups("anita") and not any(g["name"] == "Anita's Group" for g in db.list_groups("yuvraj"))

# ============================ admin
ov = admin.overview("yuvraj")
assert ov["brokerage"]["name"] == "Chadha Realty" and {m["id"] for m in ov["members"]} == {"yuvraj", "anita"}
try:
    admin.overview("anita")
    raise AssertionError("a non-owner member must be refused")
except admin.Forbidden:
    pass
vo = admin.overview("vikram")  # owner of a *different* brokerage: sees only their own team
assert vo["brokerage"]["name"] == "Singh Estates" and [m["id"] for m in vo["members"]] == ["vikram"]
ex = admin.export_rows("yuvraj")
assert any("PRIVATE" in (l.get("notes") or "") for l in ex["listings"]), "owner export includes the team's data"
assert not any(l["broker_id"] == "vikram" for l in ex["listings"]), "never another brokerage"
assert all("embedding" not in n for n in ex["notes"])
csv_text = admin.listings_csv("yuvraj")
assert csv_text.startswith("broker,id,type") and "Anita" in csv_text
assert any(h["event"] == "export" for h in db.history("yuvraj", 20))
nb = admin.add_broker("yuvraj", "Suresh Kumar", "+91 98765 43210")
assert nb["id"] == "suresh-kumar" and "suresh-kumar" in db.team_ids("yuvraj")
try:
    admin.add_broker("yuvraj", "Dup", "98765 43210")
    raise AssertionError("duplicate phone must be refused")
except ValueError:
    pass
try:
    admin.add_broker("anita", "Nope", "9000000000")
    raise AssertionError("non-owner can't add brokers")
except admin.Forbidden:
    pass

# ============================ WhatsApp webhook routing (phone number = identity)
sent = []
payload = lambda frm, body: {"entry": [{"changes": [{"value": {"messages": [
    {"from": frm, "id": "wamid.1", "type": "text", "text": {"body": body}}]}}]}]}
stub([{"intent": "chat", "reply": "hello anita"}])
res = whatsapp.handle_incoming(payload("919810000002", "good morning team"), send=lambda to, t: sent.append((to, t)))
assert res[0]["routed_to"] == "anita" and sent[-1] == ("919810000002", "hello anita")
assert any(m["text"] == "good morning team" for m in db.messages("anita") if m["role"] == "broker")
assert not any(m["text"] == "good morning team" for m in db.messages("yuvraj") if m["role"] == "broker"), "routed to the right broker only"
n_before = len(db.list_listings("yuvraj"))
res = whatsapp.handle_incoming(payload("919999999999", "250 gaj plot DLF 1"), send=lambda to, t: sent.append((to, t)))
assert res[0]["routed_to"] is None and "isn't registered" in sent[-1][1]
assert len(db.list_listings("yuvraj")) == n_before
# Meta retries: the same wamid must not be processed twice; a failing sender must not break the webhook
n_msgs = len(db.messages("anita"))
res = whatsapp.handle_incoming(payload("919810000002", "good morning team"), send=lambda to, t: sent.append((to, t)))
assert res[0].get("duplicate") and len(db.messages("anita")) == n_msgs, "idempotent on wamid"
def boom(to, t):
    raise RuntimeError("network down \u2705")
p2 = payload("919810000002", "another")
p2["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = "wamid.2"
assert whatsapp.handle_incoming(p2, send=boom)[0]["routed_to"] == "anita"
status_only = {"entry": [{"changes": [{"value": {"statuses": [{"id": "x", "status": "delivered"}]}}]}]}
assert whatsapp.parse_webhook(status_only) == []
assert whatsapp.parse_webhook({"entry": [{"changes": [{"value": {"messages": [
    {"from": "1", "type": "audio", "audio": {"id": "m1", "mime_type": "audio/ogg"}}]}}]}]})[0]["media_id"] == "m1"

print("all phase 4 tests passed")
