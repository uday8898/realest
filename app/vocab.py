"""Controlled vocabularies and the Gurgaon real-estate glossary."""

TYPES = ["plot", "builder_floor", "apartment"]
PURPOSES = ["sale", "rent"]
FACING = ["north", "south", "east", "west", "north-east", "north-west", "south-east", "south-west"]
SHAPES = ["regular", "irregular"]
GRADES = ["premium", "standard", "old"]
FURNISHING = ["unfurnished", "semi-furnished", "fully-furnished"]
PARKING = ["stilt", "covered", "open", "basement", "none"]
BUILT = ["vacant", "built"]
STATUSES = ["available", "under_negotiation", "sold", "rented", "on_hold", "stale"]
FLEXIBILITY = ["firm", "some", "high"]

MICRO_MARKETS = [
    "DLF Phase 1", "DLF Phase 2", "DLF Phase 3", "DLF Phase 4", "DLF Phase 5",
    "Sushant Lok 1", "Sushant Lok 2", "Sushant Lok 3",
    "Golf Course Road", "Golf Course Extension Road", "South City 1", "South City 2",
    "Nirvana Country", "Palam Vihar", "Malibu Towne", "Ardee City", "Suncity",
    "Sector 54", "Sector 55", "Sector 56", "Sector 57", "Sector 43", "Sector 28",
    "Sector 15", "Sector 14", "Sohna Road", "New Gurgaon", "MG Road",
]

# Enum synonyms -> canonical value
ENUM_SYNONYMS = {
    "facing": {
        "n": "north", "s": "south", "e": "east", "w": "west",
        "ne": "north-east", "nw": "north-west", "se": "south-east", "sw": "south-west",
        "north east": "north-east", "north west": "north-west",
        "south east": "south-east", "south west": "south-west",
        "northeast": "north-east", "northwest": "north-west",
        "southeast": "south-east", "southwest": "south-west",
    },
    "construction_grade": {"new": "premium", "luxury": "premium", "high": "premium",
                           "good": "standard", "average": "standard", "medium": "standard",
                           "purani": "old", "renovation": "old", "kothi": "old"},
    "furnishing": {"semi furnished": "semi-furnished", "semifurnished": "semi-furnished",
                   "fully furnished": "fully-furnished", "furnished": "fully-furnished",
                   "full furnished": "fully-furnished", "unfurnished": "unfurnished",
                   "bare": "unfurnished", "bare shell": "unfurnished"},
    "parking_type": {"stilt parking": "stilt", "covered parking": "covered",
                     "open parking": "open", "no parking": "none"},
    "shape": {"rectangular": "regular", "square": "regular", "odd": "irregular"},
    "vacant_or_built": {"khali": "vacant", "empty": "vacant", "constructed": "built",
                        "house": "built", "kothi": "built"},
    "price_flexibility": {"fixed": "firm", "serious": "firm", "negotiable": "some",
                          "medium": "some", "low": "firm", "flexible": "high", "urgent": "high"},
    "status": {"under negotiation": "under_negotiation", "negotiation": "under_negotiation",
               "on hold": "on_hold", "hold": "on_hold", "booked": "under_negotiation",
               "leased": "rented"},
}

ENUMS = {
    "type": TYPES, "purpose": PURPOSES, "facing": FACING, "shape": SHAPES,
    "construction_grade": GRADES, "furnishing": FURNISHING, "parking_type": PARKING,
    "vacant_or_built": BUILT, "status": STATUSES, "price_flexibility": FLEXIBILITY,
}

# Fields the bot asks about when missing (priority order), per type and purpose
REQUIRED_FIELDS = {
    "plot": ["micro_market", "size_gaj", "price_total", "facing", "road_width_ft", "owner_contact"],
    "builder_floor": ["micro_market", "bedrooms", "floor_number", "price_total", "facing",
                      "park_view", "owner_contact"],
    "apartment": ["micro_market", "society", "bedrooms", "super_sqft", "floor_number",
                  "price_total", "owner_contact"],
}

FIELD_LABELS = {
    "micro_market": "micro-market", "size_gaj": "plot size", "price_total": "price",
    "facing": "facing", "road_width_ft": "road width", "owner_contact": "owner contact",
    "bedrooms": "BHK", "floor_number": "floor", "park_view": "park view",
    "society": "society", "super_sqft": "super area", "corner": "corner",
    "lift": "lift", "construction_grade": "construction grade", "furnishing": "furnishing",
    "parking_type": "parking", "shape": "plot shape", "vacant_or_built": "vacant/built",
    "plot_share_included": "plot share", "price_flexibility": "owner flexibility",
    "type": "property type", "purpose": "sale/rent", "address": "address",
    "carpet_sqft": "carpet area", "owner_name": "owner name", "maintenance": "maintenance",
}

GLOSSARY = """
UNITS & PRICE
- gaj = sq yd (1 gaj = 9 sq ft). marla = 30.25 gaj. kanal = 605 gaj.
- cr / crore = 1,00,00,000 rupees. L / lac / lakh = 1,00,000. k = thousand.
- Brokers write price as a total ("15 cr", "1500 L"), per gaj ("6 per gaj", "6L/gaj") or per sq ft.
  A bare "6 per gaj" on a plot in DLF/Sushant Lok/Golf Course Road means 6 LAKH per gaj.
  Report what was said: price = {"value": 6, "unit": "lakh", "basis": "per_gaj"}. NEVER do arithmetic yourself.
  Example: "400 gaj ... 3.2 lakh per gaj" -> price {"value":3.2,"unit":"lakh","basis":"per_gaj"} (do NOT multiply by the size).
  Every number you output must be copied from THIS message, never from the CONTEXT block.
- For rent, price is per month (basis "per_month"), e.g. "1.2 L rent" -> {"value":1.2,"unit":"lakh","basis":"per_month"}.
FLOORS
- GF = ground floor (floor_number 0). FF / 1st = 1, SF / 2nd = 2, TF / 3rd = 3, 4th = 4.
- "stilt" is a parking level, not a floor: set parking_type "stilt". "Stilt + 4" builder floors are common.
- "3 BHK floor", "3BR" -> bedrooms 3.
VIEW / FACING (these mean the same thing; map them all)
- "park facing", "park view", "garden view", "garden facing", "open from front", "green view" -> park_view true.
- "north facing", "NE facing" -> facing (entrance direction) north / north-east.
- "corner wala", "corner plot", "2 side open", "two side open", "corner" -> corner true.
- "kothi", "independent house", "built" -> vacant_or_built "built"; "khali", "empty plot" -> "vacant".
- A kothi / independent house / bungalow / villa sold with its land is type "plot" (vacant_or_built "built"), NOT a
  builder_floor. A builder_floor is ONE floor of a builder-built low-rise (has a floor number and/or BHK).
- "wide road", "30 ft road" -> road_width_ft numeric.
SOFT DATA
- "owner serious", "fixed", "no bargaining", "firm" -> price_flexibility "firm".
- "thoda negotiable", "little room", "negotiable" -> "some".
- "urgent sale", "owner needs money", "kaafi flexible", "will drop X%" -> "high" (keep the exact quote in notes).
- Power backup, owner mood, drop-percentages etc. go in notes (free text), never in enum fields.
- "lift", "elevator" -> lift true. "no lift" -> false.
CONSTRUCTION: "brand new", "premium finish", "fully finished" -> premium; "old", "purani", "needs renovation" -> old.
VERIFIED: if the broker says they personally saw/verified something ("maine dekha", "I visited"), add it to
"verified_fields". Otherwise facts are owner-claimed.
"""
