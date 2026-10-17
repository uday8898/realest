"""Matching engine: hard filters -> must-haves -> nice-to-have ranking. Pure code, no LLM,
so results are fast (well under the 10 s target) and every ranking is explainable."""
from datetime import datetime, timezone
from typing import Optional

from . import normalize as N

EXCLUDED = {"sold", "rented", "on_hold"}
BUDGET_TOLERANCE = 0.05
SIZE_TOLERANCE = 0.10

PREF_LABELS = {
    "park_view": "park facing", "corner": "corner", "lift": "lift",
    "plot_share_included": "plot share included",
}


def days_since(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - d).days


def label(l: dict) -> str:
    t, bits = l.get("type"), []
    mm = l.get("micro_market") or "location?"
    if t == "plot":
        bits.append(f'{_g(l.get("size_gaj"))} gaj' + (" corner" if l.get("corner") else "") + " plot")
    elif t == "builder_floor":
        bits.append(f'{l.get("bedrooms") or "?"} BHK builder floor')
        if l.get("floor_number") is not None:
            bits.append(N.ordinal(l["floor_number"]))
        if l.get("size_gaj"):
            bits.append(f'{_g(l["size_gaj"])} gaj plot')
    elif t == "apartment":
        bits.append(f'{l.get("bedrooms") or "?"} BHK apartment')
        if l.get("society"):
            bits.append(l["society"])
        if l.get("tower_unit"):
            bits.append(l["tower_unit"])
    else:
        bits.append("property")
    bits.append(mm)
    if l.get("facing"):
        bits.append(f'{l["facing"]}-facing')
    if l.get("park_view"):
        bits.append("park facing")
    if l.get("purpose") == "rent":
        bits.append("for rent")
    return ", ".join(bits)


def title(l: dict) -> str:
    """Short headline for a list row: what the property is."""
    t = l.get("type")
    if t == "plot":
        return f'{_g(l.get("size_gaj"))} gaj plot' + (" (corner)" if l.get("corner") else "")
    if t == "builder_floor":
        return f'{l.get("bedrooms") or "?"} BHK builder floor'
    if t == "apartment":
        return f'{l.get("bedrooms") or "?"} BHK apartment'
    return "Property"


def facts(l: dict) -> list[str]:
    """Key facts for the second line of a row, most useful first."""
    out: list[str] = []
    if l.get("society"):
        out.append(l["society"])
    if l.get("floor_number") is not None:
        out.append(N.ordinal(l["floor_number"]))
    if l.get("type") == "builder_floor" and l.get("size_gaj"):
        out.append(f'{_g(l["size_gaj"])} gaj plot')
    if l.get("super_sqft"):
        out.append(f'{int(l["super_sqft"])} sq ft')
    if l.get("facing"):
        out.append(f'{l["facing"]}-facing')
    if l.get("road_width_ft") and l.get("type") == "plot":
        out.append(f'{int(l["road_width_ft"])} ft road')
    if l.get("park_view"):
        out.append("park facing")
    if l.get("vacant_or_built") == "built":
        out.append("built")
    if l.get("construction_grade"):
        out.append(l["construction_grade"])
    return out


def _g(v) -> str:
    return "?" if v is None else (str(int(v)) if float(v).is_integer() else str(v))


def _check(l: dict, key: str, want) -> Optional[bool]:
    """True = satisfied, False = definitely not, None = listing doesn't say."""
    if key == "facing":
        have = l.get("facing")
        return None if have is None else have in want
    if key in ("park_view", "corner", "lift", "plot_share_included"):
        have = l.get(key)
        return None if have is None else have == want
    if key in ("construction_grade", "furnishing", "parking_type"):
        have = l.get(key)
        return None if have is None else have in want
    if key in ("shape", "vacant_or_built"):
        have = l.get(key)
        return None if have is None else have == want
    if key == "min_road_width_ft":
        have = l.get("road_width_ft")
        return None if have is None else have >= want
    if key in ("min_floor", "max_floor"):
        have = l.get("floor_number")
        if have is None:
            return None
        return have >= want if key == "min_floor" else have <= want
    if key == "bedrooms":
        have = l.get("bedrooms")
        return None if have is None else have == want
    return None


def _pref_label(key: str, want) -> str:
    if key == "facing":
        return "/".join(want) + "-facing"
    if key in PREF_LABELS:
        return PREF_LABELS[key] if want else "not " + PREF_LABELS[key]
    if key in ("construction_grade", "furnishing", "parking_type"):
        return "/".join(want)
    if key == "min_road_width_ft":
        return f"road ≥ {int(want)} ft"
    if key == "min_floor":
        return f"floor ≥ {int(want)}"
    if key == "max_floor":
        return f"floor ≤ {int(want)}"
    if key == "bedrooms":
        return f"{int(want)} BHK"
    return f"{key.replace('_', ' ')}: {want}"


