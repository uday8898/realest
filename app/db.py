"""SQLite storage. Every row carries broker_id: each broker only ever sees their own data."""
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

DB_PATH = os.environ.get("WA_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "app.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS brokers (id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS listings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT, type TEXT, purpose TEXT,
  status TEXT DEFAULT 'available', last_confirmed TEXT, source TEXT,
  data TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS requirements (
  id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT, status TEXT DEFAULT 'open',
  data TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT, entity TEXT, entity_id INTEGER,
  event TEXT, detail TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT, role TEXT, text TEXT, media TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS sessions (broker_id TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT, text TEXT, due TEXT, done INTEGER DEFAULT 0, ts TEXT,
  reminded INTEGER DEFAULT 0, outcome TEXT, listing_id INTEGER, requirement_id INTEGER);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT, kind TEXT, text TEXT, listing_id INTEGER,
  requirement_id INTEGER, client_name TEXT, embedding TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS settings (broker_id TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS brokerages (id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS wa_inbound (phone TEXT PRIMARY KEY, ts TEXT);
CREATE TABLE IF NOT EXISTS shares (
  id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT, to_phone TEXT, mode TEXT, status TEXT, text TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT, name TEXT, muted INTEGER DEFAULT 0,
  seen INTEGER DEFAULT 0, surfaced INTEGER DEFAULT 0, ignored INTEGER DEFAULT 0, duplicates INTEGER DEFAULT 0,
  last_seen TEXT, UNIQUE(broker_id, name));
CREATE TABLE IF NOT EXISTS group_seen (broker_id TEXT, hash TEXT, ts TEXT, PRIMARY KEY(broker_id, hash));
"""

MIGRATIONS = ["ALTER TABLE tasks ADD COLUMN reminded INTEGER DEFAULT 0", "ALTER TABLE tasks ADD COLUMN outcome TEXT",
              "ALTER TABLE tasks ADD COLUMN listing_id INTEGER", "ALTER TABLE tasks ADD COLUMN requirement_id INTEGER",
              "ALTER TABLE brokers ADD COLUMN brokerage_id TEXT", "ALTER TABLE brokers ADD COLUMN role TEXT DEFAULT 'broker'",
              "ALTER TABLE brokers ADD COLUMN phone TEXT"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        for m in MIGRATIONS:  # upgrade databases created before Phase 3
            try:
                c.execute(m)
            except sqlite3.OperationalError:
                pass


def wipe() -> None:
    with conn() as c:
        for t in ("brokers", "listings", "requirements", "history", "messages", "sessions", "tasks", "notes", "settings", "brokerages", "groups", "group_seen", "wa_inbound", "shares"):
            c.execute(f"DELETE FROM {t}")


def _listing(r: sqlite3.Row) -> dict:
    d = json.loads(r["data"])
    d.update(id=r["id"], broker_id=r["broker_id"], type=r["type"], purpose=r["purpose"],
             status=r["status"], last_confirmed=r["last_confirmed"], source=r["source"],
             created_at=r["created_at"])
    return d


def _split(d: dict) -> dict:
    meta = {k: d.pop(k, None) for k in ("id", "broker_id", "type", "purpose", "status",
                                         "last_confirmed", "source", "created_at")}
    return meta


def add_listing(broker_id: str, data: dict, status: str = "available",
                last_confirmed: Optional[str] = None) -> int:
    data = dict(data)
    meta = _split(data)
    ts = now()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO listings(broker_id,type,purpose,status,last_confirmed,source,data,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (broker_id, meta["type"], meta["purpose"] or "sale", status, last_confirmed or ts,
             meta["source"], json.dumps(data), meta["created_at"] or ts, ts))
        return cur.lastrowid


def get_listing(broker_id: str, lid: int) -> Optional[dict]:
    with conn() as c:
        r = c.execute("SELECT * FROM listings WHERE id=? AND broker_id=?", (lid, broker_id)).fetchone()
    return _listing(r) if r else None


def list_listings(broker_id: str) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM listings WHERE broker_id=? ORDER BY id DESC", (broker_id,)).fetchall()
    return [_listing(r) for r in rows]


def save_listing(broker_id: str, listing: dict) -> None:
    d = dict(listing)
    meta = {k: d.pop(k, None) for k in ("id", "broker_id", "type", "purpose", "status",
                                         "last_confirmed", "source", "created_at")}
    with conn() as c:
        c.execute("UPDATE listings SET type=?,purpose=?,status=?,last_confirmed=?,source=?,data=?,updated_at=?"
                  " WHERE id=? AND broker_id=?",
                  (meta["type"], meta["purpose"], meta["status"], meta["last_confirmed"], meta["source"],
                   json.dumps(d), now(), listing["id"], broker_id))


def add_requirement(broker_id: str, data: dict, created_at: Optional[str] = None) -> int:
    with conn() as c:
        cur = c.execute("INSERT INTO requirements(broker_id,status,data,created_at) VALUES(?,?,?,?)",
                        (broker_id, "open", json.dumps(data), created_at or now()))
        return cur.lastrowid


def _req(r: sqlite3.Row) -> dict:
    d = json.loads(r["data"])
    d.update(id=r["id"], status=r["status"], created_at=r["created_at"], broker_id=r["broker_id"])
    return d


def get_requirement(broker_id: str, rid: int) -> Optional[dict]:
    with conn() as c:
        r = c.execute("SELECT * FROM requirements WHERE id=? AND broker_id=?", (rid, broker_id)).fetchone()
    return _req(r) if r else None


def list_requirements(broker_id: str, only_open: bool = False) -> list[dict]:
    q = "SELECT * FROM requirements WHERE broker_id=?" + (" AND status='open'" if only_open else "")
    with conn() as c:
        rows = c.execute(q + " ORDER BY id DESC", (broker_id,)).fetchall()
    return [_req(r) for r in rows]


def save_requirement(broker_id: str, req: dict) -> None:
    d = {k: v for k, v in req.items() if k not in ("id", "status", "created_at", "broker_id")}
    with conn() as c:
        c.execute("UPDATE requirements SET status=?,data=? WHERE id=? AND broker_id=?",
                  (req["status"], json.dumps(d), req["id"], broker_id))


def log(broker_id: str, entity: str, entity_id: Optional[int], event: str, detail: Any = "") -> None:
    with conn() as c:
        c.execute("INSERT INTO history(broker_id,entity,entity_id,event,detail,ts) VALUES(?,?,?,?,?,?)",
                  (broker_id, entity, entity_id, event,
                   detail if isinstance(detail, str) else json.dumps(detail), now()))


def history(broker_id: str, limit: int = 100, entity: Optional[str] = None,
            entity_id: Optional[int] = None) -> list[dict]:
    q, args = "SELECT * FROM history WHERE broker_id=?", [broker_id]
    if entity:
        q += " AND entity=? AND entity_id=?"
        args += [entity, entity_id]
    with conn() as c:
        rows = c.execute(q + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
    return [dict(r) for r in rows]


def add_message(broker_id: str, role: str, text: str, media: Optional[list] = None) -> None:
    with conn() as c:
        c.execute("INSERT INTO messages(broker_id,role,text,media,ts) VALUES(?,?,?,?,?)",
                  (broker_id, role, text, json.dumps(media or []), now()))


def last_broker_message_ts(broker_id: str) -> Optional[str]:
    with conn() as c:
        r = c.execute("SELECT ts FROM messages WHERE broker_id=? AND role='broker' ORDER BY id DESC LIMIT 1",
                      (broker_id,)).fetchone()
    return r["ts"] if r else None


def messages_after(broker_id: str, after_id: int) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM messages WHERE broker_id=? AND id>? ORDER BY id", (broker_id, after_id)).fetchall()
    out = [dict(r) for r in rows]
    for m in out:
        m["media"] = json.loads(m["media"] or "[]")
    return out


def messages(broker_id: str, limit: int = 200) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM messages WHERE broker_id=? ORDER BY id DESC LIMIT ?",
                         (broker_id, limit)).fetchall()
    out = [dict(r) for r in rows][::-1]
    for m in out:
        m["media"] = json.loads(m["media"] or "[]")
    return out


def get_session(broker_id: str) -> dict:
    with conn() as c:
        r = c.execute("SELECT data FROM sessions WHERE broker_id=?", (broker_id,)).fetchone()
    return json.loads(r["data"]) if r else {}


def set_session(broker_id: str, data: dict) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO sessions(broker_id,data) VALUES(?,?)", (broker_id, json.dumps(data)))


def add_task(broker_id: str, text: str, due: Optional[str], listing_id: Optional[int] = None,
             requirement_id: Optional[int] = None) -> int:
    with conn() as c:
        return c.execute("INSERT INTO tasks(broker_id,text,due,ts,listing_id,requirement_id) VALUES(?,?,?,?,?,?)",
                         (broker_id, text, due, now(), listing_id, requirement_id)).lastrowid


def list_tasks(broker_id: str, only_open: bool = False) -> list[dict]:
    q = "SELECT * FROM tasks WHERE broker_id=?" + (" AND done=0" if only_open else "")
    with conn() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY id DESC", (broker_id,)).fetchall()]


def get_task(broker_id: str, tid: int) -> Optional[dict]:
    with conn() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=? AND broker_id=?", (tid, broker_id)).fetchone()
    return dict(r) if r else None


def update_task(broker_id: str, tid: int, **f: Any) -> None:
    cols = ", ".join(f"{k}=?" for k in f)
    with conn() as c:
        c.execute(f"UPDATE tasks SET {cols} WHERE id=? AND broker_id=?", [*f.values(), tid, broker_id])


def add_note(broker_id: str, kind: str, text: str, listing_id: Optional[int] = None,
             requirement_id: Optional[int] = None, client_name: Optional[str] = None,
             embedding: Optional[list] = None, ts: Optional[str] = None) -> int:
    with conn() as c:
        return c.execute(
            "INSERT INTO notes(broker_id,kind,text,listing_id,requirement_id,client_name,embedding,ts)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (broker_id, kind, text, listing_id, requirement_id, client_name,
             json.dumps(embedding) if embedding else None, ts or now())).lastrowid


def list_notes(broker_id: str, limit: int = 500) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM notes WHERE broker_id=? ORDER BY ts DESC, id DESC LIMIT ?",
                         (broker_id, limit)).fetchall()
    out = [dict(r) for r in rows]
    for n in out:
        n["embedding"] = json.loads(n["embedding"]) if n["embedding"] else None
    return out


DEFAULT_SETTINGS = {"enabled": True, "business_start": "09:00", "business_end": "19:00", "interval_hours": 3,
                    "eod_time": "19:00", "snooze_until": None, "last_ping": None, "last_eod_date": None,
                    "awaiting": None}


def get_settings(broker_id: str) -> dict:
    with conn() as c:
        r = c.execute("SELECT data FROM settings WHERE broker_id=?", (broker_id,)).fetchone()
    return {**DEFAULT_SETTINGS, **(json.loads(r["data"]) if r else {})}


def update_settings(broker_id: str, **changes: Any) -> dict:
    s = {**get_settings(broker_id), **changes}
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO settings(broker_id,data) VALUES(?,?)", (broker_id, json.dumps(s)))
    return s


def brokers(brokerage_id: Optional[str] = None) -> list[dict]:
    q, a = "SELECT * FROM brokers", []
    if brokerage_id:
        q, a = q + " WHERE brokerage_id=?", [brokerage_id]
    with conn() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY id", a).fetchall()]


def get_broker(bid: str) -> Optional[dict]:
    with conn() as c:
        r = c.execute("SELECT * FROM brokers WHERE id=?", (bid,)).fetchone()
    return dict(r) if r else None


def add_broker(bid: str, name: str, brokerage_id: Optional[str] = None, role: str = "broker",
               phone: Optional[str] = None) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO brokers(id,name,brokerage_id,role,phone) VALUES(?,?,?,?,?)",
                  (bid, name, brokerage_id, role, phone))


def add_brokerage(bid: str, name: str) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO brokerages(id,name) VALUES(?,?)", (bid, name))


def get_brokerage(bid: Optional[str]) -> Optional[dict]:
    if not bid:
        return None
    with conn() as c:
        r = c.execute("SELECT * FROM brokerages WHERE id=?", (bid,)).fetchone()
    return dict(r) if r else None


def digits(phone: Optional[str]) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())[-10:]


def broker_by_phone(phone: str) -> Optional[dict]:
    d = digits(phone)
    if not d:
        return None
    return next((b for b in brokers() if digits(b.get("phone")) == d), None)


def team_ids(bid: str) -> list[str]:
    """Brokers in the same brokerage (including this one). A broker with no brokerage is alone."""
    b = get_broker(bid)
    if not b or not b.get("brokerage_id"):
        return [bid]
    return [x["id"] for x in brokers(b["brokerage_id"])]


def visible_listings(bid: str) -> list[dict]:
    """Own listings + listings teammates explicitly shared. Everything else stays private."""
    out = list_listings(bid)
    for t in team_ids(bid):
        if t == bid:
            continue
        name = (get_broker(t) or {}).get("name", t)
        for l in list_listings(t):
            if l.get("shared"):
                l["shared_by"] = name
                out.append(l)
    return out


def get_listing_any(bid: str, lid: int) -> Optional[dict]:
    return next((l for l in visible_listings(bid) if l["id"] == lid), None)


# ---- outbound WhatsApp (share to client / self)
def touch_inbound(phone: str) -> None:
    """Remember that this number messaged our business number: opens the 24h free-form window."""
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO wa_inbound(phone,ts) VALUES(?,?)", (phone, now()))


def last_inbound(phone: str) -> Optional[str]:
    with conn() as c:
        r = c.execute("SELECT ts FROM wa_inbound WHERE phone=?", (phone,)).fetchone()
    return r["ts"] if r else None


def log_share(broker_id: str, to_phone: str, mode: str, status: str, text: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO shares(broker_id,to_phone,mode,status,text,ts) VALUES(?,?,?,?,?,?)",
                  (broker_id, to_phone, mode, status, text, now()))


def shares_since(broker_id: str, since_iso: str) -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) n FROM shares WHERE broker_id=? AND ts>=? AND status='sent'",
                         (broker_id, since_iso)).fetchone()["n"]


def list_shares(broker_id: str, limit: int = 30) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM shares WHERE broker_id=? ORDER BY id DESC LIMIT ?",
                                           (broker_id, limit)).fetchall()]


# ---- WhatsApp groups (per broker)
def group_touch(bid: str, name: str, outcome: str) -> dict:
    """outcome: surfaced | ignored | duplicate | muted. Only counters are kept for ignored messages."""
    col = {"surfaced": "surfaced", "ignored": "ignored", "duplicate": "duplicates", "muted": "ignored"}[outcome]
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO groups(broker_id,name) VALUES(?,?)", (bid, name))
        c.execute(f"UPDATE groups SET seen=seen+1, {col}={col}+1, last_seen=? WHERE broker_id=? AND name=?",
                  (now(), bid, name))
        return dict(c.execute("SELECT * FROM groups WHERE broker_id=? AND name=?", (bid, name)).fetchone())


def get_group(bid: str, name: str) -> Optional[dict]:
    with conn() as c:
        r = c.execute("SELECT * FROM groups WHERE broker_id=? AND name=?", (bid, name)).fetchone()
    return dict(r) if r else None


def list_groups(bid: str) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM groups WHERE broker_id=? ORDER BY name", (bid,)).fetchall()]


def set_group_muted(bid: str, name: str, muted: bool) -> None:
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO groups(broker_id,name) VALUES(?,?)", (bid, name))
        c.execute("UPDATE groups SET muted=? WHERE broker_id=? AND name=?", (int(muted), bid, name))


def seen_before(bid: str, h: str) -> bool:
    """Record a message fingerprint; True if this broker already saw it (repeated broadcasts)."""
    with conn() as c:
        if c.execute("SELECT 1 FROM group_seen WHERE broker_id=? AND hash=?", (bid, h)).fetchone():
            return True
        c.execute("INSERT INTO group_seen(broker_id,hash,ts) VALUES(?,?,?)", (bid, h, now()))
    return False
