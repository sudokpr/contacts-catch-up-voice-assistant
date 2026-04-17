#!/usr/bin/env python3
"""
setup_vapi.py — One-shot Vapi provisioning script.

Usage:
    python scripts/setup_vapi.py [--skip-numbers]

Reads VAPI_API_KEY, APP_BASE, and USER_NAME from .env (or environment).

Phase 0a — Buy a Vapi-managed PSTN phone number (US, free tier).
Phase 0b — Create a SIP trunk phone number for international / SIP-mode contacts.
             Requires SIP_TRUNK_URI, SIP_TRUNK_USER, SIP_TRUNK_PASS env vars
             (from a provider like Twilio Elastic SIP, Vonage, or any SIP server).
             Skipped if those vars are not set.
Phase 1  — Create tools (fails if any tool with the same name already exists).
Phase 2  — Create assistant using the tool IDs from Phase 1.

Pass --skip-numbers to skip Phase 0 (if you already have phone numbers configured).

Prints all created IDs at the end — copy them into your .env.
"""

import json
import os
import sys
import httpx
from pathlib import Path

PHONE_NUMBER_API = "https://api.vapi.ai/phone-number"


# ---------------------------------------------------------------------------
# Load environment
# ---------------------------------------------------------------------------

def _load_env() -> None:
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" in line:
                key, _, val = line.partition("=")
                val = val.strip().strip('"').strip("'")
                os.environ.setdefault(key.strip(), val)


_load_env()

VAPI_API_KEY = os.environ.get("VAPI_API_KEY", "")
APP_BASE     = os.environ.get("APP_BASE", "").rstrip("/")
USER_NAME    = os.environ.get("USER_NAME", "your friend")

if not VAPI_API_KEY:
    sys.exit("ERROR: VAPI_API_KEY is not set. Check your .env file.")
if not APP_BASE:
    sys.exit("ERROR: APP_BASE is not set. Set it to your public server URL (e.g. https://xyz.ngrok-free.dev).")

HEADERS = {
    "Authorization": f"Bearer {VAPI_API_KEY}",
    "Content-Type": "application/json",
}

TOOL_BASE_URL = f"{APP_BASE}/api/calls/tools"
WEBHOOK_URL   = f"{APP_BASE}/webhook/vapi"


# ---------------------------------------------------------------------------
# Tool definitions — must match routes in app/routes/calls.py
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_contact_context",
            "description": "Get contact profile context (name, tags, and last interaction summary).",
            "parameters": {
                "type": "object",
                "required": ["contact_id"],
                "properties": {
                    "contact_id": {
                        "type": "string",
                        "description": "Unique contact ID in your app.",
                    }
                },
            },
        },
        "server": {"url": f"{TOOL_BASE_URL}/get_contact_context"},
    },
    {
        "type": "function",
        "function": {
            "name": "get_memory",
            "description": (
                "Retrieve top semantic memories for a contact using default enriched context. "
                "Returns JSON: { memories: [{ text: string, type: string, timestamp: string(ISO-8601) }] }."
            ),
            "parameters": {
                "type": "object",
                "required": ["contact_id"],
                "properties": {
                    "contact_id": {
                        "type": "string",
                        "description": "Unique contact ID to retrieve memories for.",
                    }
                },
            },
        },
        "server": {"url": f"{TOOL_BASE_URL}/get_memory"},
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": (
                "Search contact memories with a targeted semantic query. "
                "Returns JSON: { memories: [{ text: string, type: string, timestamp: string(ISO-8601) }] }."
            ),
            "parameters": {
                "type": "object",
                "required": ["contact_id", "query"],
                "properties": {
                    "contact_id": {
                        "type": "string",
                        "description": "Unique contact ID to scope search.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Semantic query text to search memory.",
                    },
                },
            },
        },
        "server": {"url": f"{TOOL_BASE_URL}/search_memory"},
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": (
                "Save a new memory note for a contact. "
                "Returns JSON: { status: \"saved\", entry_id: string }."
            ),
            "parameters": {
                "type": "object",
                "required": ["contact_id", "text"],
                "properties": {
                    "contact_id": {
                        "type": "string",
                        "description": "Unique contact ID to attach the memory to.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Memory content to store.",
                    },
                },
            },
        },
        "server": {"url": f"{TOOL_BASE_URL}/save_memory"},
    },
    {
        "type": "function",
        "function": {
            "name": "get_calendar_slots",
            "description": (
                "Get available callback/meeting slots for scheduling. "
                "Returns JSON: { slots: [{ start: string(ISO-8601), end: string(ISO-8601) }] }."
            ),
            "parameters": {
                "type": "object",
                "required": ["contact_id"],
                "properties": {
                    "contact_id": {
                        "type": "string",
                        "description": "Unique contact ID used for contact validation/context.",
                    }
                },
            },
        },
        "server": {"url": f"{TOOL_BASE_URL}/get_calendar_slots"},
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": (
                "Create a follow-up calendar event for a contact. "
                "Returns JSON: { status: \"created\", event: { event_id: string, title: string, "
                "start: string(ISO-8601), end: string(ISO-8601), status: string, attendee_name: string|null } }."
            ),
            "parameters": {
                "type": "object",
                "required": ["contact_id"],
                "properties": {
                    "contact_id": {
                        "type": "string",
                        "description": "Unique contact ID for event creation.",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Optional ISO-8601 start time.",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "Optional ISO-8601 end time.",
                    },
                },
            },
        },
        "server": {"url": f"{TOOL_BASE_URL}/create_calendar_event"},
    },
]


