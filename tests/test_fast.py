"""Offline tests: fast path (no model call), suggestions, briefing, timing metadata."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["WA_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "tf.db")
from app import db, engine, fastpath as F, llm, phase3, seed, service  # noqa: E402

seed.run(reset=True)
llm.configured = lambda: True
phase3.embed_fn = lambda texts: None
phase3.text_llm = lambda *a, **k: "stub"
route = F.route

# ---- pure routing table
S_SEARCH = {"last_search": {"ids": [4, 5, 6]}}
AW_ALERT, AW_ACT, AW_EOD = ({"awaiting": {"kind": k}} for k in ("alert", "activity", "eod"))
assert route("yes", {}, AW_ALERT) == [{"intent": "view_alert"}]
assert route("Show me!", {}, AW_ALERT) == [{"intent": "view_alert"}]
assert route("ok", {"pending_confirm": {"4": ["price_total"]}}, {}) == [{"intent": "confirm"}]
assert route("ok", {}, {}) is None, "'ok' means nothing when nothing is pending: let the model decide"
assert route("skip", {}, AW_ACT)[0]["skipped"] is True and route("Skip.", {}, AW_EOD)[0]["intent"] == "activity_log"
assert route("skip", {}, {}) is None
assert route("show 2nd", S_SEARCH, {}) == [{"intent": "show", "result_index": 2, "what": "details"}]
assert route("show the second one's photos", S_SEARCH, {}) is None  # ambiguous phrasing -> model
assert route("show the 3rd one", S_SEARCH, {}) == [{"intent": "show", "result_index": 3, "what": "details"}]
assert route("photos of 2nd", S_SEARCH, {})[0]["what"] == "photos"
assert route("show 2nd", {}, {}) is None, "no search yet -> nothing to show"
assert route("Which listings need confirming?", {}, {}) == [{"intent": "stale_check"}]
assert route("today's summary", {}, {}) == [{"intent": "eod_report"}]
assert route("my follow-ups", {}, {}) == [{"intent": "list_tasks"}]
assert route("hi", {}, {})[0]["intent"] == "chat" and "*help*" in route("hello", {}, {})[0]["reply"]
assert "Find matches" in route("help", {}, {})[0]["reply"]
# anything with real content goes to the model
for t in ("find me a 3 BHK floor in Sushant Lok under 8 cr", "250 gaj plot DLF 1 6 per gaj", "ok but change the price to 7 cr",
          "show me plots in DLF 1", "yes the owner said no", "x" * 80):
    assert route(t, S_SEARCH, {"awaiting": {"kind": "activity"}}) is None, t


# ---- end to end: fast messages never reach the model
def boom(*a):
    raise AssertionError("model must not be called on the fast path")


engine.set_understander(boom)
t0 = time.perf_counter()
out = engine.handle("yuvraj", "which listings need confirming?")
assert "not confirmed in 30+ days" in out[0]["text"]
assert engine.handle("yuvraj", "my follow-ups")[0]["text"].startswith("1 open follow-up")
assert "Add inventory" in engine.handle("yuvraj", "help")[0]["text"]
assert time.perf_counter() - t0 < 1.0, "fast path should be near-instant"
assert db.get_session("yuvraj")["_meta"]["fast"] is True

# images disable the fast path (a photo + "ok" is not a confirmation)
calls = []
engine.set_understander(lambda t, i, c: calls.append(t) or {"actions": []})
engine.handle("yuvraj", "ok", [("image/png", b"x")])
assert calls == ["ok"]

# ---- the model path still works and marks itself as such
engine.set_understander(lambda t, i, c: {"actions": [{"intent": "search", "criteria": dict(
    type="builder_floor", micro_markets=["Sushant Lok"], bedrooms=3, budget_max={"value": 8, "unit": "cr"},
    must_haves={"park_view": True})}]})
r = service.process_message("yuvraj", "find me a 3 BHK floor in Sushant Lok under 8 cr, park facing")
assert r["fast"] is False and isinstance(r["ms"], int) and r["ms"] >= 0
labels = [s["label"] for s in r["suggestions"]]
assert "Show the first one" in labels and any("share" in s for s in r["suggestions"]) and len(r["suggestions"]) <= 3, r["suggestions"]
share = next(s for s in r["suggestions"] if "share" in s)
assert share["share"] and all(db.get_listing_any("yuvraj", i) for i in share["share"])

# "show 1st" now resolves instantly from the stored search results
engine.set_understander(boom)
r = service.process_message("yuvraj", "show 1st")
assert r["fast"] is True and "Price" in r["replies"][0]["text"] and r["ms"] < 500, r

# ---- suggestions after saving a listing
engine.set_understander(lambda t, i, c: {"actions": [{"intent": "save_listing", "listing": dict(
    type="plot", micro_market="DLF Phase 5", size={"value": 300, "unit": "gaj"}, price={"value": 6, "unit": "lakh", "basis": "per_gaj"})}]})
r = service.process_message("yuvraj", "300 gaj plot DLF 5 6 per gaj")
assert any(s["label"] == "Add owner contact" and "fill" in s for s in r["suggestions"]), r["suggestions"]

# ---- briefing is computed in code and reflects real state
b = engine.briefing("yuvraj")
texts = " | ".join(i["text"] for i in b["items"])
assert b["greeting"].endswith("Yuvraj") and "follow-up" in texts and "need a quick check" in texts and "matching properties" in texts, texts
assert all(i["send"] for i in b["items"]) and b["stats"]["open_requirements"] >= 3
assert engine.briefing("vikram")["stats"]["open_requirements"] == 0

print("all fast-path tests passed")
