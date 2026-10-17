# Broker Assistant: web app + WhatsApp (all four phases)

A **web app** where a broker chats with the assistant (type, attach photos/PDFs, record voice notes) and has
Inventory, Requirements, Notes, Groups and Admin tabs. Works on a phone (Chat/Data switch). Any answer or
listing can be **sent to WhatsApp** (to the broker themselves or to a client). The same brain also answers
**inside WhatsApp** through the webhook, so brokers can use either.

## Data views
Inventory is searchable, filterable (type, attention, sale/rent), sortable and grouped by type, with compact
two-line rows that expand in place for full details and actions. Requirements show live top matches per client.
Notes and Activity are grouped by day. Panels update in place (no rebuild on refresh), so scroll position, filters,
expanded rows and half-typed text survive. Tests: `tests/test_views.py`.

## Deploy: backend on FastAPI Cloud, frontend on Vercel

```
Browser (Vercel: static/) --HTTPS + x-access-key--> FastAPI Cloud (app/)  -->  Azure OpenAI / Azure Speech
Meta WhatsApp  ------------- webhook ------------->  FastAPI Cloud (/webhook/whatsapp)
```

**1. Backend (FastAPI Cloud).** From the repo root: `pip install "fastapi[standard]"`, `fastapi login`, `fastapi deploy`.
The entrypoint is declared in `pyproject.toml` (`app.main:app`). Set these environment variables in the FastAPI Cloud
dashboard (names only here; values come from your `.env`, never from git):

| Variable | Purpose |
|---|---|
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION`, `AZURE_OPENAI_CHAT_DEPLOYMENT`, `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | the AI |
| `AZURE_SPEECH_KEY`, `AZURE_SPEECH_ENDPOINT`, `AZURE_SPEECH_LOCALES` | voice notes |
| `APP_ACCESS_KEY` | **required**: the access code. Without it the API refuses to serve on a public host |
| `CORS_ORIGINS` | your Vercel URL, e.g. `https://realest.vercel.app` |
| `SEED_DEMO` | `0` to start empty |
| `WHATSAPP_*` | only when connecting the business number |

**2. Frontend (Vercel).** Import the repo, set **Root Directory = `static`**, framework "Other". Add the env var
`API_BASE` = your FastAPI Cloud URL. `static/vercel.json` writes it into `config.js` at build time. Then add the Vercel URL
to `CORS_ORIGINS` on the backend and redeploy it.

**3. First visit.** The app asks for the access code once and remembers it in that browser.

**Known limits before real users**
- **Storage is SQLite on the server's disk.** If the host's disk is not persistent, data resets on redeploy or restart.
  Move to Postgres before trusting it with real inventory.
- **One shared access code**, not per-broker logins: anyone with the code can act as any broker. Real brokers should be
  identified by their WhatsApp number (webhook) and the owner admin should get a proper login.
- The WhatsApp webhook is not covered by the access code (Meta cannot send it); set `WHATSAPP_APP_SECRET` so its
  signature is verified.
- No rate limiting yet: the access code is what protects your AI spend.

## Sending to WhatsApp from the app
Every assistant reply and every listing card has "↗ Send to WhatsApp". Two modes:
- **Open in WhatsApp** (works with zero setup): opens a WhatsApp chat with the text pre-filled; the broker taps send.
- **Send from business number**: sent by the server through the WhatsApp Business API (needs `WHATSAPP_TOKEN` and
  `WHATSAPP_PHONE_ID` in `.env`). Inside the 24h window it is a normal message; otherwise WhatsApp only allows an
  approved **template** (`WHATSAPP_SHARE_TEMPLATE`, one `{{1}}` variable). Limited to 30 sends/hour/broker and logged.

Messages for **clients** are built from the database, not copied from the chat: owner name/contact, price
flexibility and private notes are left out (`app/share.py`, covered by `tests/test_share.py`).

The business-number send path is tested against a fake API only; it has **not** been run against Meta yet.

## Run
```
py -3.11 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env        # fill in Azure endpoint, key, deployment names
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```
Open http://127.0.0.1:8000. Two demo brokers (Rohit, Anita) show data isolation. "Try a sample message"
loads realistic Gurgaon / Hinglish messages.

## Azure OpenAI setup
- `AZURE_OPENAI_CHAT_DEPLOYMENT`: a vision + JSON-mode chat model (gpt-4o, gpt-4.1, ...).
- `AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT`: `gpt-4o-transcribe` or `whisper` (voice notes).

## Design
| Layer | File | Notes |
|---|---|---|
| LLM | `app/llm.py` | One call per message: classify + extract (multi-listing split, confidence per field). Reads images natively; voice via speech-to-text; PDFs via text extraction. |
| Normalisation | `app/normalize.py` | **All maths in code**: gaj/sq ft/marla, cr/lakh, per-gaj vs total, controlled vocabularies, Hinglish synonyms, price sanity guard. |
| Matching | `app/matching.py` | Hard filters -> must-haves -> nice-to-have ranking, one-line "fits / lacks". Pure code (ms, not seconds). |
| Conversation | `app/engine.py` | Applies actions, composes replies from DB values (never from LLM text), dedup, history, freshness. |
| Storage | `app/db.py` | SQLite; every row has `broker_id`. Swap for Postgres + pgvector later. |
| API/UI | `app/main.py`, `static/` | `/api/chat` is the only channel-specific piece: replace with a WhatsApp Cloud API webhook. |

