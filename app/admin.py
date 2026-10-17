"""Account-owner features: team overview, data export, adding brokers.

Prototype auth: the caller is identified by `broker_id` (the simulator's "acting as" selector).
Production: brokers are identified by their WhatsApp phone number (see whatsapp.py); the owner
signs in to a web admin with a real login (e.g. email OTP). Never trust a client-supplied broker_id.
"""
import csv
import io
import re

from . import db, matching as M

LISTING_COLS = ["broker", "id", "type", "purpose", "status", "micro_market", "society", "bedrooms", "size_gaj",
                "floor_number", "facing", "road_width_ft", "park_view", "corner", "price_total", "price_per_gaj",
                "price_flexibility", "owner_name", "owner_contact", "source", "shared", "last_confirmed", "notes"]


class Forbidden(Exception):
    pass


def require_owner(broker_id: str) -> dict:
    b = db.get_broker(broker_id)
    if not b or b.get("role") != "owner" or not b.get("brokerage_id"):
        raise Forbidden("Only the account owner can use the admin view.")
    return b


def overview(broker_id: str) -> dict:
    owner = require_owner(broker_id)
    members = []
    for m in db.brokers(owner["brokerage_id"]):
        ls = db.list_listings(m["id"])
        live = [l for l in ls if l["status"] in ("available", "under_negotiation", "stale")]
        fresh = [l for l in live if (M.days_since(l.get("last_confirmed")) or 0) <= 30]
        msgs = db.messages(m["id"], 1)
        members.append({
            "id": m["id"], "name": m["name"], "phone": m["phone"], "role": m["role"],
            "listings": len(ls), "shared": sum(1 for l in ls if l.get("shared")),
            "open_requirements": len(db.list_requirements(m["id"], only_open=True)),
            "fresh_pct": round(100 * len(fresh) / len(live)) if live else 100,
            "groups": len(db.list_groups(m["id"])),
            "last_activity": msgs[-1]["ts"] if msgs else None})
    return {"brokerage": db.get_brokerage(owner["brokerage_id"]), "members": members}


def export_rows(broker_id: str) -> dict:
    owner = require_owner(broker_id)
    out = {"listings": [], "requirements": [], "notes": [], "tasks": []}
    for m in db.brokers(owner["brokerage_id"]):
        for l in db.list_listings(m["id"]):
            out["listings"].append({**l, "broker": m["name"]})
        for r in db.list_requirements(m["id"]):
            out["requirements"].append({**r, "broker": m["name"]})
        for n in db.list_notes(m["id"]):
            n.pop("embedding", None)
            out["notes"].append({**n, "broker": m["name"]})
        for t in db.list_tasks(m["id"]):
            out["tasks"].append({**t, "broker": m["name"]})
    db.log(broker_id, "admin", None, "export",
           f'{len(out["listings"])} listings, {len(out["requirements"])} requirements')
    return out


def listings_csv(broker_id: str) -> str:
    rows = export_rows(broker_id)["listings"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=LISTING_COLS, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def add_broker(broker_id: str, name: str, phone: str) -> dict:
    owner = require_owner(broker_id)
    name, d = name.strip(), db.digits(phone)
    if not name or len(d) != 10:
        raise ValueError("Give a name and a 10-digit mobile number.")
    if db.broker_by_phone(phone):
        raise ValueError("That number is already registered.")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "broker"
    bid, n = slug, 2
    while db.get_broker(bid):
        bid, n = f"{slug}-{n}", n + 1
    db.add_broker(bid, name, owner["brokerage_id"], "broker", phone)
    db.log(broker_id, "admin", None, "broker_added", f"{name} {phone}")
    team = db.get_brokerage(owner["brokerage_id"])["name"]
    db.add_message(bid, "bot", f"Hi {name.split()[0]}! You've been added to {team}. "
                               "Your inventory is private to you unless you share it with the team.")
    return {"id": bid, "name": name}
