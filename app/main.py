"""FastAPI app: WhatsApp-style simulator + production-shaped endpoints (webhook, group relay, admin).
Everything below `service.process_message()` is channel-agnostic."""
import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import admin, db, engine, groups, llm, matching as M, scheduler, seed, service, share as share_mod, whatsapp

ROOT = service.ROOT
UPLOADS = service.UPLOADS


async def _scheduler_loop():
    while True:
        try:
            await asyncio.to_thread(scheduler.tick)
        except Exception as e:  # keep the loop alive
            print("scheduler error:", e)
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_):
    task = asyncio.create_task(_scheduler_loop())
    yield
    task.cancel()


app = FastAPI(title="WhatsApp AI Assistant for Brokers – prototype", lifespan=lifespan)
db.init()
if not db.brokers() and os.environ.get("SEED_DEMO", "1") != "0":
    seed.run(reset=False)

# ---------------------------------------------------------------- public-deployment safety
# The prototype identifies brokers by the broker_id the client sends, so an open API would let anyone read any
# broker's data and spend the AI budget. Every /api call therefore needs the shared APP_ACCESS_KEY, and the server
# refuses to serve /api on a non-local host until one is configured (fail closed).
GATE_OPEN = {"/api/health"}            # liveness check only
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


@app.middleware("http")
async def access_gate(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS" or not path.startswith("/api/") or path in GATE_OPEN:
        return await call_next(request)
    key = os.environ.get("APP_ACCESS_KEY", "")
    if not key:
        if request.url.hostname in LOCAL_HOSTS:
            return await call_next(request)       # local development
        return JSONResponse({"detail": "Server is not configured: set APP_ACCESS_KEY."}, status_code=503)
    got = request.headers.get("x-access-key") or request.query_params.get("key") or ""
    if not hmac.compare_digest(got.encode(), key.encode()):
        return JSONResponse({"detail": "Access code required."}, status_code=401)
    return await call_next(request)


# CORS: only the frontend origins listed in CORS_ORIGINS (comma separated, e.g. https://realest.vercel.app).
# Added last so it is the outermost layer and also decorates the 401/503 responses above.
app.add_middleware(
    CORSMiddleware, allow_origins=[o.strip().rstrip("/") for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()],
    allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["content-type", "x-access-key", "x-ingest-token"], max_age=600)


@app.exception_handler(admin.Forbidden)
async def _forbidden(_, e):
    return JSONResponse({"error": str(e)}, status_code=403)


@app.get("/api/health")
def health():
    return {"llm_configured": llm.configured(),
            "chat_deployment": os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o"),
            "transcribe": "azure-speech" if os.environ.get("AZURE_SPEECH_KEY") else
            os.environ.get("AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT", "gpt-4o-transcribe")}


@app.get("/api/brokers")
def brokers():
    out = db.brokers()
    for b in out:
        b["brokerage"] = (db.get_brokerage(b.get("brokerage_id")) or {}).get("name")
    return out


@app.get("/api/samples")
def samples():
    with open(os.path.join(ROOT, "samples", "messages.json"), encoding="utf-8") as f:
        return json.load(f)


@app.get("/api/group-samples")
def group_samples():
    with open(os.path.join(ROOT, "samples", "group_feed.json"), encoding="utf-8") as f:
        return json.load(f)


@app.get("/api/state")
def state(broker_id: str):
    ls = db.list_listings(broker_id)
    live = [l for l in ls if l["status"] in ("available", "under_negotiation", "stale")]
    fresh = [l for l in live if (M.days_since(l.get("last_confirmed")) or 0) <= 30]
    visible = db.visible_listings(broker_id)
    team = [l for l in visible if l.get("shared_by")]
    for l in ls + team:
        l["label"] = M.label(l)
        l["title"], l["market"], l["facts"] = M.title(l), l.get("micro_market") or "Location not set", M.facts(l)
        l["age_days"] = M.days_since(l.get("last_confirmed"))
    reqs = db.list_requirements(broker_id)
    for r in reqs:
        r["label"] = M.req_label(r)
        r["match_count"] = len(M.find_matches(r, visible, 5)) if r["status"] == "open" else 0
    me = db.get_broker(broker_id) or {}
    return {"me": {**me, "brokerage": (db.get_brokerage(me.get("brokerage_id")) or {}).get("name"),
                   "team_size": len(db.team_ids(broker_id))},
            "listings": ls, "team_listings": team, "requirements": reqs, "groups": db.list_groups(broker_id),
            "history": db.history(broker_id, 60), "messages": db.messages(broker_id), "tasks": db.list_tasks(broker_id),
            "notes": [{k: v for k, v in n.items() if k != "embedding"} for n in db.list_notes(broker_id, 100)],
            "settings": db.get_settings(broker_id),
            "metrics": {"live": len(live), "fresh_pct": round(100 * len(fresh) / len(live)) if live else 100}}


@app.get("/api/requirement/matches")
def requirement_matches(broker_id: str, id: int):
    """Top matches for one of THIS broker's requirements (a broker can never read another's)."""
    r = db.get_requirement(broker_id, id)
    if not r:
        raise HTTPException(404, "requirement not found")
    out = []
    for m in M.find_matches(r, db.visible_listings(broker_id), 5):
        l = m["listing"]
        out.append({"id": l["id"], "title": M.title(l), "market": l.get("micro_market"), "price": l.get("price_total"),
                    "rent": l.get("purpose") == "rent", "fits": m["matches"][:4], "lacks": m["lacks"][:3],
                    "shared_by": l.get("shared_by")})
    return out


@app.get("/api/briefing")
def briefing(broker_id: str):
    if not db.get_broker(broker_id):
        raise HTTPException(404, "unknown broker")
    return engine.briefing(broker_id)


@app.get("/api/poll")
def poll(broker_id: str, after: int = 0):
    """Proactive bot messages (check-ins, alerts, reminders) arrive here; a real channel would push them."""
    return {"messages": db.messages_after(broker_id, after)}


@app.post("/api/dev/trigger")
def trigger(broker_id: str, kind: str):
    """Demo buttons: fire a scheduler event now instead of waiting for the clock."""
    if kind not in ("ping", "eod", "tasks"):
        return JSONResponse({"error": "kind must be ping|eod|tasks"}, status_code=400)
    return {"sent": scheduler.tick(force=kind, only=broker_id)}


@app.post("/api/reset")
def reset():
    """Demo only: wipes everything and reloads the sample data. Disabled when SEED_DEMO=0 (real data)."""
    if os.environ.get("SEED_DEMO", "1") == "0":
        raise HTTPException(403, "Reset is disabled because SEED_DEMO=0 (this server holds real data).")
    seed.run(reset=True)
    return {"ok": True}


@app.post("/api/chat")
async def chat(broker_id: str = Form(...), text: str = Form(""), files: List[UploadFile] = File(default=[])):
    if not db.get_broker(broker_id):
        raise HTTPException(404, "unknown broker")
    blobs = [(f.filename or "file", f.content_type or "", await f.read()) for f in files]
    return await asyncio.to_thread(service.process_message, broker_id, text, blobs)


@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)):
    if not llm.configured():
        return JSONResponse({"error": "Azure OpenAI not configured"}, status_code=400)
    data = await file.read()
    return {"text": llm.transcribe(file.filename or "voice.webm", data, file.content_type or "audio/webm")}


