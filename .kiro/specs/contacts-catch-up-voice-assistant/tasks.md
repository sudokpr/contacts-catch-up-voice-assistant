# Implementation Plan: Contacts Catch-Up Voice Assistant

## Overview

Incremental implementation ordered to get a real end-to-end outbound call working as early as possible (by task 5), then layer in memory, full webhook processing, the scheduler, and finally the dashboard. Social adapters and calendar stub are explicitly marked as polish tasks.

## Tasks

- [x] 1. Project scaffold, configuration, and README skeleton
  - Create the directory structure: `app/`, `app/routes/`, `app/services/`, `app/services/social/`, `app/models/`, `app/workers/`, `app/templates/contacts/`, `tests/unit/`, `tests/integration/`
  - Create `app/config.py` — read all required env vars using `pydantic-settings`; raise `ConfigurationError` with the variable name if any required var is missing at startup
  - Create `app/main.py` — FastAPI app factory, register routers, call `ensure_collection_exists()` and `start_scheduler()` on startup lifespan
  - Create `pyproject.toml` with `uv` as the build tool; runtime deps: `fastapi`, `uvicorn`, `pydantic`, `pydantic-settings`, `sqlalchemy`, `aiosqlite`, `qdrant-client`, `openai`, `apscheduler`, `jinja2`, `python-multipart`, `httpx`; dev deps (optional group): `hypothesis`, `pytest`, `pytest-asyncio`, `respx`
  - Create a skeleton `README.md` with sections: env var setup, how to run, how to expose via ngrok / `vapi listen` (fill in details at task 16)
  - _Requirements: 10.1, 10.2_

  - [x] 1.1 Write property test for missing env var startup failure
    - **Property 20: Missing required environment variable causes startup failure**
    - **Validates: Requirements 10.2**

- [x] 2. Contact data model, SQLite persistence, and DB schema
  - Create `app/models/contact.py` — `TimeWindow`, `SocialHandles`, `Contact` Pydantic models exactly as specified in the design (including `call_started_at` field)
  - Create `app/models/memory.py` — `MemoryEntry`, `CallbackIntent`, `ExtractionResult` Pydantic models; use `datetime.now(UTC)` not `utcnow()`
  - Create `app/db.py` — SQLite connection via `aiosqlite`; `init_db()` creates:
    - `contacts` table with all columns including `call_started_at`
    - `processed_calls` table with `call_id TEXT PRIMARY KEY, processed_at TEXT` (idempotency for webhook)
  - Add JSON serialization/deserialization helpers for `tags`, `preferred_time_window`, `social_handles`
  - _Requirements: 1.1_

  - [ ]* 2.1 Write property test for contact creation round-trip
    - **Property 7: Contact creation round-trip**
    - **Validates: Requirements 1.2**

  - [ ]* 2.2 Write property test for invalid phone rejection
    - **Property 8: Invalid phone number rejected**
    - **Validates: Requirements 1.4**

  - [ ]* 2.3 Write property test for missing required field rejection
    - **Property 9: Missing required field rejected**
    - **Validates: Requirements 1.3**

- [x] 3. Contact API routes
  - Create `app/routes/contacts.py` — implement `POST /api/contacts`, `GET /api/contacts`, `GET /api/contacts/{contact_id}`, `PUT /api/contacts/{contact_id}`, `DELETE /api/contacts/{contact_id}`
  - `DELETE` must call `delete_contact_memories(contact_id)` on the Memory Store before removing the DB record
  - Validate E.164 phone format with a regex validator on the `Contact` model (`^\+[1-9]\d{6,14}$`)
  - _Requirements: 1.2, 1.3, 1.4, 1.5, 1.6_

  - [ ]* 3.1 Write property test for contact deletion removes all data
    - **Property 10: Contact deletion removes all associated data**
    - **Validates: Requirements 1.6**

