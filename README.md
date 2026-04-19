# Contacts Catch-Up Voice Assistant

> *"Arjun secured a major funding round today. I've ordered a bouquet and I'm calling him now to congratulate."*

Managing relationships — personal or professional — takes consistent effort that busy people don't always have. Old friends drift away. Business partners feel forgotten. Opportunities to reach out at the right moment get missed.

**Contacts Catch-Up is an autonomous AI relationship manager** that works for both personal contacts and business partners. It runs in the background, monitors news and occasions, orders gifts, and places outbound AI phone calls on your behalf — with full context about who they are, what you last talked about, and what's happening in their life right now. No human needs to remember anything. The AI handles it all.

---

## What it actually does

The system runs several background jobs every day, each targeting a different relationship signal:

**Regular catch-ups:** A scoring engine ranks contacts by how overdue they are (time since last call, priority boosts, timezone-aware call windows) and calls the top-ranked ones each morning.

**Birthday & anniversary calls:** Detects today's birthdays and anniversaries across all contacts. For contacts tagged `send-gift`, automatically orders flowers + a custom mug before the call.

**Festival gifting:** On major holidays (Diwali, Christmas, Eid, New Year, Holi), sends sweet boxes to business partners and friends tagged `send-gift`, then calls to wish them.

**Deal-news triggered calls:** Monitors LinkedIn (and other social feeds) for deal announcements, funding rounds, and major contracts. When a business partner shares big news, the AI calls to congratulate them and sends a bouquet — no human prompt needed.

**CRM deal closure calls:** When a deal closes with one of your contacts (via Salesforce or a mock adapter), the AI calls to thank them and celebrate the partnership.

**LinkedIn promotion detection:** Detects when a business contact announces a promotion or new senior role, then calls to congratulate them.

**Past-promise follow-ups:** If a call summary mentions "next quarter", "next month", or similar commitments, the scheduler automatically sets a follow-up call for the right time.

**Tone adaptation:** Uses a professional tone for business contacts and a casual, warm tone for personal contacts — all from a single AI assistant.

**Gift delivery status:** If a gift was recently ordered for a contact, the AI naturally mentions it during the call ("a bouquet is on its way to you!").

After every call:
1. Highlights and key facts are stored in Qdrant (semantic memory)
2. The contact record is updated with outcome and summary
3. The dashboard refreshes in real-time via Server-Sent Events

---

## Example calls

**Catching up with a friend:**
> "Hey Maya! This is an AI assistant calling on behalf of Kirthi — hope I'm not catching you at a bad time? Last time you spoke you'd just started leading that Kubernetes migration — how did that go?"

**Congratulating a business partner on a funding round:**
> "Congratulations Arjun! We just saw the news about your seed funding — absolutely thrilled for you and the team! Kirthi also arranged a little something — a bouquet is on its way to you."

**Festival wishes:**
> "Happy Diwali, Priya! Calling on behalf of Kirthi to wish you and your family a wonderful celebration. He's also sent a sweet box your way — hope it arrives in time!"

**Honoring a past commitment:**
> "Hi David — Kirthi mentioned last quarter you were open to catching up in three months. He wanted to make sure that didn't slip through the cracks — would you be up for a time soon?"

---

## Architecture

```
Scheduler → Scoring engine → Vapi outbound call
                                     ↓
                          AI conversation (with tools)
                                     ↓
                          End-of-call webhook → Process transcript
                                     ↓
                    ┌────────────────┼────────────────┐
                    ↓                ↓                ↓
              Update contact    Store memories    Notify UI (SSE)
               (SQLite)          (Qdrant)         (live view)
```

**Stack:**
- **FastAPI** — REST API + SSE streaming
- **Vapi** — outbound voice calls + AI conversation
- **Qdrant** — vector database for semantic memory
- **APScheduler** — call scheduling
- **SQLite (aiosqlite)** — contact and call history storage
- **Vanilla JS SPA** — modern dark-theme dashboard, no build step

---

## Quick Start

### 1. Clone and install

```bash
git clone <repo>
cd contacts-catch-up-voice-assistant
uv sync --extra dev
```

### 2. Configure environment

