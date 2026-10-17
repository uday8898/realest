"""Deterministic normalisation: units, prices, enums, micro-markets.

The LLM only *extracts what was said*. All arithmetic and vocabulary mapping happens here,
so that a wrong price or unit can't come from model maths.
"""
import re
from difflib import get_close_matches
from typing import Any, Optional

from . import vocab

AREA_TO_GAJ = {
    "gaj": 1.0, "sq yd": 1.0, "sqyd": 1.0, "sq yards": 1.0, "yard": 1.0, "yards": 1.0, "syd": 1.0,
    "sq ft": 1 / 9, "sqft": 1 / 9, "sq feet": 1 / 9, "sft": 1 / 9,
    "sq m": 1.19599, "sqm": 1.19599, "sq mtr": 1.19599, "marla": 30.25, "kanal": 605.0,
}
MONEY_MULT = {
    "cr": 1e7, "crore": 1e7, "crores": 1e7, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5,
    "l": 1e5, "thousand": 1e3, "k": 1e3, "rupees": 1.0, "rs": 1.0, "inr": 1.0,
}


def _num(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def _unit(u) -> Optional[str]:
    return str(u).lower().strip().replace(".", "") if u else None


def area_to_gaj(a: Any) -> Optional[float]:
    if a is None:
        return None
    if isinstance(a, dict):
        v, u = _num(a.get("value")), _unit(a.get("unit")) or "gaj"
    else:
        v, u = _num(a), "gaj"
    if v is None:
        return None
    return round(v * AREA_TO_GAJ.get(u, 1.0), 2)


def area_to_sqft(a: Any) -> Optional[float]:
    if a is None:
        return None
    if isinstance(a, dict):
        v, u = _num(a.get("value")), _unit(a.get("unit")) or "sq ft"
    else:
        v, u = _num(a), "sq ft"
    if v is None:
        return None
    return round(v * AREA_TO_GAJ.get(u, 1 / 9) * 9, 1)


def money_to_rupees(m: Any) -> Optional[float]:
    """{"value": 7, "unit": "cr"} -> 70000000. Used for budgets."""
    if m is None:
        return None
    if not isinstance(m, dict):
        v = _num(m)
        return v
    v, u = _num(m.get("value")), _unit(m.get("unit"))
    if v is None:
        return None
    if u in MONEY_MULT:
        return v * MONEY_MULT[u]
    return v * (1e7 if v < 200 else 1.0)  # bare "8" in a budget means crore


def price(p: Any, size_gaj: Optional[float] = None, area_sqft: Optional[float] = None,
          purpose: str = "sale") -> tuple[Optional[float], Optional[float], list[str]]:
    """Return (total_rupees, per_gaj_rupees, warnings)."""
    warn: list[str] = []
    if not isinstance(p, dict):
        return None, None, warn
    v, u, basis = _num(p.get("value")), _unit(p.get("unit")), (p.get("basis") or "").lower() or None
    if v is None:
        return None, None, warn
    if purpose == "rent" or basis == "per_month":
        mult = MONEY_MULT.get(u) or (1e5 if v < 20 else 1.0)
        return v * mult, None, warn
    if u not in MONEY_MULT:
        if basis == "per_gaj":
            u = "lakh" if v < 1000 else "rupees"
        elif basis == "per_sqft":
            u = "rupees"
        else:
            u = "cr" if v < 200 else "rupees"
            warn.append("price_total")  # unit was assumed
    mult = MONEY_MULT[u]
    total = per_gaj = None
    if basis == "per_gaj":
        per_gaj = v * mult
        total = per_gaj * size_gaj if size_gaj else None
    elif basis == "per_sqft":
        total = v * mult * area_sqft if area_sqft else (v * mult * size_gaj * 9 if size_gaj else None)
        per_gaj = v * mult * 9
    else:
        total = v * mult
        per_gaj = total / size_gaj if size_gaj else None
    return total, per_gaj, warn


def canon_market(name: Optional[str]) -> Optional[str]:
    if not name or not str(name).strip():
        return None
    s = re.sub(r"\s+", " ", str(name).strip())
    low = s.lower()
    m = re.match(r"dlf\s*(?:ph(?:ase)?\.?)?\s*-?\s*([1-5])\b", low)
    if m:
        return f"DLF Phase {m.group(1)}"
    m = re.match(r"sushant\s*lok\s*-?\s*(?:ph(?:ase)?\.?)?\s*([1-3])?\b", low)
    if m:
        return f"Sushant Lok {m.group(1)}" if m.group(1) else "Sushant Lok"
    m = re.match(r"sec(?:tor)?\.?\s*-?\s*(\d+)\b", low)
    if m:
        return f"Sector {m.group(1)}"
    if low in ("gcr", "golf course rd", "golf course road"):
        return "Golf Course Road"
    if low in ("gcer", "golf course extension", "golf course ext road", "golf course extn road"):
        return "Golf Course Extension Road"
    hit = get_close_matches(s.title(), vocab.MICRO_MARKETS, n=1, cutoff=0.82)
    return hit[0] if hit else s.title()


def market_match(want: str, have: Optional[str]) -> bool:
    if not want or not have:
        return False
    w, h = want.lower().split(), have.lower().split()
    return h[: len(w)] == w or w[: len(h)] == h


def norm_enum(field: str, val: Any) -> Optional[str]:
    if val is None:
        return None
    s = re.sub(r"[_\s]+", " ", str(val).strip().lower())
    s = s.replace("-", " ") if field == "facing" and s not in vocab.FACING else s
    syn = vocab.ENUM_SYNONYMS.get(field, {})
    s = syn.get(s, s)
    canon = {c.replace("_", " ").replace("-", " "): c for c in vocab.ENUMS[field]}
    key = s.replace("_", " ").replace("-", " ")
    return canon.get(key)


def to_bool(v) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("y", "yes", "true", "1", "haan"):
        return True
    if s in ("n", "no", "false", "0", "nahi"):
        return False
    return None


def _int(v) -> Optional[int]:
    n = _num(v)
    return int(n) if n is not None else None


def normalize_listing(raw: dict, base: Optional[dict] = None) -> tuple[dict, list[str], list[str]]:
    """Return (changes, low_confidence_fields, verified_fields).

    `changes` only holds fields present in `raw` (so it works for both create and update).
    `base` is the existing record (for context such as plot size when only price changes).
    """
    base = base or {}
    ch: dict[str, Any] = {}
    for f in ("type", "purpose", "facing", "shape", "construction_grade", "furnishing",
              "parking_type", "vacant_or_built", "price_flexibility"):
        if raw.get(f) is not None:
            v = norm_enum(f, raw[f])
            if v:
                ch[f] = v
    if raw.get("micro_market"):
        ch["micro_market"] = canon_market(raw["micro_market"])
    for f in ("address", "owner_name", "owner_contact", "society", "tower_unit", "dimensions",
              "source", "maintenance"):
        if raw.get(f):
            ch[f] = str(raw[f]).strip()
    for f in ("park_view", "corner", "lift", "plot_share_included"):
        b = to_bool(raw.get(f))
        if b is not None:
            ch[f] = b
    for f in ("floor_number", "bedrooms", "parking_slots"):
        n = _int(raw.get(f))
        if n is not None:
            ch[f] = n
    if raw.get("bedrooms") is None and raw.get("bhk") is not None and _int(raw.get("bhk")) is not None:
        ch["bedrooms"] = _int(raw["bhk"])
    rw = _num(raw.get("road_width_ft"))
    if rw is not None:
        ch["road_width_ft"] = rw
    if raw.get("size") is not None:
        g = area_to_gaj(raw["size"])
        if g:
            ch["size_gaj"] = g
    if raw.get("carpet") is not None:
        ch["carpet_sqft"] = area_to_sqft(raw["carpet"])
    if raw.get("super") is not None:
        ch["super_sqft"] = area_to_sqft(raw["super"])
    if raw.get("notes"):
        old = base.get("notes")
        new = str(raw["notes"]).strip()
        ch["notes"] = f"{old}; {new}" if old and new not in old else new

    low = [k for k, c in (raw.get("confidence") or {}).items()
           if isinstance(c, (int, float)) and c < 0.7]

    if raw.get("price") is not None:
        size = ch.get("size_gaj") or base.get("size_gaj")
        area = ch.get("super_sqft") or base.get("super_sqft")
        purpose = ch.get("purpose") or base.get("purpose") or "sale"
        total, per_gaj, warn = price(raw["price"], size, area, purpose)
        if total is not None:
            ch["price_total"] = round(total)
        if per_gaj is not None:
            ch["price_per_gaj"] = round(per_gaj)
        if isinstance(raw["price"], dict):
            ch["price_said"] = (f'{raw["price"].get("value")} {raw["price"].get("unit") or ""} '
                                f'{(raw["price"].get("basis") or "").replace("_", " ")}').strip()
        low += warn

    # sanity guard: implausible prices are flagged, never silently stored as fact
    typ = ch.get("type") or base.get("type")
    purpose = ch.get("purpose") or base.get("purpose") or "sale"
    pt, pg = ch.get("price_total"), ch.get("price_per_gaj")
    if purpose == "sale":
        if typ == "plot" and pg and not (50_000 <= pg <= 6_000_000):
            low.append("price_total")
        if typ in ("builder_floor", "apartment") and pt and not (3_000_000 <= pt <= 600_000_000):
            low.append("price_total")
    elif pt and not (5_000 <= pt <= 3_000_000):
        low.append("price_total")

    # map confidence keys onto stored field names
    alias = {"price": "price_total", "size": "size_gaj", "super": "super_sqft", "carpet": "carpet_sqft"}
    low = sorted({alias.get(k, k) for k in low if alias.get(k, k) in ch or k == "price_total"})
    ver = [alias.get(k, k) for k in (raw.get("verified_fields") or [])]
    return ch, low, ver


def normalize_requirement(raw: dict) -> dict:
    r: dict[str, Any] = {}
    for f in ("type", "purpose"):
        v = norm_enum(f, raw.get(f)) if raw.get(f) else None
        if v:
            r[f] = v
    r.setdefault("purpose", "sale")
    r["client_name"] = (raw.get("client_name") or "").strip() or None
    mm = raw.get("micro_markets") or ([raw["micro_market"]] if raw.get("micro_market") else [])
    r["micro_markets"] = [canon_market(m) for m in mm if m]
    r["budget_min"] = money_to_rupees(raw.get("budget_min"))
    r["budget_max"] = money_to_rupees(raw.get("budget_max"))
    r["size_min_gaj"] = area_to_gaj(raw.get("size_min"))
    r["size_max_gaj"] = area_to_gaj(raw.get("size_max"))
    r["bedrooms"] = _int(raw.get("bedrooms"))
    r["must_haves"] = _norm_prefs(raw.get("must_haves") or {})
    r["nice_to_haves"] = _norm_prefs(raw.get("nice_to_haves") or {})
    for f in ("timeline", "decision_maker", "motivation", "notes"):
        r[f] = (str(raw[f]).strip() if raw.get(f) else None)
    return r


def _norm_prefs(p: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in p.items():
        if v is None:
            continue
        if k in ("facing", "construction_grade", "furnishing", "parking_type"):
            vals = v if isinstance(v, list) else [v]
            vals = [x for x in (norm_enum(k, i) for i in vals) if x]
            if vals:
                out[k] = vals
        elif k in ("shape", "vacant_or_built"):
            x = norm_enum(k, v)
            if x:
                out[k] = x
        elif k in ("park_view", "corner", "lift", "plot_share_included"):
            b = to_bool(v)
            if b is not None:
                out[k] = b
        elif k in ("min_road_width_ft", "min_floor", "max_floor", "bedrooms"):
            n = _num(v)
            if n is not None:
                out[k] = n
    return out


def _numbers_in(text: str) -> set:
    out = set()
    for m in re.findall(r"\d[\d,]*\.?\d*", text or ""):
        try:
            out.add(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def ungrounded(raw: dict, text: str) -> list[str]:
    """Fields whose number does not appear anywhere in the broker's message. The model can copy a number from
    its context (e.g. an existing listing) instead of the message; such values must never be trusted silently."""
    nums, out = _numbers_in(text), []
    for key, field in (("price", "price_total"), ("size", "size_gaj")):
        v = _num((raw.get(key) or {}).get("value")) if isinstance(raw.get(key), dict) else None
        if v is not None and v not in nums:
            out.append(field)
    return out


_PRICE_TOKEN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(cr(?:ore)?s?|lakhs?|lacs?|l|k)?\s*(?:(?:/|per|a)\s*(gaj|gz|sq\.?\s?yd|yards?))?(?![a-z0-9])", re.I)


def repair_price(raw: dict, text: str) -> bool:
    """If the model's price number is not in the message (it copied or multiplied something) but the message holds
    exactly ONE price expression ("15 cr", "3.2 lakh per gaj", "6 per gaj"), use that instead. Returns True if repaired.
    Messages with several prices (broadcasts) are left alone: they stay flagged for the broker to confirm."""
    p = raw.get("price")
    if not text or not isinstance(p, dict) or _num(p.get("value")) is None or _num(p.get("value")) in _numbers_in(text):
        return False
    found = []
    for m in _PRICE_TOKEN.finditer(text):
        num, unit, per = m.group(1), (m.group(2) or "").lower(), m.group(3)
        if not unit and not per:
            continue
        u = {"k": "thousand", "l": "lakh"}.get(unit, "cr" if unit.startswith("cr") else "lakh" if unit else None)
        found.append({"value": float(num), "unit": u, "basis": "per_gaj" if per else "total"})
    if len(found) != 1:
        return False
    raw["price"] = found[0]
    return True


# ---------- formatting ----------
def fmt_money(r: Optional[float], rent: bool = False) -> str:
    if r is None:
        return "?"
    if rent:
        return f"₹{int(r):,}/month"
    if r >= 1e7:
        return f"{r / 1e7:.2f}".rstrip("0").rstrip(".") + " cr"
    if r >= 1e5:
        return f"{r / 1e5:.2f}".rstrip("0").rstrip(".") + " lakh"
    return f"₹{int(r):,}"


def fmt_per_gaj(r: Optional[float]) -> str:
    if r is None:
        return "?"
    if r >= 1e5:
        return f"{r / 1e5:.2f}".rstrip("0").rstrip(".") + " lakh/gaj"
    return f"₹{int(r):,}/gaj"


def ordinal(n: int) -> str:
    if n == 0:
        return "ground floor"
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf} floor"
