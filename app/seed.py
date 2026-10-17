"""Demo data. Goes through the same normaliser as live messages, so units/prices are consistent."""
from datetime import datetime, timedelta, timezone

from . import db, normalize as N

# (id, name, brokerage, role, phone). Rohit + Anita are one team; Vikram is a separate brokerage.
BROKERS = [("rohit", "Rohit Sharma (demo)", "sharma-realty", "owner", "+91 98100 00001"),
           ("anita", "Anita Verma (demo)", "sharma-realty", "broker", "+91 98100 00002"),
           ("vikram", "Vikram Singh (demo)", "singh-estates", "owner", "+91 98100 00003")]
BROKERAGES = [("sharma-realty", "Sharma Realty"), ("singh-estates", "Singh Estates")]


def ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def P(v, unit, basis="total"):
    return {"value": v, "unit": unit, "basis": basis}


ROHIT_LISTINGS = [
    # (days since confirmed, raw listing)
    (3, dict(type="plot", micro_market="DLF 1", size={"value": 250, "unit": "gaj"}, corner=True, facing="north",
             road_width_ft=24, price=P(6, "lakh", "per_gaj"), price_flexibility="firm", owner_name="Mr. Bhalla",
             owner_contact="98100 11111", source="owner", notes="Owner serious, no bargaining")),
    (50, dict(type="plot", micro_market="DLF Phase 2", size={"value": 300, "unit": "gaj"}, facing="east",
              road_width_ft=40, price=P(5.2, "lakh", "per_gaj"), price_flexibility="some", source="group: GGN Plots")),
    (10, dict(type="plot", micro_market="Sushant Lok 1", size={"value": 180, "unit": "gaj"}, facing="west",
              road_width_ft=30, price=P(4.5, "lakh", "per_gaj"), price_flexibility="some", owner_contact="98111 22222")),
    (5, dict(type="builder_floor", micro_market="Sushant Lok 1", bedrooms=3, floor_number=3,
             size={"value": 250, "unit": "gaj"}, park_view=True, facing="east", lift=True, construction_grade="premium",
             parking_type="stilt", price=P(7.5, "cr"), price_flexibility="firm", owner_name="Mrs. Khanna",
             owner_contact="98999 33333", notes="Power backup in building; owner firm", verified_fields=["park_view"])),
    (12, dict(type="builder_floor", micro_market="Sushant Lok 1", bedrooms=4, floor_number=2,
              size={"value": 300, "unit": "gaj"}, park_view=True, facing="north", lift=True, construction_grade="premium",
              price=P(7.9, "cr"), price_flexibility="some", owner_contact="98777 44444")),
    (8, dict(type="builder_floor", micro_market="Sushant Lok 2", bedrooms=3, floor_number=1,
             size={"value": 200, "unit": "gaj"}, park_view=False, facing="south", construction_grade="standard",
             price=P(6.4, "cr"), price_flexibility="high", notes="Owner relocating, will drop 3% if closed this week")),
    (20, dict(type="builder_floor", micro_market="Golf Course Road", bedrooms=3, floor_number=2,
              size={"value": 250, "unit": "gaj"}, park_view=True, facing="west", lift=True, construction_grade="premium",
              price=P(8.8, "cr"), price_flexibility="some")),
    (60, dict(type="builder_floor", micro_market="DLF Phase 3", bedrooms=4, floor_number=0,
              size={"value": 350, "unit": "gaj"}, facing="south", construction_grade="premium",
              price=P(9.5, "cr"), price_flexibility="firm")),
    (15, dict(type="apartment", micro_market="Golf Course Road", society="Crest Residency", tower_unit="Tower 4, 12B",
              bedrooms=3, super={"value": 2100, "unit": "sq ft"}, floor_number=12, park_view=True, parking_slots=2,
              furnishing="semi-furnished", price=P(6.8, "cr"), maintenance="₹18,000/month")),
    (30, dict(type="apartment", micro_market="Sector 56", society="Park Place", tower_unit="Tower B, 703",
              bedrooms=3, super={"value": 1850, "unit": "sq ft"}, floor_number=7, parking_slots=1,
              price=P(3.9, "cr"), price_flexibility="some")),
    (6, dict(type="apartment", purpose="rent", micro_market="Sector 54", society="Skyline Towers", bedrooms=2,
             super={"value": 1300, "unit": "sq ft"}, floor_number=9, furnishing="fully-furnished",
             price=P(85, "thousand", "per_month"))),
    (4, dict(type="builder_floor", purpose="rent", micro_market="Sushant Lok 1", bedrooms=3, floor_number=1,
             furnishing="semi-furnished", park_view=True, price=P(1.6, "lakh", "per_month"))),
    (45, dict(type="plot", micro_market="South City 1", size={"value": 400, "unit": "gaj"}, vacant_or_built="built",
              facing="east", price=P(3.2, "lakh", "per_gaj"), price_flexibility="high",
              notes="Old kothi; owner needs urgent money")),
]

