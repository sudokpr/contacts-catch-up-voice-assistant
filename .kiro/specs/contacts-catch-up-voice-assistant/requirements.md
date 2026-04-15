# Requirements Document

## Introduction

A voice assistant that proactively places outbound calls to a curated list of ~10 contacts to keep personal and professional relationships warm. The system uses a deterministic scoring engine to decide who to call and when, enriches conversations with semantic memory retrieved from a vector database, and extracts structured insights from call transcripts for future use. It exposes a web dashboard for managing contacts and monitoring call history.

---

## Glossary

- **System**: The contacts-catch-up-voice-assistant application as a whole
- **Contact**: A person stored in the system with phone/SIP details, preferences, and relationship metadata
- **Scoring_Engine**: The deterministic component that ranks contacts by call priority
- **Scheduler**: The APScheduler-based background worker that triggers calls on a schedule
- **Vapi**: The third-party voice API used to place outbound calls
- **Webhook_Handler**: The FastAPI endpoint that receives post-call events from Vapi
- **LLM_Extractor**: The component that calls an OpenAI-compatible API to extract structured data from transcripts
- **Memory_Store**: The Qdrant vector database used to store and retrieve semantic memories per contact
- **Embedding_Service**: The component that generates vector embeddings using nomic-embed-text
- **Social_Adapter**: A pluggable adapter that fetches social signal data for a given platform
- **Calendar_Service**: The stub component that provides free slot lookup and event creation
- **Dashboard**: The server-rendered HTML web interface for managing contacts and viewing call history

---

## Requirements

### Requirement 1: Contact Management

**User Story:** As a user, I want to manage my list of contacts, so that the system knows who to call and how to reach them.

#### Acceptance Criteria

1. THE System SHALL store each contact with the following fields: `contact_id` (UUID), `name`, `phone` (E.164 format), `sip` (optional), `contact_method` (phone or sip), `tags`, `timezone`, `last_called`, `last_spoken`, `call_time_preference`, `preferred_time_window`, `next_call_at`, `priority_boost`, `last_call_outcome`, `last_call_note`, and `social_handles`.
2. WHEN a user submits the onboarding form with valid contact details, THE System SHALL persist the new contact to the database and return a success response.
3. IF a user submits the onboarding form with a missing required field (name, phone, or timezone), THEN THE System SHALL reject the submission and return a descriptive validation error.
4. IF a user submits a phone number that does not conform to E.164 format, THEN THE System SHALL reject the submission and return a descriptive validation error.
5. WHEN a user updates an existing contact's details, THE System SHALL persist the changes and reflect them in subsequent scheduling and scoring decisions.
6. WHEN a user deletes a contact, THE System SHALL remove the contact record and all associated memory entries from the database and Memory_Store.

---

### Requirement 2: Call Decision Engine (Scoring)

**User Story:** As a user, I want the system to automatically decide who to call each day, so that I don't have to manually pick contacts.

#### Acceptance Criteria

