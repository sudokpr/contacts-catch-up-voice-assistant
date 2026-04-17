# AGENTS.md — RelayAI Relationship Manager

This file provides context for coding assistants working in this repository. Read it before making any changes.

---

## What This Project Is

A locally-hosted Python/FastAPI application that acts as an **autonomous AI relationship manager** for both personal contacts and business partners. It places proactive outbound voice calls on your behalf — for regular catch-ups, birthdays, anniversaries, festival occasions, deal celebrations, promotion congratulations, and promised follow-ups.

Key pillars:
- **Scoring engine** — daily cron ranks contacts by overdue-ness and calls the top ones
- **Occasion detection** — birthday/anniversary (from contact fields), festivals (hardcoded MM-DD dict)
- **News monitoring** — LinkedIn/social fixture adapters flag deal-news and promotions → trigger calls
- **CRM integration** — mock CRM adapter (fixture-based) detects deal closures with contacts → call + gift
- **Gifting service** — mock orders for flowers, custom mugs, and sweet boxes; delivery status injected into calls
- **Commitment tracking** — call summaries scanned for phrases like "next quarter" → auto-schedule follow-up
- **Tone adaptation** — `relationship_type` field drives `tone_instructions` variableValue (casual vs professional)
- **Vapi** — single assistant with dynamic `{{variable}}` injection for all per-call context

This is a **hackathon POC** — optimise for working demo over production robustness. Mocked external services (social feeds, CRM, gifting, calendar) are intentional and correct.

---

## Architecture at a Glance

- **FastAPI HTTP server** — SPA (`/`), contact API (`/api/contacts`), call trigger + SSE stream (`/api/calls`), Vapi tool endpoints (`/api/calls/tools/*`), webhook handler (`/webhook/vapi`)
- **APScheduler background workers** — daily cron (scoring + call trigger) and 5-minute polling loop (callback checks + stale call sweep)
- **SSE pub/sub bus** (`app/sse_bus.py`) — webhook publishes events; `/api/calls/live/{id}` streams them to the browser UI in real time
- **External services** — Vapi (voice calls), Qdrant Cloud (vector memory), OpenAI-compatible LLM (fallback classification + embeddings)

Two datastores: **SQLite** holds structured contact records; **Qdrant** holds semantic memory entries (embeddings of highlights, facts, summaries, and social updates per contact).

---

## Directory Structure

```
app/
  config.py              # pydantic-settings env var loading
  main.py                # FastAPI app factory; registers routers; startup lifespan
  db.py                  # aiosqlite connection; init_db(); JSON helpers
  sse_bus.py             # In-process async pub/sub for live call SSE events
  models/
    contact.py           # Contact, TimeWindow, SocialHandles Pydantic models
    memory.py            # MemoryEntry, ExtractionResult, CallbackIntent models
  routes/
    contacts.py          # /api/contacts CRUD + /api/contacts/{id}/memories
    calls.py             # /api/calls/trigger, /api/calls/live SSE, /api/calls/tools/* Vapi tools
    webhook.py           # /webhook/vapi post-call processing (background task)
    dashboard.py         # Serves app/static/index.html for all SPA routes
  services/
    scoring.py           # Deterministic scoring engine
    vapi.py              # Outbound call initiation (phone + SIP); active call guard; variable injection
    qdrant.py            # Memory store; ensure_collection_exists on startup
    embedding.py         # Text embeddings with SHA-256 deterministic fallback
    calendar.py          # Google Calendar or mock
    gifting.py           # Mock gifting service (flowers, mug, sweet_box); gift delivery context
    crm.py               # Mock CRM adapter — fixture-based deal closure detection
    social/
      base.py            # SocialAdapterBase ABC + SocialUpdate model
      fixtures.py        # Fixture data keyed by contact name; DEAL_KEYWORDS, PROMOTION_KEYWORDS
      twitter.py / instagram.py / linkedin.py / ingest.py
  workers/
    scheduler.py         # APScheduler: daily cron, 5-min poller, crash recovery
  static/
    index.html           # Single-page app (Tailwind CSS + vanilla JS, no build step)
    tailwind.js          # Tailwind Play CDN served locally (committed to repo)
scripts/
  setup_vapi.py          # One-shot Vapi provisioning: PSTN number, SIP number, tools, assistant
  seed_contacts.py       # Seed SQLite + Qdrant with sample contacts and memories
tests/
  unit/
  integration/
```