- [x] 4. Checkpoint — data layer solid
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Vapi outbound call service (stub — hardcoded context, no memory yet)
  - Create `app/services/vapi.py` — implement `initiate_call`, `mark_call_ended`, `sweep_stale_active_calls`
  - `_active_calls: dict[str, datetime]` tracks contact_id → call_started_at (dict, not set — needed for TTL sweep)
  - `initiate_call` raises `AlreadyOnCallError` if contact is in `_active_calls`; persists `call_started_at` to the Contact DB record
  - `sweep_stale_active_calls` releases entries older than 30 minutes
  - Routes to PSTN or SIP based on `contact.contact_method`
  - On Vapi API error: log, set `last_call_outcome = no_answer`, do not raise
  - Add `POST /api/calls/trigger/{contact_id}` in `app/routes/calls.py` — manual trigger endpoint (used for smoke testing before the dashboard exists)
  - _Requirements: 4.1, 4.2, 4.9, 4.10_

  - [x]* 5.1 Write property test for outbound call uses correct contact method
    - **Property 11: Outbound call uses correct contact method**
    - **Validates: Requirements 4.1**

- [x] 6. Webhook handler skeleton — return 200, log transcript
  - Create `app/routes/webhook.py` — implement `POST /webhook/vapi`
  - Return 200 immediately; log the raw payload (transcript + metadata) for inspection
  - Check `processed_calls` table for `call_id` idempotency; insert on first receipt
  - Enqueue `process_call_webhook` via FastAPI `BackgroundTasks` (stub: just log for now)
  - Call `mark_call_ended(contact_id)` at the end of the background task
  - This gives a working end-to-end loop: trigger call → Vapi calls contact → webhook fires → 200 returned
  - _Requirements: 5.1_

- [x] 7. Checkpoint — first real call end-to-end
  - Place a test call via `POST /api/calls/trigger/{contact_id}`, verify Vapi calls the contact, verify the webhook fires and returns 200. Ask the user if questions arise.

- [x] 8. Scoring engine
  - Create `app/services/scoring.py` — implement `compute_score`, `compute_category_gap_scores`, `is_in_call_window`, and `get_top_contacts`
  - `compute_score` uses `contact.last_spoken` (not `last_called`) for `days_since_last_spoken`
  - `get_top_contacts` applies in order: immediate callback override (bypasses recency filter) → exclude contacts where `call_started_at IS NOT NULL` (currently on a call) → recency filter → time-window filter → category balancing → score sort → top 2
  - `is_in_call_window` converts `now` to the contact's IANA timezone before comparing against `preferred_time_window` or the default 09:00–20:00 window
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_

  - [ ]* 8.1 Write property test for scoring formula exactness
    - **Property 1: Scoring formula is exact**
    - **Validates: Requirements 2.1**

  - [ ]* 8.2 Write property test for immediate callback override
    - **Property 2: Immediate callback always wins (overrides recency filter)**
    - **Validates: Requirements 2.2**

  - [ ]* 8.3 Write property test for recency filter
    - **Property 3: Recency filter excludes recently-called contacts**
    - **Validates: Requirements 2.3**

  - [ ]* 8.4 Write property test for category gap score ordering
    - **Property 4: Category gap score ordering**
    - **Validates: Requirements 2.4**

  - [ ]* 8.5 Write property test for time window filter
    - **Property 5: Time window filter (including default window)**
    - **Validates: Requirements 2.5, 2.6**

  - [ ]* 8.6 Write property test for result set size bound
    - **Property 6: Result set size bounded**
    - **Validates: Requirements 2.7**

- [x] 9. Scheduler
  - Create `app/workers/scheduler.py` — implement `start_scheduler` and `schedule_one_off_call`
  - Daily cron job: calls `get_top_contacts`, then `initiate_call` for each result; also calls `ingest_social_updates` for each selected contact
  - 5-minute polling job: queries contacts where `next_call_at <= now()`, calls `initiate_call`; also calls `sweep_stale_active_calls()`
  - On startup: scan for contacts with `next_call_at <= now()` and re-queue (crash recovery for persisted callbacks)
  - `schedule_one_off_call` persists `next_call_at` to SQLite and adds an APScheduler one-off job
  - Wrap all job callbacks in try/except; log errors without crashing the scheduler
  - _Requirements: 3.1, 3.2, 3.3, 3.4_

  - [x]* 9.1 Write unit test for one-off job scheduling
    - Verify that after `schedule_one_off_call`, the contact's `next_call_at` is persisted and the APScheduler job exists
    - _Requirements: 3.3_