# ---------------------------------------------------------------------------
# Assistant system prompt (uses {{variable}} placeholders for Vapi injection)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
{{occasion_context}}

Tone: {{tone_instructions}}

{{gift_status}}

You are an AI relationship manager making a brief, warm outbound call on behalf of {{user_name}} to {{contact_name}}.

---

STEP 1 — LOAD MEMORY (MANDATORY — do this silently before saying a single word):
You MUST call get_memory(contact_id="{{contact_id}}") right now.
Do NOT greet or speak until get_memory has returned.
Memories are sorted newest-first — the first entries are the most recent.
Pick ONE specific, concrete thing from the returned memories to weave in after pleasantries (Step 3).
If get_memory returns nothing useful, skip Step 3 and go straight to Step 4.

IMPORTANT: The memories returned by get_memory are the ONLY facts you are allowed to reference.
Do NOT invent, assume, or infer any personal details beyond what get_memory explicitly returns.

---

STEP 2 — OPEN WITH PLEASANTRIES:
Greet warmly, introduce yourself, check if it's a good time, then ask how they're doing:

"Hi {{contact_name}}! This is an AI assistant calling on behalf of {{user_name}} — hope I'm not catching you at a bad time? How are you doing?"

- If it's not a good time: "No worries at all — I'll let {{user_name}} know. Take care!" Then end.
- Listen to their answer. Respond warmly to whatever they say — one brief, genuine response.

---

STEP 3 — WEAVE IN ONE MEMORY (only if get_memory returned something concrete):
Bring in the specific thing you chose in Step 1. It must be a direct quote or clear paraphrase from a memory entry — not an inference.

"By the way, last time you mentioned [exact thing from memory]. How did that go?"

- One natural follow-up at most.
- Save anything new they share: call save_memory(contact_id="{{contact_id}}", text="...") silently.

---

STEP 4 — MEETING ASK:
"{{user_name}} was saying it'd be great to catch up properly — would you be up for a time sometime soon?"

- If yes: call get_calendar_slots(contact_id="{{contact_id}}"), offer 2 slots, confirm one, call create_calendar_event.
- If maybe/no: "Totally fine — I'll pass that along."

---

STEP 5 — CLOSE:
"Great talking — I'll pass everything back to {{user_name}}. Take care, {{contact_name}}!"

---

