"""Channel-agnostic message pipeline. The simulator (`/api/chat`) and the WhatsApp webhook both call this."""
import io
import os
import time
import uuid
from typing import Optional

from . import db, engine, llm

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
UPLOADS = os.path.join(ROOT, "data", "uploads")
os.makedirs(UPLOADS, exist_ok=True)

NOT_CONFIGURED = ("Azure OpenAI isn't configured yet. Copy .env.example to .env, add your endpoint, key and "
                  "deployment names, then restart the server.")


def save_upload(data: bytes, name: str) -> str:
    ext = os.path.splitext(name)[1].lower() or ".bin"
    fn = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(UPLOADS, fn), "wb") as f:
        f.write(data)
    return f"/uploads/{fn}"


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages).strip()


def process_message(broker_id: str, text: str, files: Optional[list] = None) -> dict:
    """files: [(filename, content_type, bytes)]. Returns {"replies": [...], "transcript": str|None, "last_id": int}."""
    t0 = time.perf_counter()
    media, images, extra, transcript = [], [], [], None
    for name, ctype, data in files or []:
        ctype = (ctype or "").split(";")[0]
        if ctype.startswith("audio/") or (ctype == "video/webm" and name.startswith("voice")):
            if not llm.configured():
                break
            transcript = llm.transcribe(name, data, ctype or "audio/webm")
            extra.append(transcript)
            media.append({"kind": "audio", "transcript": transcript})
        elif ctype.startswith("image/"):
            url = save_upload(data, name)
            images.append((ctype, data, url))
            media.append({"kind": "image", "url": url})
        elif ctype == "application/pdf":
            body = pdf_text(data)
            extra.append(f"[PDF text]\n{body}" if body else "[PDF had no readable text]")
            media.append({"kind": "pdf", "name": name, "url": save_upload(data, name)})
    full_text = "\n".join(x for x in [text.strip(), *extra] if x)
    db.add_message(broker_id, "broker", text.strip(), media)

    if not llm.configured():
        db.add_message(broker_id, "bot", NOT_CONFIGURED)
        return {"replies": [engine.R(NOT_CONFIGURED)], "transcript": transcript, "last_id": _last_id(broker_id)}

    before = db.get_session(broker_id).get("last_listing_id")
    try:
        replies = engine.handle(broker_id, full_text, [(m, d) for m, d, _ in images])
    except Exception as e:
        replies = [engine.R(f"Something went wrong talking to the AI service: {type(e).__name__}: {e}")]

    sess = db.get_session(broker_id)
    target = sess.pop("pending_photo_listing", None) or \
        (sess.get("last_listing_id") if sess.get("last_listing_id") != before else None)
    if target and images:
        l = db.get_listing(broker_id, int(target))
        if l:
            l.setdefault("photos", []).extend(u for _, _, u in images)
            db.save_listing(broker_id, l)
            db.log(broker_id, "listing", l["id"], "photo_added", f"{len(images)} file(s)")
    db.set_session(broker_id, sess)

    for r in replies:
        db.add_message(broker_id, "bot", r["text"], [{"kind": "image", "url": u} for u in r["images"]])
    meta = db.get_session(broker_id).get("_meta") or {}
    return {"replies": replies, "transcript": transcript, "last_id": _last_id(broker_id),
            "ms": round((time.perf_counter() - t0) * 1000), "fast": bool(meta.get("fast")),
            "suggestions": meta.get("suggestions", [])}


def _last_id(broker_id: str) -> int:
    m = db.messages(broker_id, 1)
    return m[-1]["id"] if m else 0
