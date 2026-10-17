"""Build text a broker can send to a CLIENT.

Deliberately leaves out everything confidential to the broker: owner name and contact (it would let the client
bypass the broker), price flexibility, private notes, sources and internal status. Only what a buyer needs to
judge the property goes out.
"""
import re
from typing import Optional

from . import db, matching as M, normalize as N

_NOT_FOR_CLIENTS = ("owner_name", "owner_contact", "price_flexibility", "notes", "source", "sources", "verified")


def client_safe(l: dict) -> str:
    rent = l.get("purpose") == "rent"
    lines = [f"{M.label(l)}"]
    if l.get("price_total"):
        price = N.fmt_money(l["price_total"], rent)
        if l.get("type") == "plot" and l.get("price_per_gaj") and not rent:
            price += f' ({N.fmt_per_gaj(l["price_per_gaj"])})'
        lines.append(f"Price: {price}")
    specs = []
    if l.get("super_sqft"):
        specs.append(f'{int(l["super_sqft"])} sq ft super area')
    if l.get("road_width_ft"):
        specs.append(f'{int(l["road_width_ft"])} ft road')
    if l.get("park_view"):
        specs.append("park facing")
    if l.get("corner") and l.get("type") != "plot":
        specs.append("corner")
    if l.get("lift"):
        specs.append("lift")
    for k in ("construction_grade", "furnishing"):
        if l.get(k):
            specs.append(l[k].replace("-", " ") + (" finish" if k == "construction_grade" else ""))
    if l.get("parking_type") and l["parking_type"] != "none":
        specs.append(f'{l["parking_type"]} parking')
    if l.get("parking_slots"):
        specs.append(f'{l["parking_slots"]} car park(s)')
    if specs:
        lines.append(" · ".join(specs))
    return "\n".join(lines)


def listings_text(broker_id: str, ids: list[int], intro: Optional[str] = None) -> str:
    """Client-safe message for listings this broker can see (own + team pool)."""
    ls = [l for l in (db.get_listing_any(broker_id, int(i)) for i in ids) if l]
    if not ls:
        return ""
    b = db.get_broker(broker_id) or {}
    team = (db.get_brokerage(b.get("brokerage_id")) or {}).get("name")
    head = intro or ("Hi! Here is a property that may suit you:" if len(ls) == 1
                     else f"Hi! Here are {len(ls)} options that may suit you:")
    body = "\n\n".join((f"{i}) " if len(ls) > 1 else "") + client_safe(l) for i, l in enumerate(ls, 1))
    sign = f"— {b.get('name', '').replace(' (demo)', '')}" + (f", {team}" if team else "")
    return f"{head}\n\n{body}\n\nReply here if you'd like to visit.\n{sign}"


def ids_in(text: str) -> list[int]:
    """Listing ids mentioned in a bot reply ("#5", "#15")."""
    seen: list[int] = []
    for m in re.findall(r"#(\d+)", text or ""):
        if int(m) not in seen:
            seen.append(int(m))
    return seen
