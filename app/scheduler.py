"""Proactive messages: 3-hour check-ins, end-of-day prompt, follow-up reminders.

`tick()` is a pure function of (DB state, current time) so it is unit-testable; main.py just calls it
every minute. WhatsApp rule modelled here: outside the 24h customer-service window the bot may only send a
pre-approved *template*, so such messages are flagged `template` (the UI shows a badge). In production the
template is a short "tap to continue" nudge and the full text goes out once the broker replies.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import db

IST = timezone(timedelta(hours=5, minutes=30))  # India has no DST
WINDOW = timedelta(hours=24)
EOD_QUIET_MIN = 45  # no 3-hour ping this close to the end-of-day prompt


def ist(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(IST)


def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def at(day: datetime, hm: str) -> datetime:
    h, m = _hm(hm)
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def _parse(ts: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(ts) if ts else None


def window_open(broker_id: str, now: datetime) -> bool:
    last = _parse(db.last_broker_message_ts(broker_id))
    return bool(last and now - last < WINDOW)


def send(broker_id: str, text: str, kind: str, now: datetime, awaiting: Optional[dict] = None) -> dict:
    """Store a proactive bot message (the simulator UI polls for it)."""
    template = not window_open(broker_id, now)
    media = [{"kind": "template", "name": kind}] if template else []
    db.add_message(broker_id, "bot", text, media)
    db.update_settings(broker_id, awaiting=awaiting)
    return {"broker": broker_id, "kind": kind, "template": template, "text": text}


def ping_text(now: datetime, hours: float) -> str:
    h = int(hours) if float(hours).is_integer() else hours
    return (f"It's {_t12(now)}. "
            f"What happened in the last {h} hours? Any visits, calls, discussions or notes?\n"
            f"Reply by text or voice note — or say *skip*.")


def _t12(d: datetime) -> str:
    return f"{d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def tick(now: Optional[datetime] = None, force: Optional[str] = None, only: Optional[str] = None) -> list[dict]:
    """Send whatever is due. `force` ('ping' | 'eod' | 'tasks') ignores timing (demo button)."""
    now = now or datetime.now(timezone.utc)
    local = ist(now)
    sent: list[dict] = []
    for b in db.brokers():
        bid = b["id"]
        if only and bid != only:
            continue
        s = db.get_settings(bid)
        if not s["enabled"] and not force:
            continue
        snoozed = bool(_parse(s["snooze_until"]) and _parse(s["snooze_until"]) > now)
        start, end, eod = at(local, s["business_start"]), at(local, s["business_end"]), at(local, s["eod_time"])
        in_hours = start <= local < end

        # follow-up reminders (due today or overdue), once each
        if force == "tasks" or (not force and in_hours and not snoozed):
            for t in reversed(db.list_tasks(bid, only_open=True)):
                due = (t["due"] or "")[:10]
                if t["reminded"] or not due or due > local.date().isoformat():
                    continue
                db.update_task(bid, t["id"], reminded=1)
                sent.append(send(bid, f"Reminder: {t['text']} (due {due}). How did it go?", "task_reminder",
                                 now, {"kind": "task", "id": t["id"]}))

        # end-of-day prompt
        if force == "eod" or (not force and local >= eod and s["last_eod_date"] != local.date().isoformat()
                              and local < eod + timedelta(hours=4) and not snoozed):
            db.update_settings(bid, last_eod_date=local.date().isoformat())
            sent.append(send(bid, "End-of-day check-in. How was today? Tell me about deals that moved, visits done, "
                                  "anything to remember — text or voice note. I'll send back a summary and tomorrow's priorities.",
                             "eod_prompt", now, {"kind": "eod"}))
            continue

        # 3-hour activity check-in
        if force == "ping" or (not force and in_hours and not snoozed and eod - local > timedelta(minutes=EOD_QUIET_MIN)):
            last = _parse(s["last_ping"])
            anchor = max(last, start.astimezone(timezone.utc)) if last else start.astimezone(timezone.utc)
            if force == "ping" or now - anchor >= timedelta(hours=float(s["interval_hours"])):
                db.update_settings(bid, last_ping=now.isoformat(timespec="seconds"))
                sent.append(send(bid, ping_text(local, s["interval_hours"]), "activity_ping", now, {"kind": "activity"}))
    return sent