# ------------------------------------------------------------------ Phase 4: groups
@app.post("/api/group/ingest")
async def group_ingest(request: Request, body: dict = Body(...)):
    """Relay endpoint for a group listener. Optional shared secret via GROUP_INGEST_TOKEN."""
    secret = os.environ.get("GROUP_INGEST_TOKEN")
    if secret and request.headers.get("x-ingest-token") != secret:
        raise HTTPException(401, "bad token")
    b, text = body.get("broker_id"), (body.get("text") or "").strip()
    if not db.get_broker(b or "") or not body.get("group") or not text:
        raise HTTPException(400, "broker_id, group and text are required")
    if not llm.configured():
        raise HTTPException(503, "Azure OpenAI not configured")
    return await asyncio.to_thread(groups.ingest, b, body["group"], body.get("sender") or "Someone", text)


@app.post("/api/group/mute")
def group_mute(broker_id: str, group: str, muted: bool = True):
    db.set_group_muted(broker_id, group, muted)
    return {"groups": db.list_groups(broker_id)}


# ------------------------------------------------------------------ Phase 4: team pool
@app.post("/api/share")
def share(broker_id: str, listing_id: int, shared: bool = True):
    if len(db.team_ids(broker_id)) < 2:
        raise HTTPException(400, "Not part of a team")
    return {"changed": [l["id"] for l in engine.set_shared(broker_id, [listing_id], shared)]}


