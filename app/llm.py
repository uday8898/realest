"""Azure OpenAI wrapper: message understanding (classify + extract) and voice transcription."""
import base64
import json
import os
from datetime import date
from typing import Optional

from dotenv import load_dotenv

from . import vocab

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

_client = None


def configured() -> bool:
    return bool(os.environ.get("AZURE_OPENAI_API_KEY") and os.environ.get("AZURE_OPENAI_ENDPOINT"))


def client():
    global _client
    if _client is None:
        from openai import AzureOpenAI
        _client = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2025-03-01-preview"),
        )
    return _client


LISTING_SHAPE = """{
  "type": "plot|builder_floor|apartment", "purpose": "sale|rent", "micro_market": str, "address": str,
  "facing": one of FACING, "road_width_ft": number, "park_view": bool, "corner": bool,
  "price": {"value": number, "unit": "cr|lakh|thousand|rupees|null", "basis": "total|per_gaj|per_sqft|per_month|null"},
  "price_flexibility": "firm|some|high", "owner_name": str, "owner_contact": str,
  "size": {"value": number, "unit": "gaj|sq ft|sq m|marla|kanal"},   // plot size (plots and builder floors)
  "dimensions": "e.g. 50x45 ft", "shape": "regular|irregular", "vacant_or_built": "vacant|built",
  "floor_number": int, "parking_type": "stilt|covered|open|basement|none", "lift": bool,
  "construction_grade": "premium|standard|old", "furnishing": "unfurnished|semi-furnished|fully-furnished",
  "plot_share_included": bool, "bedrooms": int,
  "society": str, "tower_unit": str, "carpet": {"value":n,"unit":"sq ft"}, "super": {"value":n,"unit":"sq ft"},
  "parking_slots": int, "maintenance": str,
  "source": "who this came from: owner / another broker / group name, if stated",
  "notes": "free text: soft knowledge only (flexibility quotes, power backup, owner mood...)",
  "verified_fields": [field names the BROKER personally checked],
  "confidence": {"<field>": 0.0-1.0 for EVERY field you filled; use <0.7 when guessing or ambiguous}
}"""

REQ_SHAPE = """{
  "type": "plot|builder_floor|apartment|null", "purpose": "sale|rent", "client_name": str,
  "micro_markets": [str], "budget_min": {"value":n,"unit":"cr|lakh"}, "budget_max": {"value":n,"unit":"cr|lakh"},
  "size_min": {"value":n,"unit":"gaj"}, "size_max": {"value":n,"unit":"gaj"}, "bedrooms": int,
  "must_haves":    {facing:[...], park_view:bool, corner:bool, lift:bool, construction_grade:[...],
                    furnishing:[...], parking_type:[...], shape:str, vacant_or_built:str,
                    plot_share_included:bool, min_road_width_ft:n, min_floor:n, max_floor:n},
  "nice_to_haves": {same keys as must_haves},
  "timeline": str, "decision_maker": str, "motivation": str, "notes": str
}"""