---

## Code Conventions

**Python version:** 3.12+. Use `datetime.now(UTC)` — never `datetime.utcnow()` (deprecated).

**Async:** All service functions are `async`. Use `await` consistently. Do not mix sync blocking calls in async paths.

**Pydantic:** Models live in `app/models/`. Use Pydantic v2. Validate E.164 phone numbers with a field validator on `Contact`. Do not use `dict()` — use `model_dump()`.

**Database:** Raw `aiosqlite` — no ORM. JSON columns (`tags`, `preferred_time_window`, `social_handles`) are serialised with `json.dumps` on write and `json.loads` on read. Helpers in `db.py`.

**Error handling:** Raise typed exceptions (`ConfigurationError`, `VapiError`, `AlreadyOnCallError`). Never swallow exceptions silently — always log before continuing.

**Routes:** `/api/...` routes return JSON only. `/` and `/contacts/*` serve the SPA. This keeps the API decoupled from the frontend.

---

## Key Design Decisions to Preserve

**Webhook returns 200 immediately.** All post-call processing runs in a FastAPI `BackgroundTasks` task. Never do this inline — Vapi has a short webhook timeout.

**Idempotency on webhooks.** A `processed_calls` table in SQLite stores processed `call_id` values. The webhook checks this before processing and skips duplicates.

**Scoring uses `last_spoken`, not `last_called`.** `last_called` is when a call was initiated (may have been unanswered). `last_spoken` is when a real conversation happened. The scoring formula derives `days_since_last_call` from `last_spoken`.

**`_active_calls` is `dict[str, datetime]`, not a set.** Maps `contact_id → call_started_at`. The 5-minute polling loop calls `sweep_stale_active_calls()` which releases any entry older than 30 minutes — prevents contacts getting stuck if a webhook is never delivered.

**Phone vs SIP routing.** `contact.contact_method` is the sole decision field in `initiate_call()`. `"phone"` sends `customer.number`; `"sip"` sends `customer.sipUri`. Both require `phoneNumberId` — use `VAPI_SIP_TRUNK_ID` for SIP contacts (Vapi-native SIP number), `VAPI_PHONE_NUMBER_ID` for PSTN contacts.

**Vapi Web SDK is the default call method.** When `VAPI_PUBLIC_KEY` is set, the browser uses `@vapi-ai/web` (loaded from esm.sh as ESM) to initiate WebRTC calls directly — free, no PSTN charges, and passes `variableValues` correctly. The SDK is loaded in a `<script type="module">` block and exposed as `window.VapiClass`. `triggerCall()` waits for the module to load before deciding: if `window.VapiClass` + `vapiConfig.vapi_public_key` are available, it calls `_startWebCall()` (SDK); otherwise it falls back to `_startBackendCall()` (PSTN/SIP via `/api/calls/trigger/{id}`).

**Web SDK contact_id extraction in webhook.** Backend-initiated calls set `metadata.contact_id`. Web SDK calls pass `contact_id` in `assistantOverrides.variableValues`. The webhook `contact_id` property checks metadata first, then falls back to `call.assistantOverrides.variableValues.contact_id`. This ensures Last Called / Last Outcome are updated for both call types.

**SSE live call flow (PSTN/SIP fallback only).** UI posts to `/api/calls/trigger/{id}` → backend calls Vapi → webhook receives events → publishes to `sse_bus` → `/api/calls/live/{id}` SSE stream delivers them to the browser. The UI has a 45s watchdog timeout in case no webhook arrives.

**Memory retrieval is sorted by recency.** `get_memory` and `search_memory` tool endpoints sort Qdrant results by `timestamp` descending before returning, so the AI always sees the most recent memories first regardless of semantic similarity score.