def match_one(req: dict, l: dict) -> Optional[dict]:
    """Return a scored match dict, or None if the listing fails a hard requirement."""
    if l.get("status") in EXCLUDED:
        return None
    if req.get("type") and l.get("type") != req["type"]:
        return None
    if (req.get("purpose") or "sale") != (l.get("purpose") or "sale"):
        return None
    matches: list[str] = []
    lacks: list[str] = []
    score = 100.0

    mms = req.get("micro_markets") or []
    if mms:
        if not any(N.market_match(m, l.get("micro_market")) for m in mms):
            return None
        matches.append(l.get("micro_market") or "")

    price = l.get("price_total")
    rent = l.get("purpose") == "rent"
    bmax, bmin = req.get("budget_max"), req.get("budget_min")
    if bmax:
        if price is None:
            lacks.append("price not confirmed")
            score -= 10
        elif price > bmax * (1 + BUDGET_TOLERANCE):
            return None
        elif price > bmax:
            lacks.append(f"slightly over budget ({N.fmt_money(price, rent)})")
            score -= 8
        else:
            matches.append(f"{N.fmt_money(price, rent)}, in budget")
            score += 5 * (price / bmax)  # reward using the budget well
    if bmin and price and price < bmin * 0.6:
        return None

    size = l.get("size_gaj")
    smin, smax = req.get("size_min_gaj"), req.get("size_max_gaj")
    if smin or smax:
        if size is None:
            lacks.append("size not confirmed")
            score -= 5
        elif (smin and size < smin * (1 - SIZE_TOLERANCE)) or (smax and size > smax * (1 + SIZE_TOLERANCE)):
            return None
        else:
            matches.append(f"{_g(size)} gaj")

    if req.get("bedrooms"):
        if l.get("bedrooms") is None:
            lacks.append("BHK not confirmed")
            score -= 5
        elif l["bedrooms"] != req["bedrooms"]:
            return None
        else:
            matches.append(f'{l["bedrooms"]} BHK')

    for k, want in (req.get("must_haves") or {}).items():
        r = _check(l, k, want)
        if r is False:
            return None
        if r is None:
            lacks.append(f"{_pref_label(k, want)} unconfirmed")
            score -= 6
        else:
            matches.append(_pref_label(k, want))
            score += 4
    for k, want in (req.get("nice_to_haves") or {}).items():
        r = _check(l, k, want)
        if r is True:
            matches.append(_pref_label(k, want))
            score += 10
        elif r is False:
            lacks.append(f"not {_pref_label(k, want)}")
            score -= 2
        else:
            lacks.append(f"{_pref_label(k, want)} unknown")

    age = days_since(l.get("last_confirmed"))
    if l.get("status") == "stale" or (age is not None and age > 30):
        lacks.append(f"not confirmed for {age} days — verify it's still available")
        score -= 12
    if l.get("status") == "under_negotiation":
        lacks.append("under negotiation")
        score -= 6
    flex = l.get("price_flexibility")
    if flex:
        matches.append(f"owner {flex}")
    return {"listing": l, "score": round(score, 1), "matches": [m for m in matches if m], "lacks": lacks}


def find_matches(req: dict, listings: list[dict], limit: int = 5) -> list[dict]:
    out = [m for m in (match_one(req, l) for l in listings) if m]
    out.sort(key=lambda m: -m["score"])
    return out[:limit]


def req_label(r: dict) -> str:
    bits = []
    if r.get("bedrooms"):
        bits.append(f'{r["bedrooms"]} BHK')
    bits.append((r.get("type") or "property").replace("_", " "))
    if r.get("micro_markets"):
        bits.append("in " + "/".join(r["micro_markets"]))
    rent = r.get("purpose") == "rent"
    if r.get("budget_min") and r.get("budget_max"):
        bits.append(f'{N.fmt_money(r["budget_min"], rent)}–{N.fmt_money(r["budget_max"], rent)}')
    elif r.get("budget_max"):
        bits.append(f'under {N.fmt_money(r["budget_max"], rent)}')
    mh = [_pref_label(k, v) for k, v in (r.get("must_haves") or {}).items()]
    if mh:
        bits.append("must: " + ", ".join(mh))
    return " ".join(bits[:3]) + (", " + ", ".join(bits[3:]) if bits[3:] else "")