- [x] 10. Embedding service and memory store
  - Create `app/services/embedding.py` — `embed(text: str) -> list[float]` calls the nomic-embed-text model via the OpenAI-compatible embeddings endpoint
  - Create `app/services/qdrant.py` — implement `ensure_collection_exists()` (vector_size=768, cosine distance), `store_memory()`, `search_memory()`, `delete_contact_memories()`
  - `store_memory` upserts using `entry_id` as the Qdrant point ID (natural idempotency)
  - `search_memory` filters by `contact_id` payload field before returning results
  - _Requirements: 6.1, 6.2, 6.3, 6.4_

  - [ ]* 10.1 Write property test for memory store round-trip with embedding
    - **Property 16: Memory store round-trip with embedding**
    - **Validates: Requirements 6.1, 6.2**

  - [ ]* 10.2 Write property test for cross-contact isolation
    - **Property 17: Memory search is scoped to contact_id — no cross-contact leakage**
    - **Validates: Requirements 6.4, 4.5**

  - [ ]* 10.3 Write property test for search result count bound
    - **Property 18: Search result count bounded by top_k**
    - **Validates: Requirements 6.3**

- [x] 11. Vapi tool endpoints (backed by real memory)
  - Expand `app/routes/calls.py` with tool endpoints:
    - `POST /tools/get_contact_context` — returns name, last interaction summary (`last_call_note`), tags
    - `POST /tools/get_memory` — calls `search_memory(contact_id, query)` where query is `"{name} {tags_joined} {last_call_note}"` (enriched default context query)
    - `POST /tools/search_memory` — calls `search_memory` with the query string from the request body
    - `POST /tools/save_memory` — calls `store_memory` with the provided text
    - `POST /tools/get_calendar_slots` — delegates to Calendar Service
    - `POST /tools/create_calendar_event` — delegates to Calendar Service
  - All tool endpoints return 404 JSON if contact not found
  - _Requirements: 4.3, 4.4, 4.5, 4.6, 4.7, 4.8_

- [x] 12. LLM extractor
  - Create `app/services/llm.py` — implement `extract_from_transcript(transcript: str) -> ExtractionResult`
  - Use the OpenAI-compatible client pointed at `OPENAI_BASE_URL` with `OPENAI_MODEL`
  - Retry up to 2 times on failure; return empty `ExtractionResult()` after exhausting retries
  - Parse the LLM JSON response into `ExtractionResult`; fall back to empty on parse error
  - _Requirements: 5.3, 5.4, 5.5_

  - [ ]* 12.1 Write property test for LLM extraction schema conformance
    - **Property 13: LLM extraction result conforms to schema**
    - **Validates: Requirements 5.3**

  - [ ]* 12.2 Write unit test for LLM retry and fallback behavior
    - Mock the HTTP client to fail twice, verify empty `ExtractionResult` is returned
    - _Requirements: 5.4, 5.5_

- [ ] 13. Full post-call webhook processing
  - Expand `app/routes/webhook.py` — replace the stub background task with full `process_call_webhook`:
    1. Classify outcome (rule-based on metadata; LLM fallback if ambiguous)
    2. If answered: call `extract_from_transcript`
    3. Store each highlight and fact as a `MemoryEntry` via `store_memory`
    4. Update contact: `last_called`, `last_spoken`, `last_call_outcome`, `last_call_note`, `call_started_at = None`
    5. If `callback.type != none`: parse value, call `schedule_one_off_call`
    6. Call `mark_call_ended(contact_id)`
  - _Requirements: 5.1, 5.2, 5.3, 5.6, 5.7, 5.8_

  - [ ]* 13.1 Write property test for webhook outcome classification
    - **Property 12: Webhook outcome classification always produces a valid value**
    - **Validates: Requirements 5.2**

  - [ ]* 13.2 Write property test for highlights and facts count
    - **Property 14: Highlights and facts count matches stored memory entries**
    - **Validates: Requirements 5.6**

  - [ ]* 13.3 Write property test for contact fields updated after webhook
    - **Property 15: Contact fields updated after webhook processing**
    - **Validates: Requirements 5.7**