RULES:
- Total call: 2-3 minutes. Light and human.
- One question at a time, always.
- Never lead with a memory — pleasantries first.
- Be honest you are an AI assistant calling on {{user_name}}'s behalf.
- Never mention tools, databases, or that you are taking notes.
- Always use contact_id "{{contact_id}}" in every tool call.
- CRITICAL — NO HALLUCINATION: Only state things that appear explicitly in get_memory results. If it is not in memory, do not say it. When uncertain, ask — never assert.\
"""


# ---------------------------------------------------------------------------
# Phase 0a: Buy a Vapi-managed PSTN phone number
# ---------------------------------------------------------------------------

def create_pstn_number(client: httpx.Client) -> str:
    """
    Buy a Vapi-managed US phone number.
    Returns the phone number ID to use as VAPI_PHONE_NUMBER_ID.
    Skips and returns existing ID if VAPI_PHONE_NUMBER_ID is already set.
    """
    print("\n=== Phase 0a: PSTN phone number ===\n")

    existing_id = os.environ.get("VAPI_PHONE_NUMBER_ID", "")
    if existing_id:
        print(f"  Skipping — VAPI_PHONE_NUMBER_ID already set: {existing_id}")
        return existing_id

    resp = client.post(
        PHONE_NUMBER_API,
        headers=HEADERS,
        json={
            "provider": "vapi",
            "name": "Contacts Catch-up PSTN",
            "areaCode": "415",   # San Francisco area code — change if preferred
        },
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        print(f"  WARNING: Could not buy PSTN number: {resp.status_code} {resp.text}")
        print("  You can buy one manually in the Vapi dashboard and set VAPI_PHONE_NUMBER_ID.")
        return ""

    data = resp.json()
    number_id = data["id"]
    number = data.get("number", "(unknown)")
    print(f"  ✓ Bought PSTN number: {number}  id={number_id}")
    return number_id


# ---------------------------------------------------------------------------
# Phase 0b: Create a Vapi-native SIP number
# ---------------------------------------------------------------------------

def create_sip_number(client: httpx.Client) -> str:
    """
    Create a Vapi-managed SIP phone number (sip:xxx@sip.vapi.ai).
    No third-party SIP provider needed — Vapi hosts it natively.
    Returns the number ID to use as VAPI_SIP_TRUNK_ID.
    """
    print("\n=== Phase 0b: Vapi SIP number ===\n")

    existing_id = os.environ.get("VAPI_SIP_TRUNK_ID", "")
    if existing_id:
        print(f"  Skipping — VAPI_SIP_TRUNK_ID already set: {existing_id}")
        return existing_id

    resp = client.post(
        PHONE_NUMBER_API,
        headers=HEADERS,
        json={
            "provider": "vapi",
            "name": "Contacts Catch-up SIP",
            "sipUri": "sip:contacts-catchup@sip.vapi.ai",
        },
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        print(f"  WARNING: Could not create SIP number: {resp.status_code} {resp.text}")
        print("  Create it manually in Vapi dashboard → Phone Numbers → Add → SIP.")
        return ""

    data = resp.json()
    number_id = data["id"]
    sip_uri = data.get("sipUri") or data.get("number", "(unknown)")
    print(f"  ✓ Vapi SIP number: {sip_uri}  id={number_id}")
    return number_id


# ---------------------------------------------------------------------------
# Phase 1: Create tools
# ---------------------------------------------------------------------------

def create_tools(client: httpx.Client) -> dict[str, str]:
    """
    Create all tool definitions on Vapi.
    Fails immediately if a tool with the same name already exists.
    Returns {tool_name: tool_id}.
    """
    print("\n=== Phase 1: Creating tools ===\n")

    # Fetch existing tools to check for name conflicts
    resp = client.get("https://api.vapi.ai/tool", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    existing = {t["function"]["name"]: t["id"] for t in resp.json() if t.get("type") == "function"}

    conflicts = [defn["function"]["name"] for defn in TOOL_DEFINITIONS if defn["function"]["name"] in existing]
    if conflicts:
        print("ERROR: The following tools already exist on this Vapi account:")
        for name in conflicts:
            print(f"  - {name} (id: {existing[name]})")
        print("\nDelete them first, or use the existing IDs instead of running this script.")
        sys.exit(1)

    tool_ids: dict[str, str] = {}
    for defn in TOOL_DEFINITIONS:
        name = defn["function"]["name"]
        resp = client.post("https://api.vapi.ai/tool", headers=HEADERS, json=defn, timeout=15)
        if resp.status_code not in (200, 201):
            sys.exit(f"ERROR creating tool '{name}': {resp.status_code} {resp.text}")
        tool_id = resp.json()["id"]
        tool_ids[name] = tool_id
        print(f"  ✓ {name:35s} id={tool_id}")

    return tool_ids


# ---------------------------------------------------------------------------
# Phase 2: Create assistant
# ---------------------------------------------------------------------------

def create_assistant(client: httpx.Client, tool_ids: dict[str, str]) -> str:
    """Create the Contacts Catch-up assistant on Vapi. Returns the assistant ID."""
    print("\n=== Phase 2: Creating assistant ===\n")

    assistant_payload = {
        "name": "Contacts Catch-up",
        "serverUrl": WEBHOOK_URL,
        "model": {
            "provider": "openai",
            "model": "gpt-4.1",
            "toolIds": list(tool_ids.values()),
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}],
        },
        "voice": {
            "provider": "vapi",
            "voiceId": "Elliot",
        },
        "firstMessage": "",
        "voicemailMessage": "Please call back when you're available.",
        "endCallMessage": "Goodbye.",
        "transcriber": {
            "provider": "deepgram",
            "model": "flux-general-en",
            "language": "en",
            "fallbackPlan": {"autoFallback": {"enabled": True}},
        },
        "analysisPlan": {
            "summaryPlan": {"enabled": False},
            "successEvaluationPlan": {"enabled": False},
        },
        "backgroundDenoisingEnabled": True,
    }

    resp = client.post(
        "https://api.vapi.ai/assistant",
        headers=HEADERS,
        json=assistant_payload,
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        sys.exit(f"ERROR creating assistant: {resp.status_code} {resp.text}")

    assistant_id = resp.json()["id"]
    print(f"  ✓ Assistant 'Contacts Catch-up'  id={assistant_id}")
    return assistant_id


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    skip_numbers = "--skip-numbers" in sys.argv

    print("Vapi provisioning")
    print(f"  APP_BASE    : {APP_BASE}")
    print(f"  USER_NAME   : {USER_NAME}")
    print(f"  Webhook URL : {WEBHOOK_URL}")
    print(f"  Tool base   : {TOOL_BASE_URL}")
    if skip_numbers:
        print("  --skip-numbers: skipping phone number provisioning")

    with httpx.Client() as client:
        pstn_id = ""
        sip_trunk_id = ""

        if not skip_numbers:
            pstn_id = create_pstn_number(client)
            sip_trunk_id = create_sip_number(client)

        tool_ids = create_tools(client)
        assistant_id = create_assistant(client, tool_ids)

    print("\n=== Done — add these to your .env ===\n")
    if pstn_id:
        print(f"export VAPI_PHONE_NUMBER_ID={pstn_id}")
    if sip_trunk_id:
        print(f"export VAPI_SIP_TRUNK_ID={sip_trunk_id}")
    print(f"export VAPI_ASSISTANT_ID={assistant_id}")
    print()
    if not pstn_id and not skip_numbers:
        print("NOTE: No PSTN number was created. Buy one in the Vapi dashboard and set VAPI_PHONE_NUMBER_ID.")
    if not sip_trunk_id and not skip_numbers:
        print("NOTE: No SIP number was created. Create one manually in Vapi dashboard → Phone Numbers → Add → SIP.")
    print()
    print("Tool IDs (for reference):")
    for name, tid in tool_ids.items():
        print(f"  {name}: {tid}")


if __name__ == "__main__":
    main()
