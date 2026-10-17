"""Offline tests: the LLM is replaced by canned 'understanding' so the deterministic parts
(unit/price maths, vocab mapping, dedup, matching, isolation, freshness) are verified without any API key.
Run:  .venv\\Scripts\\python tests\\test_engine.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["WA_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")

from app import db, engine, matching as M, normalize as N, seed  # noqa: E402

seed.run(reset=True)


import json  # noqa: E402


def say(actions, broker="yuvraj", text=None):
    engine.set_understander(lambda t, i, c: {"actions": actions})
    return engine.handle(broker, text if text is not None else json.dumps(actions), [])


def txt(replies):
    return "\n".join(r["text"] for r in replies)


# --- unit & price maths
assert N.area_to_gaj({"value": 2700, "unit": "sq ft"}) == 300
assert N.area_to_gaj({"value": 2, "unit": "marla"}) == 60.5
t, pg, w = N.price({"value": 6, "unit": None, "basis": "per_gaj"}, 250)
assert (t, pg) == (150_000_000, 600_000) and not w, (t, pg, w)
t, pg, w = N.price({"value": 7.25, "unit": "cr", "basis": "total"}, 250)
assert t == 72_500_000 and round(pg) == 290_000
assert N.price({"value": 15, "unit": None, "basis": None}, None)[2] == ["price_total"]  # assumed unit -> flagged
assert N.canon_market("dlf ph 1") == "DLF Phase 1" and N.canon_market("Sushant Lok") == "Sushant Lok"
assert N.norm_enum("facing", "NE") == "north-east" and N.norm_enum("furnishing", "semi furnished") == "semi-furnished"
assert N.fmt_money(150_000_000) == "15 cr" and N.fmt_money(7_250_000) == "72.5 lakh"

# --- intake: the proposal's own example
r = say([{"intent": "save_listing", "listing": dict(
    type="plot", micro_market="DLF 5", size={"value": 250, "unit": "gaj"}, corner=True, facing="north",
    road_width_ft=24, price={"value": 6, "unit": None, "basis": "per_gaj"}, price_flexibility="firm",
    confidence={"price": 0.9})}])
t0 = txt(r)
assert "Saved" in t0 and "250 gaj corner plot" in t0 and "DLF Phase 5" in t0 and "15 cr" in t0 and "6 lakh/gaj" in t0, t0
assert "owner contact" in t0 and "Not sure" not in t0, t0  # asks only for what's missing

# --- suspicious price is flagged, not silently trusted
r = say([{"intent": "save_listing", "listing": dict(type="plot", micro_market="Sector 57",
         size={"value": 500, "unit": "gaj"}, price={"value": 85, "unit": None, "basis": "per_gaj"})}])
assert "Not sure about: price" in txt(r), txt(r)
r = say([{"intent": "confirm"}])
assert "Confirmed" in txt(r)

# --- number grounding: a value the model copied from its context (not from the message) is flagged, never trusted
# (a) the message holds exactly one price expression -> deterministically repaired from the text
r = say([{"intent": "save_listing", "listing": dict(type="plot", micro_market="South City 2", size={"value": 400, "unit": "gaj"},
          price={"value": 12.8, "unit": "lakh", "basis": "per_gaj"})}],
        text="South City 2 plot 400 gaj, 3.2 lakh per gaj")
assert "12.8 cr total" in txt(r) and "3.2 lakh/gaj" in txt(r) and "Not sure" not in txt(r), txt(r)
# (b) not repairable (no price expression in the text) -> flagged for the broker to confirm
r = say([{"intent": "save_listing", "listing": dict(type="plot", micro_market="South City 2", size={"value": 400, "unit": "gaj"},
          price={"value": 5, "unit": "cr", "basis": "total"})}], text="South City 2 plot 400 gaj price negotiable")
assert "Not sure about: price" in txt(r), txt(r)
# (c) several prices in one broadcast -> never guess which one
raw = {"price": {"value": 99, "unit": "cr"}}
assert N.repair_price(raw, "1. 3BHK 7.2 cr 2. 4BHK 8.1 cr") is False and raw["price"]["value"] == 99
raw = {"price": {"value": 99, "unit": "cr"}}
assert N.repair_price(raw, "kothi 400 gaj 6 per gaj") is True and raw["price"] == {"value": 6.0, "unit": None, "basis": "per_gaj"}, raw
r = say([{"intent": "save_listing", "listing": dict(type="plot", micro_market="South City 2", size={"value": 410, "unit": "gaj"},
          price={"value": 3.2, "unit": "lakh", "basis": "per_gaj"})}], text="South City 2 plot 410 gaj, 3.2 lakh per gaj")
assert "Not sure" not in txt(r), txt(r)   # grounded numbers are not flagged

# --- broadcast split into several records
before = len(db.list_listings("yuvraj"))
say([{"intent": "save_listing", "listing": dict(type="builder_floor", micro_market="Sushant Lok 1", bedrooms=3,
                                                 floor_number=2, price={"value": 7.25, "unit": "cr"})},
     {"intent": "save_listing", "listing": dict(type="plot", micro_market="DLF Phase 3",
                                                 size={"value": 300, "unit": "gaj"},
                                                 price={"value": 5.5, "unit": "lakh", "basis": "per_gaj"})}])
assert len(db.list_listings("yuvraj")) == before + 2

# --- duplicate arriving from another source is merged, not doubled
n = len(db.list_listings("yuvraj"))
r = say([{"intent": "save_listing", "listing": dict(
    type="plot", micro_market="DLF Phase 5", size={"value": 250, "unit": "gaj"}, corner=True, facing="north",
    price={"value": 15, "unit": "cr"}, source="group: GGN Plots", owner_contact="98100 11111")}])
assert "Already had" in txt(r) and len(db.list_listings("yuvraj")) == n, txt(r)

# --- correction in plain language
floor = next(l for l in db.list_listings("yuvraj") if l["micro_market"] == "Sushant Lok 2" and l["type"] == "builder_floor"
             and l["purpose"] == "sale")
r = say([{"intent": "update_listing", "target_id": floor["id"], "updates": {"price": {"value": 6.1, "unit": "cr"}}}])
assert "6.1 cr" in txt(r)
assert any(h["event"] == "price_changed" for h in db.history("yuvraj", 20, "listing", floor["id"]))

# --- matching: must-have filters, ranking, reasons
r = say([{"intent": "search", "criteria": dict(
    type="builder_floor", micro_markets=["Sushant Lok"], bedrooms=3, budget_max={"value": 8, "unit": "cr"},
    must_haves={"park_view": True})}])
out = txt(r)
assert "match" in out and "park facing" in out and "7.5 cr" in out, out
assert "Sector 56" not in out and "rent" not in out
ids = [l for l in engine.db.get_session("yuvraj")["last_search"]["ids"]]
assert ids, "search should store results for follow-ups"
for m in M.find_matches(N.normalize_requirement(dict(
        type="builder_floor", micro_markets=["Sushant Lok"], budget_max={"value": 8, "unit": "cr"},
        must_haves={"park_view": True})), db.list_listings("yuvraj")):
    assert m["listing"].get("park_view") is not False and m["listing"]["price_total"] <= 8.4e7

# --- follow-up "show 2nd"
r = say([{"intent": "show", "result_index": 1, "what": "details"}])
assert "Price" in txt(r)

# --- reverse matching: new requirement is checked against existing inventory at once
r = say([{"intent": "save_requirement", "requirement": dict(
    client_name="Mr. Malhotra", type="plot", micro_markets=["DLF 2", "DLF 3"],
    size_min={"value": 300, "unit": "gaj"}, size_max={"value": 400, "unit": "gaj"},
    budget_max={"value": 18, "unit": "cr"})}])
assert "Saved requirement" in txt(r) and "plot" in txt(r) and "match" in txt(r), txt(r)

# --- proactive: new listing triggers alert for an open requirement
r = say([{"intent": "save_listing", "listing": dict(
    type="builder_floor", micro_market="Sushant Lok 1", bedrooms=4, floor_number=3, park_view=True,
    size={"value": 250, "unit": "gaj"}, price={"value": 7.6, "unit": "cr"}, facing="east")}])
assert "New listing" in txt(r) and "matches Mr. Sharma" in txt(r), txt(r)

# --- freshness
r = say([{"intent": "stale_check"}])
assert "not confirmed in 30+ days" in txt(r), txt(r)
stale_id = next(l["id"] for l in db.list_listings("yuvraj") if l["status"] == "stale")
say([{"intent": "set_status", "target_id": stale_id, "status": "available"}])
assert db.get_listing("yuvraj", stale_id)["status"] == "available"
assert M.days_since(db.get_listing("yuvraj", stale_id)["last_confirmed"]) == 0

# --- sold listings never match
sold = ids[0]
say([{"intent": "set_status", "target_id": sold, "status": "sold"}])
assert all(m["listing"]["id"] != sold for m in M.find_matches(
    N.normalize_requirement(dict(micro_markets=["Sushant Lok"])), db.list_listings("yuvraj")))

# --- broker isolation
assert all(l["broker_id"] == "anita" for l in db.list_listings("anita"))
assert db.get_listing("anita", floor["id"]) is None
r = say([{"intent": "update_listing", "target_id": floor["id"], "updates": {"facing": "east"}}], broker="anita")
assert "couldn't tell which listing" in txt(r)

print("all engine tests passed")