1. THE Scoring_Engine SHALL compute a priority score for each eligible contact using the formula: `score = days_since_last_call * 0.6 + category_gap_score * 0.3 + priority_boost * 0.1`.
2. WHEN a contact has a `next_call_at` timestamp that is less than or equal to the current time, THE Scoring_Engine SHALL assign that contact the highest possible priority, overriding the formula score.
3. THE Scoring_Engine SHALL exclude contacts called within the last configurable recency window from the candidate pool.
4. THE Scoring_Engine SHALL apply category balancing by computing a `category_gap_score` that is higher for categories whose least-recently-contacted member was contacted longest ago.
5. WHEN selecting candidates, THE Scoring_Engine SHALL only include contacts whose current time (in the contact's configured timezone) falls within their `preferred_time_window`.
6. IF a contact has no `preferred_time_window` set, THEN THE Scoring_Engine SHALL apply the default constraint that calls are only placed between 09:00 and 20:00 in the contact's local timezone.
7. THE Scoring_Engine SHALL return the top 1–2 highest-scoring eligible contacts per scheduling cycle.

---

### Requirement 3: Scheduler

**User Story:** As a user, I want calls to be placed automatically on a schedule, so that I don't have to manually trigger them every day.

#### Acceptance Criteria

1. THE Scheduler SHALL run a daily cron job that invokes the Scoring_Engine and triggers outbound calls for the selected top contacts.
2. THE Scheduler SHALL run a short-interval polling loop (every 5–10 minutes) that checks for contacts whose `next_call_at` has passed and triggers immediate callbacks.
3. WHEN a one-off callback is requested (e.g., "call me in 1 hour"), THE Scheduler SHALL schedule an APScheduler one-off job for the specified future time.
4. IF the Scheduler fails to trigger a call due to a transient error, THEN THE Scheduler SHALL log the error and retry on the next polling cycle without crashing.

---

### Requirement 4: Vapi Outbound Call Integration

**User Story:** As a user, I want the system to place real voice calls through Vapi, so that contacts receive a natural phone or SIP call.

#### Acceptance Criteria

1. WHEN the Scheduler selects a contact for a call, THE System SHALL invoke the Vapi API to initiate an outbound call using the contact's `contact_method` (phone via PSTN or SIP).
2. THE Vapi assistant SHALL be configured with server-side tool definitions for: `get_contact_context`, `get_memory`, `search_memory`, `save_memory`, `get_calendar_slots`, and `create_calendar_event`.
3. WHEN Vapi invokes the `get_contact_context` tool during a call, THE System SHALL return the contact's name, last interaction summary, and category tags.
4. WHEN Vapi invokes the `get_memory` tool during a call, THE System SHALL perform a semantic search in the Memory_Store and return the top relevant memory entries for that contact.
5. WHEN Vapi invokes the `search_memory` tool during an ongoing call (triggered when the contact mentions a specific topic such as a trip, job change, or event), THE System SHALL perform a targeted semantic search in the Memory_Store using the provided query string and return the top relevant memory entries for that contact in real time.
6. WHILE a call is in progress, THE System SHALL respond to `search_memory` tool invocations within a latency that does not disrupt the natural flow of conversation (target: under 2 seconds).
7. WHEN Vapi invokes the `save_memory` tool during a call, THE System SHALL store the provided text as a memory entry in the Memory_Store with the appropriate contact ID and timestamp.
8. WHEN Vapi invokes the `get_calendar_slots` tool during a call, THE System SHALL return available time slots from the Calendar_Service.
9. WHEN Vapi invokes the `create_calendar_event` tool during a call, THE System SHALL create a calendar event via the Calendar_Service and return a confirmation.
10. IF the Vapi API returns an error when initiating a call, THEN THE System SHALL log the error, update the contact's `last_call_outcome` to `no_answer`, and schedule a retry.

---

### Requirement 5: Post-Call Webhook Processing

**User Story:** As a user, I want the system to automatically process call outcomes and extract insights, so that future calls are more informed and relevant.

#### Acceptance Criteria

1. WHEN Vapi sends a POST request to `/webhook/vapi` after a call ends, THE Webhook_Handler SHALL parse the payload and extract the transcript and call metadata.
2. THE Webhook_Handler SHALL classify the call outcome as `answered`, `busy`, or `no_answer` using rule-based logic on the call metadata, with an optional LLM fallback.
3. WHEN the call outcome is `answered` and a transcript is available, THE LLM_Extractor SHALL extract a structured object containing: `summary`, `highlights`, `facts`, `followups`, `callback`, and `call_time_preference`.
4. WHEN the LLM_Extractor receives a valid transcript, THE LLM_Extractor SHALL return a structured extraction result conforming to the defined schema within 2 retry attempts.
5. IF the LLM_Extractor fails after 2 retries, THEN THE Webhook_Handler SHALL fall back to an empty extraction structure and continue processing without crashing.
6. WHEN extraction succeeds, THE Webhook_Handler SHALL store each highlight and fact as a separate memory entry in the Memory_Store via the Embedding_Service.
7. WHEN extraction succeeds, THE Webhook_Handler SHALL update the contact record with: `last_called`, `last_call_outcome`, `last_call_note` (summary), and `next_call_at` (if a callback was requested).
8. WHEN the extracted `callback` field has type `relative` or `absolute`, THE Webhook_Handler SHALL parse the value and schedule a callback via the Scheduler.

---

### Requirement 6: Semantic Memory

**User Story:** As a user, I want the system to remember what was discussed in past calls, so that conversations feel personal and continuous.

#### Acceptance Criteria

1. THE Memory_Store SHALL store memory entries with the fields: `contact_id`, `type` (summary | highlight | fact | social), `text`, and `timestamp`.
2. WHEN a memory entry is stored, THE Embedding_Service SHALL generate a vector embedding for the entry's `text` field using the nomic-embed-text model and store it alongside the entry in Qdrant.
3. WHEN a semantic search is requested for a contact, THE Memory_Store SHALL return the top-K most semantically similar memory entries for that contact using cosine similarity.
4. THE Memory_Store SHALL scope all searches and retrievals to a specific `contact_id` to prevent cross-contact data leakage.

---

### Requirement 7: Social Signal Integration

**User Story:** As a user, I want the system to incorporate recent social activity from my contacts, so that calls feel timely and relevant.

#### Acceptance Criteria

1. THE System SHALL support social signal ingestion via pluggable Social_Adapter implementations, one per platform (Twitter/X, Instagram, LinkedIn).
2. WHEN a Social_Adapter fetches updates for a contact, THE System SHALL store each update as a memory entry of type `social` in the Memory_Store.
3. WHERE real API credentials are not configured, THE Social_Adapter SHALL return mock social data to enable development and testing without live credentials.
4. WHEN a new Social_Adapter is added, THE System SHALL integrate it without modifying existing adapter code (open/closed principle).

---

### Requirement 8: Calendar Integration

**User Story:** As a user, I want the assistant to be able to schedule follow-up meetings during a call, so that I can book time with contacts without leaving the conversation.

#### Acceptance Criteria

1. WHEN the `get_calendar_slots` tool is invoked, THE Calendar_Service SHALL return a list of available time slots.
2. WHEN the `create_calendar_event` tool is invoked with a start time, end time, and contact, THE Calendar_Service SHALL create a calendar event and return a confirmation object.
3. WHERE Google Calendar OAuth credentials are not configured, THE Calendar_Service SHALL return mock data for both slot lookup and event creation.

---

### Requirement 9: Web Dashboard

**User Story:** As a user, I want a web interface to manage contacts and monitor call activity, so that I have visibility and control over the system.

#### Acceptance Criteria

1. THE Dashboard SHALL display a contact list view showing each contact's name, category tags, last called date, next scheduled call, and last call outcome.
2. THE Dashboard SHALL display a contact detail view showing the contact's highlights, facts, notes, and a chronological call timeline.
3. THE Dashboard SHALL provide an onboarding form for adding a new contact with fields for: name, phone, category tags, timezone, preferred call time window, and social handles.
4. THE Dashboard SHALL provide a manual trigger button on the contact detail view that initiates an immediate outbound call for that contact.
5. THE System SHALL expose all data operations via `/api/...` routes and all HTML rendering via separate `/...` routes to allow future replacement of the HTML frontend with a JavaScript frontend.
6. WHEN a manual call trigger is activated, THE System SHALL initiate the call via the Vapi integration and return a confirmation response to the dashboard.

---

### Requirement 10: Configuration and Environment

**User Story:** As a developer, I want all external service credentials and tunable parameters to be managed via environment variables, so that the system is portable and secure.

#### Acceptance Criteria

1. THE System SHALL read all external service credentials from environment variables: `VAPI_API_KEY`, `VAPI_ASSISTANT_ID`, `VAPI_PHONE_NUMBER_ID`, `QDRANT_API_KEY`, `QDRANT_ENDPOINT`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_REFRESH_TOKEN`.
2. IF a required environment variable is missing at startup, THEN THE System SHALL raise a descriptive configuration error and refuse to start.
3. THE System SHALL support pointing the LLM client at a local Ollama instance by setting `OPENAI_BASE_URL` to a local endpoint, without any code changes.
4. THE System SHALL expose the local server to Vapi webhooks via ngrok or the `vapi listen` CLI, with the tunnel URL configurable via environment variable.