- [~] 14. Checkpoint — full webhook processing
  - Ensure all tests pass, ask the user if questions arise.

- [~] 15. Web dashboard routes and templates
  - Create `app/routes/dashboard.py` — HTML routes (separate from `/api/...`):
    - `GET /` → contact list view
    - `GET /contacts/{contact_id}` → contact detail view
    - `GET /contacts/new` → onboarding form
    - `POST /contacts/{contact_id}/call` → manual trigger (calls `initiate_call`, redirects back to detail view)
  - Create `app/templates/base.html` — base layout with nav
  - Create `app/templates/contacts/list.html` — table: name, tags, last called, next call, last outcome
  - Create `app/templates/contacts/detail.html` — highlights, facts, notes, call timeline, manual trigger button
  - Create `app/templates/contacts/form.html` — onboarding form with all required fields
  - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6_

  - [ ]* 15.1 Write unit tests for dashboard route responses
    - Verify each route returns 200 with expected HTML content (contact name present, form fields present)
    - _Requirements: 9.1, 9.2, 9.3_

- [~] 16. Checkpoint — dashboard complete
  - Ensure all tests pass, ask the user if questions arise.

- [~] 17. Social adapters and fixture ingestion (polish)
  - Create `app/services/social/base.py` — `SocialUpdate` model and `SocialAdapterBase` ABC
  - Create `app/services/social/fixtures.py` — `FIXTURES` dict keyed by platform → contact name (lowercased) → list of `SocialUpdate`; include a `__default__` key per platform with generic updates
  - Create `app/services/social/twitter.py`, `instagram.py`, `linkedin.py` — each calls `get_fixture_updates(contact, platform)` from fixtures
  - Create `app/services/social/ingest.py` — `ingest_social_updates(contact)` iterates all adapters, stores each update as a `MemoryEntry` with `type="social"` via `store_memory`
  - The daily cron in `app/workers/scheduler.py` already calls `ingest_social_updates` — no further wiring needed
  - _Requirements: 7.1, 7.2, 7.3, 7.4_

  - [ ]* 17.1 Write property test for social updates stored as type social
    - **Property 19: Social updates stored with type "social"**
    - **Validates: Requirements 7.2**

- [~] 18. Calendar service stub (polish)
  - Create `app/services/calendar.py` — implement `get_free_slots` and `create_event`
  - If Google Calendar credentials are configured: use the Google Calendar API
  - Otherwise: return mock `TimeSlot` and `CalendarEvent` objects
  - _Requirements: 8.1, 8.2, 8.3_

  - [ ]* 18.1 Write unit test for calendar stub mock mode
    - Verify `get_free_slots` returns a non-empty list and `create_event` returns a valid `CalendarEvent` when credentials are absent
    - _Requirements: 8.3_

- [~] 19. Final wiring, tunnel smoke test, and README completion
  - Ensure all routers are registered in `app/main.py`: contacts API, calls/tools, webhook, dashboard
  - Add a `GET /health` endpoint that returns `{"status": "ok"}` for tunnel health checks
  - Start ngrok or `vapi listen` CLI; verify Vapi can reach `POST /webhook/vapi` by placing a test call and confirming the webhook fires
  - Complete the `README.md`: fill in env var descriptions, run instructions, ngrok setup, and how to point Vapi's webhook URL to the tunnel
  - _Requirements: 4.2, 9.5, 10.4_

- [~] 20. Final checkpoint — all tests pass, tunnel verified
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP
- Tasks 17 and 18 are explicitly polish — implement the dashboard (task 15) before these
- Property tests use Hypothesis with `@settings(max_examples=100)`; each test is tagged `# Feature: contacts-catch-up-voice-assistant, Property N: <title>`
- The integration test in `tests/integration/test_call_flow.py` is a stretch goal — Vapi mocked via `respx` at the `httpx` boundary
- `call_started_at` on the Contact model doubles as crash-recovery state for the active-call guard; `_active_calls` in VapiService is `dict[str, datetime]` (not a set) to support the TTL sweep
- Social adapters use fixture data keyed by contact name (lowercased) with a `__default__` fallback — no real API credentials needed