## Tests
- `tests/test_engine.py`: offline (no key). Units, prices, dedup, matching, isolation, freshness.
- `tests/run_eval.py`: real model vs `samples/messages.json` expectations; prints field accuracy (target 90%).
  Add the client's real anonymised messages to the samples file to measure it properly.

## Phase 3 (added)
| Feature | Where | Notes |
|---|---|---|
| 3-hour check-ins, EOD prompt, follow-up reminders | `app/scheduler.py` | `tick()` runs every minute. IST business hours, snooze ("don't ping me till 4 pm"), interval change, on/off. Messages sent outside the 24h window are flagged **template** (the UI shows a badge), as WhatsApp requires. |
| Activity log | `app/phase3.py` | Replies to check-ins (text/voice, Hinglish) become notes linked to the right listing and client; "call owner Friday" becomes a task with a reminder; preference changes update the client's requirement. |
| End-of-day report | `build_eod` | Counts come from the DB; the LLM only writes tomorrow's priorities. Also on demand: "give me today's summary". |
| Recall | `recall_candidates` | Entity/date filter -> vector search (`text-embedding-3-small`) blended with keyword overlap -> LLM answers from the top notes only. |
Demo buttons in the UI ("Scheduler demo") fire a check-in / EOD / reminders instantly. Tests: `tests/test_phase3.py` (offline).

## Phase 4 (added): groups + multi-broker

### WhatsApp groups (`app/groups.py`)
The official WhatsApp Business API number **cannot be added to ordinary groups**. Two routes in:
1. **Forwarding (recommended, supported):** the broker forwards a group message to the bot -> normal intake.
2. **Relay (optional, carries ban risk under Meta's terms):** a separate listener number/device POSTs each group
   message to `POST /api/group/ingest` (`{broker_id, group, sender, text}`; optional `GROUP_INGEST_TOKEN`).
   The risk is the client's to accept; nothing in this codebase automates WhatsApp Web itself.

Relay messages are **filtered**, because nobody asked for them: free pre-filter (no digit and no property word =
chatter, no AI call) -> fingerprint de-dupe of repeated broadcasts -> one model call that returns nothing for
greetings, news, spam, "sold/thanks" and vague questions. Genuine requirements/listings become one short alert,
e.g. "New requirement in [group]: 3 BHK floor, Sushant Lok 1, 7-8 cr. You have 1 match. View?" (reply *yes*).
Known listings seen again are merged silently and count as a re-confirmation. Ignored messages are **never
stored** (only a counter and a one-way fingerprint). Groups can be muted per broker.

### Teams and admin (`app/admin.py`, `app/whatsapp.py`)
- Brokerages have an **owner** and brokers. Everything is **private by default**; a broker can share a listing
  ("share #4 with the team") into a team pool that teammates see, search and get alerted about, read-only.
  Other brokerages are never visible.
- **Owner admin** (Admin tab): team overview, CSV/JSON export of the whole brokerage (exports are logged), add a broker.
- **Identity = WhatsApp number.** `POST /webhook/whatsapp` (Meta Cloud API format) routes each sender's number to
  their own account; unknown numbers get a refusal and create no data. Idempotent on message id (Meta retries),
  signature-checked when `WHATSAPP_APP_SECRET` is set. The simulator's broker selector stands in for this.

### Safety nets added while testing against the real model
- **Number grounding:** a price/size the model returns must appear in the broker's message. If not (the model
  once copied "12.8" from an existing listing instead of reading "3.2 lakh per gaj"), it is repaired from the text
  when the message holds exactly one price expression, otherwise flagged "Not sure about: price".
- Group posts are extracted with **no inventory context**, so nothing can leak in from it.

## Tests
- Offline: `tests/test_engine.py`, `test_phase3.py`, `test_phase4.py` (no key needed).
- Real model: `tests/run_eval.py` (extraction, currently ~95% on 15+ synthetic messages) and
  `tests/run_group_eval.py` (group filter, 15/15 on a synthetic feed). These samples are small and written by us:
  they prove the pipeline works, **not** the 90% target on real broker traffic. Add the client's anonymised messages.

## Not in the prototype / before production
- **Not exercised against Meta:** webhook media download and sending (needs `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_ID`,
  approved templates). Parsing, routing, idempotency are unit-tested.
- **Auth is a stub:** the simulator trusts the `broker_id` it is given. Production must derive identity from the
  WhatsApp number and put the admin behind a real login.
- SQLite -> Postgres + pgvector; `/api/poll` -> push; in-process scheduler -> job queue.
- Privacy: owner/client phone numbers are personal data (India's DPDP Act): consent, retention limits, deletion
  on request and encryption at rest still need to be designed with the client.
