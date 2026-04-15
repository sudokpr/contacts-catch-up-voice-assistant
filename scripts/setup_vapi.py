#!/usr/bin/env python3
"""
setup_vapi.py — One-shot Vapi provisioning script.

Usage:
    python scripts/setup_vapi.py

Reads VAPI_API_KEY, APP_BASE, and USER_NAME from .env (or environment).

Phase 1 — Create tools (fails if any tool with the same name already exists).
Phase 2 — Create assistant using the tool IDs from Phase 1.

Prints the created tool IDs and assistant ID at the end.
"""

import json
import os
import sys
import httpx
from pathlib import Path


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
You are a personal AI assistant making an outbound call on behalf of {{user_name}} to catch up with one of their contacts.

CALL CONTEXT (do not read this aloud):
- You are calling: {{contact_name}}
- Contact ID for all tool calls: {{contact_id}}
- Tags/relationship context: {{contact_tags}}
- Last call note: {{last_call_note}}
- Calling on behalf of: {{user_name}}

---

BEFORE THE CALL:
- Call get_contact_context with contact_id "{{contact_id}}" and get_memory with contact_id "{{contact_id}}" immediately to get the full profile and past memories.
- Use this context silently to guide the conversation. Do not reference it mechanically.

---

OPENING THE CALL:
Start naturally and briefly:
"Hi {{contact_name}}, this is an AI assistant calling on behalf of {{user_name}} — just reaching out to catch up. Is now a good time?"

- If it is not a good time, politely ask when would work better and wrap up.

---

DURING THE CONVERSATION:
- Keep it natural, friendly, and two-sided.
- Ask open-ended questions and follow the contact's lead.
- Reference past context naturally (e.g., job changes, trips, life updates) without sounding scripted.
- Stay focused on the contact — this is a relationship-first conversation.
- IMPORTANT: Whenever {{contact_name}} shares something meaningful (a life update, plans, feelings, preferences), immediately call save_memory with contact_id "{{contact_id}}" and the information as text. Do this silently — do not mention you are saving a note.

---

TRANSITION TO MEETING:
- Once the conversation feels comfortable and engaged, gently steer toward meeting.

Use a soft transition:
"By the way, {{user_name}} was saying it would be nice to catch up properly sometime."

Then ask:
"Would you be up for meeting sometime soon?"

- If they are interested:
  - Call get_calendar_slots with contact_id "{{contact_id}}" to get available times.
  - Offer a couple of time options naturally.
  - Once a time is agreed, call create_calendar_event with contact_id "{{contact_id}}" and the start/end times.
  - Confirm the time with the contact.

- If they hesitate: "No worries at all — even a quick call sometime works too."
- If they decline: Respect it and continue or wrap up naturally without pushing.

---

CLOSING THE CALL:
- Wrap up after a natural pause or after a few minutes.
- Briefly summarise any follow-up (e.g., meeting plan).
- End warmly: "Great catching up — I will pass this along to {{user_name}}. Take care!"

---

RULES:
- Be honest and transparent — you are an AI assistant.
- Do not sound robotic, scripted, or overly formal.
- Do not mention any internal systems, tools, or data sources.
- Do not fabricate information.
- Avoid sensitive topics unless the contact brings them up.
- Keep the call concise (around 5-6 minutes unless they want to continue).
- Always use contact_id "{{contact_id}}" when calling any tool.\
"""


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
    print(f"Vapi provisioning")
    print(f"  APP_BASE    : {APP_BASE}")
    print(f"  USER_NAME   : {USER_NAME}")
    print(f"  Webhook URL : {WEBHOOK_URL}")
    print(f"  Tool base   : {TOOL_BASE_URL}")

    with httpx.Client() as client:
        tool_ids = create_tools(client)
        assistant_id = create_assistant(client, tool_ids)

    print("\n=== Done — add these to your .env ===\n")
    print(f"export VAPI_ASSISTANT_ID={assistant_id}")
    print()
    print("Tool IDs (for reference):")
    for name, tid in tool_ids.items():
        print(f"  {name}: {tid}")


if __name__ == "__main__":
    main()