SYSTEM = """You are the language brain of a WhatsApp assistant for real-estate brokers in Gurgaon, India.
The broker's message may be English, Hindi or Hinglish; it may be a forwarded broadcast, a transcribed voice
note, text read from a PDF, or an image (property photo, screenshot, handwritten note).
Today is {today}.

Turn the message into a JSON object {{"actions": [...]}}. One message can produce several actions
(e.g. a broadcast with 5 plots -> 5 save_listing actions: SPLIT THEM).

ACTION TYPES
1. {{"intent":"save_listing","listing":LISTING}}            new inventory (plot / builder floor / apartment)
2. {{"intent":"save_requirement","requirement":REQ}}        a CLIENT is looking for something (buyer/tenant needs)
3. {{"intent":"update_listing","target_id":int,"updates":LISTING-fields-to-change}}
   broker corrects/adds info to an existing listing ("change the price of the Sushant Lok floor to 4.2 cr",
   "owner is Rakesh 98xxx"). Resolve target_id from CONTEXT: a named one, else LAST_SAVED if it is clearly about it.
4. {{"intent":"update_requirement","target_id":int,"updates":REQ-fields-to-change}}
5. {{"intent":"set_status","target_id":int,"status":"available|under_negotiation|sold|rented|on_hold"}}
   (also "still available" => status available, which refreshes last-confirmed)
6. {{"intent":"close_requirement","target_id":int}}
7. {{"intent":"search","criteria":REQ,"refine":bool}}      broker asks to FIND/MATCH ("find me a 3 BHK ...").
   If it is a follow-up on the previous search ("anything cheaper?", "also show Sushant Lok 2") set refine=true
   and output the FULL updated criteria starting from LAST_SEARCH_CRITERIA.
8. {{"intent":"show","result_index":int|null,"listing_id":int|null,"what":"details|photos"}}   ("show the second one's photos")
9. {{"intent":"attach_photo","target_id":int}}   an image of a property with no other info: attach to LAST_SAVED.
10. {{"intent":"note","text":str,"listing_id":int|null,"requirement_id":int|null}}  free remark / visit log / client feedback.
    If the note changes a client's preferences ("wife wants park view") ALSO emit update_requirement.
11. {{"intent":"task","text":str,"due":"YYYY-MM-DD or null"}}   a follow-up ("call owner Friday")
12. {{"intent":"confirm"}}        broker confirms values the bot flagged as uncertain ("ok", "yes", "haan sahi hai")
13. {{"intent":"stale_check"}}    broker asks what listings need re-confirming / what is stale
14. {{"intent":"chat","reply":str}}   greetings, help, anything else. Short, friendly, plain text.
15. {{"intent":"activity_log","eod":bool,"skipped":bool,"entries":[ENTRY]}}   the broker reports what they did (visits, calls,
    discussions) — typically replying to AWAITING check-ins, but also any "I visited / spoke to ..." message.
    ENTRY = {{"kind":"visit|call|discussion|note","summary":"one clear English sentence","listing_id":int|null,
    "requirement_id":int|null,"client_name":str|null,"follow_ups":[{{"text":str,"due":"YYYY-MM-DD"|null}}]}}
    Link listing_id / requirement_id using CONTEXT ("the Golf Course Road floor" -> its #id; "Mr. Kapoor" -> his requirement #id).
    One ENTRY per distinct event. Put every "call X on Friday"-style next step in follow_ups with a resolved date.
    If the broker says skip / nothing happened: entries=[] and skipped=true. eod=true only when replying to the end-of-day prompt.
    If a remark changes a client's preferences ("wife wants park view"), ALSO emit update_requirement.
    ALWAYS use activity_log (never note or task) for any message that reports a visit, call or discussion, and for any
    reply when AWAITING is set to a check-in. Its follow_ups replace separate task actions.
16. {{"intent":"eod_report"}}   broker asks for today's summary / report on demand.
17. {{"intent":"recall","question":str,"listing_id":int|null,"client_name":str|null,"since":"YYYY-MM-DD"|null,"until":"YYYY-MM-DD"|null}}
    broker asks about something said/noted earlier ("what did the owner of the Golf Course Road floor say about price?",
    "what did I discuss with Mr. Gupta last week?"). Resolve dates from today's date; resolve listing/client from CONTEXT.
18. {{"intent":"settings","changes":{{"interval_hours":n,"snooze_until":"HH:MM 24h","business_start":"HH:MM","business_end":"HH:MM",
    "eod_time":"HH:MM","reminders":"on|off"}}}}   "don't ping me till 4 pm" -> snooze_until "16:00"; "every 2 hours" -> interval_hours 2.
19. {{"intent":"task_done","target_id":int,"outcome":str,"follow_ups":[{{"text":str,"due":str|null}}]}}   outcome of a follow-up task
    (see OPEN FOLLOW-UP TASKS / AWAITING). Use it when the broker reports how a reminded follow-up went or says "#3 done".
    Use intent 11 ("task") only for NEW stand-alone follow-ups.
20. {{"intent":"share","target_ids":[int],"all":bool,"shared":bool}}   broker shares ("share #4 with the team", "share all my
    listings") or un-shares ("stop sharing #4", "make #4 private") listings with their brokerage team pool.
21. {{"intent":"view_alert"}}   only when AWAITING says the bot sent a group alert ending in "View?" and the broker says
    yes / view / show / dikhao.

LISTING = {listing_shape}

REQ = {req_shape}

RULES
- Extract ONLY what is stated. Unknown => omit the key. NEVER guess; if unsure, fill it but give confidence < 0.7.
- Do NOT do currency or unit maths. Copy the number and unit as written (see glossary).
- Distinguish inventory (someone HAS a property to sell/rent) from a requirement (a client WANTS one).
- "Must be / needs / only" => must_haves; "prefer / ideally / would like / wife wants" => nice_to_haves.
- A budget "7 to 8 cr" => budget_min 7 cr, budget_max 8 cr; "under 8 cr" => budget_max only.
- Fill EVERY field the text supports: "2BHK"/"3 BHK" -> bedrooms (apartments too); "1300 sq ft super" -> "super"
  {{"value":1300,"unit":"sq ft"}}; "TF" = top/third floor -> floor_number 3; "stilt+4" -> parking_type stilt.
- A message that logs a visit/discussion AND mentions a follow-up needs BOTH a "note" action and a "task" action.
- Output JSON only, no commentary.

GLOSSARY
{glossary}
Allowed micro-markets (map to these names when the text clearly means one): {markets}
"""


