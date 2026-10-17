"""Offline tests: the public-deployment gate (access code, fail-closed, CORS) and brand/config routes."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["WA_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "sec.db")
os.environ["CORS_ORIGINS"] = "https://realest.vercel.app, https://app.example.com/"   # read when the app is created
os.environ.pop("APP_ACCESS_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402
from app import main  # noqa: E402

remote = TestClient(main.app, base_url="https://api.example.com")      # a public host
local = TestClient(main.app, base_url="http://localhost")
ORIGIN = {"Origin": "https://realest.vercel.app"}

# ---- fail closed: no access code configured + public host => the API is not served at all
r = remote.get("/api/brokers")
assert r.status_code == 503 and "APP_ACCESS_KEY" in r.json()["detail"], r.text
assert remote.get("/api/health").status_code == 200, "liveness stays open"
assert local.get("/api/brokers").status_code == 200, "local development needs no code"

# ---- with a code configured, every /api call needs it (header or ?key=), and wrong codes are refused
os.environ["APP_ACCESS_KEY"] = "s3cret-code"
assert remote.get("/api/brokers").status_code == 401
assert remote.get("/api/brokers", headers={"x-access-key": "wrong"}).status_code == 401
assert remote.get("/api/brokers", headers={"x-access-key": "s3cret-codeX"}).status_code == 401
assert remote.get("/api/brokers", headers={"x-access-key": "s3cret-code"}).status_code == 200
assert remote.get("/api/brokers", params={"key": "s3cret-code"}).status_code == 200
assert local.get("/api/brokers").status_code == 401, "once a code is set it applies everywhere"
for path, method in (("/api/state?broker_id=rohit", "get"), ("/api/briefing?broker_id=rohit", "get"),
                     ("/api/admin/overview?broker_id=rohit", "get"), ("/api/samples", "get")):
    assert getattr(remote, method)(path).status_code == 401, path
assert remote.post("/api/chat", data={"broker_id": "rohit", "text": "hi"}).status_code == 401, "chat spends AI budget"
assert remote.post("/api/group/ingest", json={"broker_id": "rohit", "group": "g", "text": "x"}).status_code == 401
assert remote.post("/api/reset").status_code == 401
assert remote.get("/api/health").status_code == 200
assert remote.get("/api/state", params={"broker_id": "rohit"}, headers={"x-access-key": "s3cret-code"}).status_code == 200

# ---- WhatsApp webhook has its own verification and must NOT be blocked by the access code
assert remote.get("/webhook/whatsapp").status_code == 403           # verification failed, not 401
r = remote.post("/webhook/whatsapp", json={"entry": []})
assert r.status_code == 200, r.text

# ---- CORS: only the listed frontends, preflight allowed, errors still carry the headers
pre = remote.options("/api/chat", headers={**ORIGIN, "Access-Control-Request-Method": "POST",
                                           "Access-Control-Request-Headers": "x-access-key,content-type"})
assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == "https://realest.vercel.app", pre.headers
assert "x-access-key" in pre.headers["access-control-allow-headers"].lower()
bad = remote.options("/api/chat", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
assert "access-control-allow-origin" not in bad.headers
denied = remote.get("/api/brokers", headers=ORIGIN)                  # 401 from the gate still readable by the real frontend
assert denied.status_code == 401 and denied.headers.get("access-control-allow-origin") == "https://realest.vercel.app"
assert remote.get("/api/brokers", headers={"Origin": "https://evil.example"}).headers.get("access-control-allow-origin") is None
assert remote.get("/api/health", headers={"Origin": "https://app.example.com"}).headers["access-control-allow-origin"] == "https://app.example.com"

# ---- static assets the split frontend needs, served at the root of the backend too
assert remote.get("/brand/mark.webp").status_code == 200
assert remote.get("/config.js").status_code == 200 and "API_BASE" in remote.get("/config.js").text
assert remote.get("/").status_code == 200

print("all security tests passed")
