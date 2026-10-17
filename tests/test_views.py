"""Offline tests: structured row data (title/market/facts) and per-requirement matches endpoint."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["WA_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "tv.db")

os.environ["APP_ACCESS_KEY"] = ""   # tests must not depend on a real key from the developer's .env

from fastapi.testclient import TestClient  # noqa: E402
from app import db, main, matching as M  # noqa: E402

c = TestClient(main.app, base_url="http://localhost")

st = c.get("/api/state", params={"broker_id": "yuvraj"}).json()
assert st["listings"], "seed data present"
for l in st["listings"] + st["team_listings"]:
    assert l["title"] and l["market"] and isinstance(l["facts"], list), l["id"]
    assert l["title"].split()[0] != "None"

plot = next(l for l in st["listings"] if l["type"] == "plot" and l["micro_market"] == "DLF Phase 1")
assert plot["title"] == "250 gaj plot (corner)" and plot["market"] == "DLF Phase 1"
assert "north-facing" in plot["facts"] and "24 ft road" in plot["facts"]
floor = next(l for l in st["listings"] if l["type"] == "builder_floor" and l["micro_market"] == "Sushant Lok 1" and l["bedrooms"] == 3 and l["floor_number"] == 3)
assert floor["title"] == "3 BHK builder floor" and "3rd floor" in floor["facts"] and "park facing" in floor["facts"]
apt = next(l for l in st["listings"] if l["type"] == "apartment" and l.get("society") == "Crest Residency")
assert apt["title"] == "3 BHK apartment" and apt["facts"][0] == "Crest Residency" and "2100 sq ft" in apt["facts"]
assert M.title({"type": "plot", "size_gaj": 300}) == "300 gaj plot" and M.facts({}) == []

# requirement matches: own requirement works, shape is what the UI needs
sharma = next(r for r in st["requirements"] if r["client_name"] == "Mr. Sharma")
r = c.get("/api/requirement/matches", params={"broker_id": "yuvraj", "id": sharma["id"]})
assert r.status_code == 200 and r.json(), r.text
m = r.json()[0]
assert {"id", "title", "market", "price", "rent", "fits", "lacks", "shared_by"} <= set(m)

# isolation: another broker (even a teammate) cannot read this requirement's matches
for other in ("anita", "vikram"):
    assert c.get("/api/requirement/matches", params={"broker_id": other, "id": sharma["id"]}).status_code == 404
assert c.get("/api/requirement/matches", params={"broker_id": "yuvraj", "id": 99999}).status_code == 404

print("all view tests passed")