# ------------------------------------------------------------------ Phase 4: admin (account owner)
@app.get("/api/admin/overview")
def admin_overview(broker_id: str):
    return admin.overview(broker_id)


@app.get("/api/admin/export")
def admin_export(broker_id: str, format: str = "json"):
    if format == "csv":
        return Response(admin.listings_csv(broker_id), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=inventory.csv"})
    return Response(json.dumps(admin.export_rows(broker_id), ensure_ascii=False, indent=1, default=str),
                    media_type="application/json", headers={"Content-Disposition": "attachment; filename=export.json"})


@app.post("/api/admin/brokers")
def admin_add_broker(broker_id: str, body: dict = Body(...)):
    try:
        return admin.add_broker(broker_id, body.get("name", ""), body.get("phone", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ share to WhatsApp (web app -> client / self)
MAX_SENDS_PER_HOUR = 30


@app.get("/api/whatsapp/status")
def wa_status():
    return {"api_configured": whatsapp.api_configured(),
            "template_configured": bool(os.environ.get("WHATSAPP_SHARE_TEMPLATE"))}


@app.post("/api/share-text")
def share_text(body: dict = Body(...)):
    """Client-safe text for listings (owner contact, flexibility and private notes are left out)."""
    b = body.get("broker_id") or ""
    if not db.get_broker(b):
        raise HTTPException(404, "unknown broker")
    ids = [int(i) for i in body.get("listing_ids") or []]
    return {"text": share_mod.listings_text(b, ids)}


@app.post("/api/share-text/from-reply")
def share_text_from_reply(body: dict = Body(...)):
    """For a bot reply: if it mentions listings (#5, #15) build a client-safe message from the database,
    otherwise return the reply text itself so the broker can review and edit it."""
    b, text = body.get("broker_id") or "", body.get("text") or ""
    if not db.get_broker(b):
        raise HTTPException(404, "unknown broker")
    safe = share_mod.listings_text(b, share_mod.ids_in(text))
    return {"text": safe or text, "client_safe": bool(safe)}


@app.post("/api/whatsapp/send")
def wa_send(body: dict = Body(...)):
    b, text = body.get("broker_id") or "", (body.get("text") or "").strip()
    if not db.get_broker(b):
        raise HTTPException(404, "unknown broker")
    phone = whatsapp.normalize_phone(body.get("to") or "")
    if not phone:
        raise HTTPException(400, "Enter a valid WhatsApp number, e.g. +91 98xxxxxxxx.")
    if not text:
        raise HTTPException(400, "Nothing to send.")
    from datetime import datetime, timedelta, timezone
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    if db.shares_since(b, hour_ago) >= MAX_SENDS_PER_HOUR:
        raise HTTPException(429, f"Limit of {MAX_SENDS_PER_HOUR} WhatsApp sends per hour reached.")
    try:
        res = whatsapp.send_to(phone, text)
    except whatsapp.SendError as e:
        db.log_share(b, phone, "api", "failed", text)
        raise HTTPException(502, str(e))
    db.log_share(b, phone, res["mode"], "sent", text)
    db.log(b, "share", None, "whatsapp_sent", f"to ...{phone[-4:]} ({res['mode']})")
    return {"ok": True, **res, "to": phone}


# ------------------------------------------------------------------ Phase 4: WhatsApp webhook
@app.get("/webhook/whatsapp")
def wa_verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == os.environ.get("WHATSAPP_VERIFY_TOKEN", "-"):
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "verification failed")


@app.post("/webhook/whatsapp")
async def wa_incoming(request: Request):
    raw = await request.body()
    secret = os.environ.get("WHATSAPP_APP_SECRET")
    if secret:  # Meta signs every webhook call: reject anything that isn't from Meta
        import hashlib, hmac
        good = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, request.headers.get("x-hub-signature-256", "")):
            raise HTTPException(403, "bad signature")
    return {"results": await asyncio.to_thread(whatsapp.handle_incoming, json.loads(raw or b"{}"))}


app.mount("/uploads", StaticFiles(directory=UPLOADS), name="uploads")
app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "static")), name="static")
app.mount("/brand", StaticFiles(directory=os.path.join(ROOT, "static", "brand")), name="brand")


@app.get("/config.js")
def config_js():
    return FileResponse(os.path.join(ROOT, "static", "config.js"), media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/")
def index():
    return FileResponse(os.path.join(ROOT, "static", "index.html"), headers={"Cache-Control": "no-cache"})