**Vapi variable injection.** Per-call context is passed via `assistantOverrides.variableValues`. Variables: `user_name`, `contact_id`, `contact_name`, `contact_tags`, `last_call_note`, `occasion_context`, `tone_instructions`, `gift_status`. These substitute `{{variable}}` placeholders in the system prompt. They are NOT persisted on the assistant.

**`relationship_type` drives tone.** `contact.relationship_type` ("personal" | "business") determines `tone_instructions` in `_build_variable_values()`. The single Vapi assistant adapts tone without needing two separate assistants.

**Gift delivery context.** `get_gift_delivery_context(contact_id)` queries `gift_orders` for the most recent gift, compares `delivery_date` to today, and returns a natural-language string ("a bouquet is on its way" / "hope you enjoyed the gift") injected as `{{gift_status}}`.

**Occasion-triggered jobs in scheduler.** `_birthday_anniversary_job()` → birthday/anniversary. `_festival_job()` → hardcoded `_FESTIVALS` dict (MM-DD). `_deal_news_job(contacts)` → scans today's ingested social memories for `DEAL_KEYWORDS`/`PROMOTION_KEYWORDS`. `_crm_deal_job()` → queries mock CRM adapter. All run from `_daily_cron_job()` after the regular scoring calls.

**Commitment detection in webhook.** `process_call_webhook()` scans the call summary for commitment phrases ("next quarter", "next month", etc.). If found, stores a `"commitment"` memory entry and calls `schedule_one_off_call()` to set `next_call_at` the appropriate number of days out.

**Mock CRM adapter pattern.** `app/services/crm.py` follows the same fixture-based pattern as social adapters — `DEAL_FIXTURES` dict keyed by lowercase contact name, `get_closed_deal_today()` function. Swap for real Salesforce/HubSpot API calls in production.

**Vapi summary.** `analysisPlan.summaryPlan` is enabled on the assistant — Vapi's GPT-4 generates the call summary. Our webhook reads it from `payload.analysis.summary`. No local LLM is needed for summarisation.

**Social adapters use fixture data.** No real API credentials or HTTP calls. Fixtures are keyed by `contact.name.lower()` with a `__default__` fallback. This is correct and intentional for the POC.

---

## Scoring Formula

```
score = days_since_last_spoken * 0.6 + category_gap_score * 0.3 + priority_boost * 0.1
```

`days_since_last_spoken` dominates for contacts not spoken to recently. Do not normalise it for the hackathon — it produces correct relative rankings for a small contact list.

---

## Environment Variables

```
VAPI_API_KEY            — Vapi API key (server-side only, never sent to browser)
VAPI_PUBLIC_KEY         — Vapi public key (browser-safe; from Vapi dashboard → Account)
VAPI_ASSISTANT_ID       — Created by setup_vapi.py
VAPI_PHONE_NUMBER_ID    — Vapi PSTN number ID (created by setup_vapi.py)
VAPI_SIP_TRUNK_ID       — Vapi SIP number ID (created by setup_vapi.py, optional)
APP_BASE                — Public server URL (ngrok URL in dev)
USER_NAME               — Your name, injected into every call prompt

QDRANT_API_KEY          — Qdrant Cloud API key
QDRANT_ENDPOINT         — Qdrant Cloud endpoint URL

OPENAI_API_KEY          — OpenAI API key (or "ollama" for local)
OPENAI_BASE_URL         — OpenAI-compatible base URL
OPENAI_MODEL            — Model name (e.g. gpt-4o)

SCHEDULER_DAILY_HOUR    — Hour to fire daily cron (default: 9)
GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN  — optional
```

---

## Install & Run

```bash
source .venv/bin/activate
uv sync --extra dev          # install all dependencies

# One-time Vapi setup (buy phone numbers, create tools and assistant)
python scripts/setup_vapi.py

# Seed sample contacts with memories
python scripts/seed_contacts.py

# Start the server
uvicorn app.main:app --reload --port 8000
```

---

## Testing

Framework: `pytest` + `pytest-asyncio`. Property-based tests use `Hypothesis`.

Mock Vapi HTTP calls using `respx` at the `httpx` boundary — do not make real Vapi API calls in tests.

```bash
uv run pytest tests/unit -v
```
