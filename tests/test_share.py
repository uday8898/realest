"""Offline tests: share-to-WhatsApp (client-safe text, 24h window/template rules, errors, rate limit)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["WA_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "ts.db")
for k in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_ID", "WHATSAPP_SHARE_TEMPLATE"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402
from app import db, main, share, whatsapp  # noqa: E402

client = TestClient(main.app, base_url="http://localhost")  # no `with`: skips the background scheduler


class Resp:
    def __init__(self, status=200, js=None):
        self.status_code, self._js = status, js or {}

    def json(self):
        return self._js


# ---- phone numbers
n = whatsapp.normalize_phone
assert n("+91 98100-11111") == "919810011111" and n("9810011111") == "919810011111"
assert n("919810011111") == "919810011111" and n("12345") is None and n("0981001111") is None
assert n("+1 415 555 2671") == "14155552671"

# ---- client-safe text: nothing confidential leaves
l1 = next(l for l in db.list_listings("rohit") if l["type"] == "plot" and l["micro_market"] == "DLF Phase 1")
txt = share.listings_text("rohit", [l1["id"]])
assert "250 gaj corner plot" in txt and "15 cr" in txt and "6 lakh/gaj" in txt, txt
for secret in (l1["owner_contact"], l1.get("owner_name") or "Bhalla", "firm", "serious", "bargaining", "owner"):
    assert secret.lower() not in txt.lower().replace("owner's", ""), f"leaked {secret!r}: {txt}"
assert "Rohit Sharma" in txt and "Sharma Realty" in txt and "(demo)" not in txt
multi = share.listings_text("rohit", [4, 5])
assert "1)" in multi and "2)" in multi and "Here are 2 options" in multi
# a broker can only build text for listings they can see (own + shared), never another brokerage's
vik = next(l for l in db.list_listings("vikram"))
assert share.listings_text("rohit", [vik["id"]]) == ""
private = next(l for l in db.list_listings("anita") if "PRIVATE" in (l.get("notes") or ""))
assert share.listings_text("rohit", [private["id"]]) == "", "teammate's private listing"
assert share.ids_in("1) #5 floor ... 2) #15 floor, also #5") == [5, 15]

# ---- send_to: not configured
try:
    whatsapp.send_to("919810011111", "hi")
    raise AssertionError("must refuse when not configured")
except whatsapp.SendError as e:
    assert "isn't connected" in str(e) and "Open in WhatsApp" in str(e)

os.environ.update(WHATSAPP_TOKEN="t", WHATSAPP_PHONE_ID="123")
sent = []


def ok_post(url, headers=None, json=None, timeout=None):
    sent.append((url, json))
    return Resp(200, {"messages": [{"id": "wamid.X"}]})


# outside the 24h window and no template -> explicit, actionable refusal
try:
    whatsapp.send_to("919810011111", "hi", post=ok_post)
    raise AssertionError("must refuse outside window without a template")
except whatsapp.SendError as e:
    assert "24 hours" in str(e) and "WHATSAPP_SHARE_TEMPLATE" in str(e) and not sent

# with a template: one body variable, no newlines
os.environ.update(WHATSAPP_SHARE_TEMPLATE="share_listing", WHATSAPP_SHARE_TEMPLATE_LANG="en")
res = whatsapp.send_to("919810011111", "Hi!\n\n1) A    B\n2) C", post=ok_post)
body = sent[-1][1]
assert res["mode"] == "template" and body["type"] == "template" and body["template"]["name"] == "share_listing"
param = body["template"]["components"][0]["parameters"][0]["text"]
assert "\n" not in param and "    " not in param and param.startswith("Hi! | 1) A B"), param
assert sent[-1][0].endswith("/123/messages")

# recipient messaged us -> free-form text
db.touch_inbound("919810011111")
res = whatsapp.send_to("919810011111", "line1\nline2", post=ok_post)
assert res["mode"] == "text" and sent[-1][1]["text"]["body"] == "line1\nline2" and res["id"] == "wamid.X"

# Meta error is surfaced readably
bad = lambda *a, **k: Resp(400, {"error": {"message": "Template not found", "code": 132001}})
try:
    whatsapp.send_to("919810011111", "x", post=bad)
    raise AssertionError
except whatsapp.SendError as e:
    assert "Template not found" in str(e) and "132001" in str(e)

# an inbound webhook message opens the window for that number
assert not whatsapp.window_open("919999999999")
whatsapp.handle_incoming({"entry": [{"changes": [{"value": {"messages": [
    {"from": "919999999999", "id": "w9", "type": "text", "text": {"body": "hi"}}]}}]}]}, send=lambda *a: None)
assert whatsapp.window_open("919999999999")

# ---- HTTP endpoints
whatsapp.send_to = lambda phone, text, post=None: {"mode": "text", "id": "wamid.T"}
r = client.post("/api/whatsapp/send", json={"broker_id": "rohit", "to": "98100 11111", "text": "hello"})
assert r.status_code == 200 and r.json()["to"] == "919810011111", r.text
assert any(s["status"] == "sent" for s in db.list_shares("rohit"))
assert client.post("/api/whatsapp/send", json={"broker_id": "rohit", "to": "123", "text": "x"}).status_code == 400
assert client.post("/api/whatsapp/send", json={"broker_id": "rohit", "to": "9810011111", "text": " "}).status_code == 400
assert client.post("/api/whatsapp/send", json={"broker_id": "nobody", "to": "9810011111", "text": "x"}).status_code == 404

r = client.post("/api/share-text", json={"broker_id": "rohit", "listing_ids": [l1["id"]]})
assert "15 cr" in r.json()["text"] and l1["owner_contact"] not in r.json()["text"]
r = client.post("/api/share-text/from-reply", json={"broker_id": "rohit", "text": f"1) #{l1['id']} plot — 15 cr, owner firm"}).json()
assert r["client_safe"] and "firm" not in r["text"].lower()
r = client.post("/api/share-text/from-reply", json={"broker_id": "rohit", "text": "Reminder: call the owner Friday"}).json()
assert not r["client_safe"] and r["text"].startswith("Reminder")

# rate limit: 30 sends/hour per broker
for _ in range(main.MAX_SENDS_PER_HOUR):
    db.log_share("anita", "919810011111", "text", "sent", "x")
assert client.post("/api/whatsapp/send", json={"broker_id": "anita", "to": "9810011111", "text": "x"}).status_code == 429

# failed sends are logged and reported, not hidden
def refuse(phone, text, post=None):
    raise whatsapp.SendError("nope")
whatsapp.send_to = refuse
r = client.post("/api/whatsapp/send", json={"broker_id": "rohit", "to": "9810011111", "text": "x"})
assert r.status_code == 502 and "nope" in r.json()["detail"]
assert db.list_shares("rohit")[0]["status"] == "failed"

print("all share tests passed")