def build_system() -> str:
    return SYSTEM.format(
        today=date.today().isoformat(), listing_shape=LISTING_SHAPE, req_shape=REQ_SHAPE,
        glossary=vocab.GLOSSARY, markets=", ".join(vocab.MICRO_MARKETS))


def understand(text: str, images: Optional[list[tuple[str, bytes]]] = None, context: str = "") -> dict:
    """images: list of (mime, bytes). Returns {"actions": [...]}."""
    content: list[dict] = [{"type": "text", "text": f"CONTEXT\n{context}\n\nBROKER MESSAGE\n{text or '(no text, see image)'}"}]
    for mime, data in images or []:
        b64 = base64.b64encode(data).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    deployment = os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o")
    last_err = None
    for _ in range(2):
        resp = client().chat.completions.create(
            model=deployment, temperature=0, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": build_system()}, {"role": "user", "content": content}])
        try:
            out = json.loads(resp.choices[0].message.content)
            if isinstance(out.get("actions"), list):
                return out
        except (json.JSONDecodeError, TypeError) as e:
            last_err = e
    raise RuntimeError(f"LLM returned unparseable output: {last_err}")


TRANSCRIBE_HINT = ("Real estate in Gurgaon. Hindi, English and Hinglish: gaj, crore, lakh, builder floor, "
                   "DLF Phase 1, Sushant Lok, Golf Course Road, park facing, corner plot, stilt, 3 BHK.")


def _speech_transcribe(filename: str, data: bytes, mime: str) -> str:
    """Azure AI Speech 'fast transcription' REST API with multiple candidate locales (Hinglish-friendly)."""
    import httpx
    locales = [x.strip() for x in os.environ.get("AZURE_SPEECH_LOCALES", "en-IN,hi-IN").split(",") if x.strip()]
    url = os.environ["AZURE_SPEECH_ENDPOINT"].rstrip("/") + "/speechtotext/transcriptions:transcribe?api-version=2024-11-15"
    r = httpx.post(url, headers={"Ocp-Apim-Subscription-Key": os.environ["AZURE_SPEECH_KEY"]},
                   files={"audio": (filename, data, mime), "definition": (None, json.dumps({"locales": locales}))},
                   timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"Speech API {r.status_code}: {r.text[:300]}")
    phrases = r.json().get("combinedPhrases") or []
    return " ".join(p.get("text", "") for p in phrases).strip()


def transcribe(filename: str, data: bytes, mime: str) -> str:
    if os.environ.get("AZURE_SPEECH_KEY") and os.environ.get("AZURE_SPEECH_ENDPOINT"):
        return _speech_transcribe(filename, data, mime)
    deployment = os.environ.get("AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT", "gpt-4o-transcribe")
    resp = client().audio.transcriptions.create(model=deployment, file=(filename, data, mime),
                                                prompt=TRANSCRIBE_HINT)
    return (resp.text or "").strip()


def chat_text(system: str, user: str, max_tokens: int = 500) -> str:
    """Plain-text completion (recall answers, end-of-day priorities)."""
    resp = client().chat.completions.create(
        model=os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o"), temperature=0.2, max_tokens=max_tokens,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    return (resp.choices[0].message.content or "").strip()


def embed(texts: list[str]) -> Optional[list[list[float]]]:
    """Vector embeddings for semantic recall. Returns None when no embedding deployment is configured
    (recall then falls back to lexical ranking + LLM reading of the shortlisted notes)."""
    dep = os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")
    if not dep or not configured():
        return None
    try:
        return [d.embedding for d in client().embeddings.create(model=dep, input=texts).data]
    except Exception:
        return None