ANITA_LISTINGS = [
    (2, dict(shared=True, type="plot", micro_market="DLF Phase 4", size={"value": 500, "unit": "gaj"}, facing="north", corner=True,
             price=P(5.8, "lakh", "per_gaj"), owner_contact="97000 55555")),
    (9, dict(shared=True, type="builder_floor", micro_market="Sushant Lok 1", bedrooms=4, floor_number=1,
             size={"value": 280, "unit": "gaj"}, park_view=True, price=P(7.7, "cr"), owner_contact="97000 66666")),
    (7, dict(type="builder_floor", micro_market="Sushant Lok 2", bedrooms=3, floor_number=2,
             size={"value": 220, "unit": "gaj"}, price=P(6.2, "cr"), notes="PRIVATE: owner is a friend, off-market")),
]

VIKRAM_LISTINGS = [
    (4, dict(shared=True, type="builder_floor", micro_market="Sushant Lok 1", bedrooms=4, floor_number=3,
             size={"value": 300, "unit": "gaj"}, park_view=True, price=P(7.4, "cr"))),
]

ROHIT_REQS = [
    dict(client_name="Mr. Sharma", type="builder_floor", micro_markets=["Sushant Lok"], bedrooms=4,
         budget_min=P(7, "cr"), budget_max=P(8, "cr"), must_haves={"park_view": True},
         nice_to_haves={"facing": ["east"]}, motivation="upgrading from apartment", timeline="2 months"),
    dict(client_name="Mr. Gupta", type="plot", micro_markets=["DLF Phase 1", "DLF Phase 2"],
         size_min={"value": 250, "unit": "gaj"}, size_max={"value": 300, "unit": "gaj"},
         budget_max=P(16, "cr"), nice_to_haves={"corner": True}, decision_maker="self + brother"),
    dict(client_name="Mr. Kapoor", type="builder_floor", micro_markets=["Golf Course Road"], bedrooms=3,
         budget_max=P(9, "cr"), must_haves={"construction_grade": ["premium"]}),
]


def _add(b: str, items: list) -> list:
    ids: list = []
    for days, raw in items:
        ch, low, ver = N.normalize_listing(raw)
        ch.setdefault("purpose", "sale")
        ch["verified"] = {f: True for f in ver}
        if raw.get("shared"):
            ch["shared"] = True
        ch["low_confidence"], ch["asked"] = low, []
        if ch.get("source"):
            ch["sources"] = [ch["source"]]
        status = "stale" if days > 30 else "available"
        ch["created_at"] = ago(days + 14)
        lid = db.add_listing(b, ch, status=status, last_confirmed=ago(days))
        db.log(b, "listing", lid, "created", "seed")
        ids.append(lid)
    return ids


def run(reset: bool = True) -> None:
    db.init()
    if reset:
        db.wipe()
    for bid, name in BROKERAGES:
        db.add_brokerage(bid, name)
    for bid, name, brokerage, role, phone in BROKERS:
        db.add_broker(bid, name, brokerage, role, phone)
    ids = _add("rohit", ROHIT_LISTINGS)
    _add("anita", ANITA_LISTINGS)
    _add("vikram", VIKRAM_LISTINGS)
    rids = []
    for raw in ROHIT_REQS:
        rid = db.add_requirement("rohit", N.normalize_requirement(raw))
        db.log("rohit", "requirement", rid, "created", "seed")
        rids.append(rid)
    # past notes + a follow-up due today, so recall / reminders / EOD can be demoed immediately
    (l_plot, l_sl1, l_gcr) = ids[0], ids[3], ids[6]
    r_gupta, r_kapoor = rids[1], rids[2]
    for days, kind, text, lid, rid, client in [
        (9, "call", "Owner of the Golf Course Road floor said price is negotiable by about 2% only if a token is paid this week.", l_gcr, None, None),
        (3, "visit", "Visited the Golf Course Road floor with Mr. Kapoor. He liked the kitchen and layout but wants a lower price.", l_gcr, r_kapoor, "Mr. Kapoor"),
        (6, "call", "Mrs. Khanna (Sushant Lok floor owner) will not go below 7.4 cr; building has 100% power backup.", l_sl1, None, None),
        (5, "discussion", "Mr. Gupta liked the DLF Phase 1 corner plot; his brother insists on a vastu-compliant north-facing plot.", l_plot, r_gupta, "Mr. Gupta"),
        (8, "call", "Mr. Bhalla (DLF Phase 1 plot owner) wants serious buyers only and would give one week exclusivity.", l_plot, None, None),
    ]:
        vec = None
        try:
            from . import phase3
            out = phase3.embed_fn([text])
            vec = out[0] if out else None
        except Exception:
            pass
        db.add_note("rohit", kind, text, lid, rid, client, vec, ts=ago(days))
    db.add_task("rohit", "Call the Golf Course Road floor owner about a 2% discount", datetime.now(timezone.utc).date().isoformat(), l_gcr, r_kapoor)
    db.add_message("rohit", "bot", "Hi Rohit! Forward me any listing (text, photo, PDF or voice note) or a client "
                   "requirement. Try: “find me a 3 BHK floor in Sushant Lok under 8 cr, park facing”.")
    db.add_message("anita", "bot", "Hi Anita! Your inventory is private unless you share it. Rohit is on your team; "
                   "say “share #14 with the team” to put a listing in the team pool.")
    db.add_message("vikram", "bot", "Hi Vikram! You're in a different brokerage — nothing from Sharma Realty is visible here.")
    db.update_settings("anita", enabled=False)
    db.update_settings("vikram", enabled=False)
    for g in ("GGN Plots & Floors", "Sushant Lok Brokers"):
        db.set_group_muted("rohit", g, False)