Copy the example and fill in your credentials:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `VAPI_API_KEY` | Yes | Your Vapi API key — [vapi.ai](https://vapi.ai) |
| `VAPI_ASSISTANT_ID` | Yes | The assistant ID created by `setup_vapi.py` |
| `VAPI_PHONE_NUMBER_ID` | Yes | Your Vapi phone number ID for outbound PSTN calls |
| `APP_BASE` | Yes | Public URL of this server (ngrok URL when developing) |
| `USER_NAME` | Yes | Your name — the AI introduces itself as calling on your behalf |
| `QDRANT_API_KEY` | Yes | Qdrant Cloud API key — [cloud.qdrant.io](https://cloud.qdrant.io) |
| `QDRANT_ENDPOINT` | Yes | Qdrant Cloud endpoint URL |
| `OPENAI_API_KEY` | Yes | OpenAI API key (used for fallback outcome classification) |
| `OPENAI_BASE_URL` | Yes | OpenAI-compatible base URL (`https://api.openai.com/v1`) |
| `OPENAI_MODEL` | Yes | Model name (e.g. `gpt-4o`) |
| `VAPI_SIP_TRUNK_ID` | No | Vapi SIP number ID for SIP-mode contacts — created by `setup_vapi.py` |
| `GOOGLE_CLIENT_ID` | No | Google OAuth — for calendar-aware scheduling |
| `GOOGLE_CLIENT_SECRET` | No | Google OAuth client secret |
| `GOOGLE_REFRESH_TOKEN` | No | Google OAuth refresh token |

### 3. Expose your server publicly

Vapi needs a public URL to send webhooks and tool-call requests back to your server.

**Option A — ngrok (recommended for development):**

```bash
ngrok http 8000
# Copy the https://xxx.ngrok-free.app URL → set as APP_BASE in .env
```

**Option B — Vapi CLI:**

```bash
vapi listen --port 8000
```

### 4. Provision Vapi tools and assistant

Run the setup script once. It provisions everything on your Vapi account in order:

```bash
python scripts/setup_vapi.py
```

**Phase 0a — PSTN phone number:** Buys a Vapi-managed US phone number (area code 415 by default). Skipped if `VAPI_PHONE_NUMBER_ID` is already set in `.env`.

**Phase 0b — Vapi SIP number:** Creates a Vapi-managed SIP number (`sip:contacts-catchup@sip.vapi.ai`) — no third-party SIP provider needed. Skipped if `VAPI_SIP_TRUNK_ID` is already set.

**Phase 1 — Tools:** Creates all 6 server tools pointing to your `APP_BASE`. Exits with an error if any tool name already exists — delete them in the Vapi dashboard first.

**Phase 2 — Assistant:** Creates the assistant with the full system prompt, voice config, tool bindings, and `serverUrl` for webhooks.

At the end, the script prints all IDs — copy them into your `.env`:

```
export VAPI_PHONE_NUMBER_ID=...
export VAPI_SIP_TRUNK_ID=...
export VAPI_ASSISTANT_ID=...
```

**Re-running just the numbers** (after already creating tools and assistant):

```bash
python scripts/setup_vapi.py --skip-numbers
# This only re-runs phases 1 and 2, not phone number provisioning
```

### 5. Seed sample contacts (optional but recommended for demos)

Populates the database with 5 realistic contacts — complete with past call notes, social media handles, and conversation memories already stored in Qdrant:

```bash
python scripts/seed_contacts.py
```

This creates 8 contacts — 5 personal and 3 business:

**Personal contacts:**
- **Maya Patel** — college friend, just got promoted to Staff Engineer, cat had kittens
- **David Okafor** — ex-colleague who left Stripe to build a climate-tech startup
- **Sarah Chen** — family friend who moved to London, working on a novel
- **Raj Sundaram** — university friend in Singapore, competitive chess player
- **Priya Menon** — school friend, doctor considering a Johns Hopkins fellowship

**Business partners:**
- **Arjun Mehta** — SaaS founder in Mumbai; birthday set to today → triggers birthday call + mug + flowers demo; LinkedIn fixture shows seed funding → triggers deal congratulations call
- **Priya Sharma** — VP Sales at a Delhi logistics firm; LinkedIn fixture shows ₹10 crore contract → triggers deal congratulations call
- **Marcus Weber** — Managing Partner at a Berlin VC firm; LinkedIn fixture shows promotion → triggers congratulations call

Each contact has realistic `last_called` timestamps, outcomes, call notes, and multiple memory entries (highlights, facts, summaries, social updates) so the AI has rich context to draw on during a call. Safe to re-run — skips contacts that already exist by name.

### 6. Start the server

```bash
uv run uvicorn app.main:app --reload --port 8000
```

Open `http://localhost:8000` — the dashboard should load with your contact list.

---

## Setting up Vapi — in detail

The assistant relies on 6 server tools that Vapi calls back to your server during a live conversation:

| Tool | Endpoint | What it does |
|---|---|---|
| `get_contact_context` | `POST /api/calls/tools/get_contact_context` | Returns contact name, tags, and last call summary |
| `get_memory` | `POST /api/calls/tools/get_memory` | Semantic search over past conversation highlights |
| `save_memory` | `POST /api/calls/tools/save_memory` | Stores a new fact or highlight during the call |
| `get_calendar_events` | `POST /api/calls/tools/get_calendar_events` | Returns upcoming calendar events for context |
| `update_calendar` | `POST /api/calls/tools/update_calendar` | Schedules a follow-up or callback |
| `get_social_updates` | `POST /api/calls/tools/get_social_updates` | Returns recent social activity for conversation starters |

The assistant also has a `serverUrl` pointing to `POST /webhook/vapi` — Vapi calls this at the end of every call with the full transcript, analysis summary, and artifact data.

### Tool request/response format

Vapi sends tool calls in this envelope:

```json
{
  "message": {
    "toolWithToolCallList": [{
      "toolCall": {
        "id": "call_abc123",
        "function": {
          "name": "save_memory",
          "arguments": { "contact_id": "...", "text": "She mentioned she's training for a marathon" }
        }
      }
    }]
  }
}
```

Your server must respond with:

```json
{
  "results": [{
    "toolCallId": "call_abc123",
    "result": { "status": "saved" }
  }]
}
```

The `setup_vapi.py` script handles all the tool registration so you don't need to configure this manually.

### Testing without Vapi

You can simulate the full end-of-call flow locally without ngrok by POSTing directly to the webhook endpoint:

```bash
curl -X POST http://localhost:8000/webhook/vapi \
  -H "Content-Type: application/json" \
  -d '{
    "type": "end-of-call-report",
    "call": {
      "id": "test-001",
      "endedReason": "customer-ended-call",
      "metadata": {"contact_id": "<your-contact-id>"}
    },
    "analysis": {
      "summary": "Had a great catch-up. She is moving to Seattle next month and is excited about the new role."
    },
    "artifact": {
      "messages": [
        {"role": "user", "message": "Yeah I'm really excited, the new job starts in May!"},
        {"role": "user", "message": "We should definitely catch up in person when I'm settled in."}
      ]
    }
  }'
```

This will update the contact's `last_called`, `last_call_note`, and store memory entries in Qdrant — same as a real call.

---

## Dashboard

The web UI at `http://localhost:8000` gives you:

- **Contact list** with search, last outcome status dots, and live pulse indicators during active calls
- **Contact detail** with all fields — phone, SIP URI, timezone, tags, social handles, call history
- **Create / Edit / Delete** contacts with a full-featured modal form
- **Call Now** button that triggers an immediate outbound call and shows a live view as it happens — connecting → in progress (with timer) → ended (with outcome and summary)
- **Memory feed** showing conversation highlights, summaries, and facts stored after each call

---

## Testing with SIP (no PSTN number needed)

### What you need

A **SIP softphone** — an app that registers with a SIP server and rings like a phone when called. The easiest free option is [Linphone](https://www.linphone.org/).

### Setup with Linphone (5 minutes)

1. **Create a free SIP account** at [linphone.org](https://www.linphone.org/freesip) — you'll get a SIP address like `sip:yourname@sip.linphone.org`

2. **Install the Linphone app** on your phone or desktop:
   - iOS / Android: search "Linphone" in the app store
   - macOS / Windows / Linux: download from [linphone.org/downloads](https://www.linphone.org/downloads)

3. **Sign in** to the app with your Linphone credentials — you'll see a green "registered" indicator when it's connected

4. **Update your contact** in the dashboard (or in `seed_contacts.py`):
   - Set **Contact Method** to **SIP**
   - Set **SIP URI** to `sip:yourname@sip.linphone.org`

5. **Hit Call Now** — Vapi dials the SIP URI, Linphone rings on your device

### Alternative SIP providers

| Provider | Free tier | Notes |
|---|---|---|
| [Linphone](https://linphone.org/freesip) | Yes | Easiest, no config needed |
| [Zoiper](https://www.zoiper.com/) | Free tier | Good softphone app, needs a separate SIP account |
| [OnSIP](https://www.onsip.com/) | Free for developers | More features |

### Vapi SIP requirements

Vapi sends calls to SIP URIs in the format `sip:user@domain`. Make sure your contact's SIP URI is in this format — no `sips:` or custom ports needed for Linphone.

The `phone` field is still required by the Contact model (for validation). For SIP-only contacts, use a placeholder like `+10000000001` — it won't be dialled.

---

## Scheduler — automated calling

The server runs two background jobs via APScheduler as soon as it starts:

### Daily cron job

Fires once a day (default: **09:00 server local time**) and:
1. Loads all contacts from the database
2. Scores them using the call decision engine (time since last call, priority boost, preferred call window)
3. Calls the top-ranked contacts via Vapi

To change the hour, set `SCHEDULER_DAILY_HOUR` in your `.env`:

```bash
export SCHEDULER_DAILY_HOUR=10   # fire at 10:00 instead of 09:00
```

### 5-minute polling job

Runs every 5 minutes and calls any contacts whose `next_call_at` timestamp has passed. This is used for scheduled callbacks — if a contact asks to be called back at a specific time, it gets written to the database and this job picks it up.

### How contacts are scored

The scoring engine in `app/services/scoring.py` ranks contacts by:

- **Days since last spoken** — the longer the gap, the higher the score
- **Priority boost** — a manual multiplier you set per contact (0–10)
- **Call time preference** — contacts set to "morning" only score highly during morning hours in their timezone; "evening" contacts score at night; "any time" contacts are always eligible
- **In-progress calls** — contacts already on a call are skipped

The top-ranked contacts above a minimum threshold get called each day.

### Disabling the scheduler

The scheduler starts automatically with the server. If you want to manage calls manually only (via the Call Now button), you can comment out `start_scheduler()` in `app/main.py`.

---

## Project Structure

```
app/
  config.py           # Environment variable loading (pydantic-settings)
  main.py             # FastAPI app factory + lifespan startup
  db.py               # SQLite schema and helpers
  sse_bus.py          # In-process pub/sub for live call SSE events
  routes/
    contacts.py       # Contact CRUD + memories endpoint
    calls.py          # Call trigger, SSE stream, Vapi tool endpoints
    webhook.py        # End-of-call webhook handler
    dashboard.py      # Serves the SPA
  services/
    vapi.py           # Vapi outbound call initiation
    scoring.py        # Call decision scoring engine
    embedding.py      # Text embeddings (with SHA-256 fallback)
    qdrant.py         # Qdrant memory store
    calendar.py       # Google Calendar integration (stub)
    social/           # Social media adapters (Twitter, Instagram, LinkedIn)
  models/
    contact.py        # Contact Pydantic model
    memory.py         # MemoryEntry model
  workers/
    scheduler.py      # APScheduler periodic call jobs
  static/
    index.html        # Single-page application (Tailwind CSS, vanilla JS)
scripts/
  setup_vapi.py       # One-shot Vapi provisioning (tools + assistant)
tests/
  unit/               # Unit tests (35 passing)
```

---

## Sample walkthrough — business relationship management

Here's a full day in the life of the system managing **Arjun Mehta**, a SaaS founder and business partner.

**Morning — LinkedIn monitor fires:**
> System detects Arjun's LinkedIn post: *"Excited to share — we just secured ₹5 crore in seed funding!"*
> Scheduler orders a bouquet via mock gifting API.
> Vapi places an outbound WebRTC call.
> AI: *"Arjun, this is an AI calling on behalf of Kirthi — huge congratulations on the funding round! That's a major milestone. Kirthi also wanted to send something — a bouquet is on its way to you. How does it feel to have that runway secured?"*
> Call ends. Highlights stored in Qdrant. Contact updated.

**Birthday detected (Arjun's birthday = today):**
> System orders flowers + mug. Places birthday call.
> AI: *"Happy Birthday Arjun! Kirthi wanted to make sure he didn't let the day pass without reaching out. Hope you're celebrating — enjoy the day!"*
> No meeting ask (birthday calls skip the scheduling step).

**CRM deal closure detected:**
> Mock CRM signals that Arjun's deal *"RelayAI Pro Annual License — ₹2.4L"* closed today.
> Places CRM deal call.
> AI: *"Arjun! We just saw the annual license come through — that's fantastic. Kirthi wanted to personally thank you for the trust you've placed in the product. Looking forward to delivering real value for your team."*

**Festival (Diwali):**
> On Oct 20, system sends sweet box to all contacts tagged `send-gift`.
> AI: *"Happy Diwali, Arjun! Kirthi sends his warmest wishes to you and your family. He's also sent a sweet box your way — hope it arrives before the celebrations!"*

**Follow-up commitment honored:**
> Previous call summary contained: *"let's reconnect next quarter"*
> System auto-scheduled a follow-up 60 days out.
> Follow-up fires automatically with full memory context from last call.

**Tone:**
> Because Arjun is tagged `relationship_type=business`, all calls use a professional, concise tone — no casual slang, mindful of his time.

---

## Roadmap

- Real LinkedIn API integration (currently fixture-based)
- Real Salesforce/HubSpot CRM adapter (currently fixture-based)
- Real gifting vendor API (Ferns & Petals, Amazon Business)
- Multi-user support with per-user assistant provisioning
- Mobile app for managing contacts on the go