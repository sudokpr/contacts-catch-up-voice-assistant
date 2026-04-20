#!/usr/bin/env python3
"""
setup_vapi.py — Idempotent Vapi provisioning script.

Usage:
    python scripts/setup_vapi.py [--skip-numbers]

Reads VAPI_API_KEY, APP_BASE, USER_NAME, and existing IDs from .env (or environment).

Behaviour (fully idempotent — safe to re-run):
  - VAPI_ASSISTANT_ID set   → patch prompt + serverUrl + tool URLs on existing assistant
  - VAPI_ASSISTANT_ID unset → create everything from scratch
  - VAPI_PHONE_NUMBER_ID set  → skip PSTN purchase
  - VAPI_SIP_TRUNK_ID set     → skip SIP creation
  - Tool already exists by name → patch its server URL; otherwise create it

Pass --skip-numbers to skip phone number provisioning entirely.
"""

import os
import sys
import httpx
from pathlib import Path

PHONE_NUMBER_API   = "https://api.vapi.ai/phone-number"
ASSISTANT_API      = "https://api.vapi.ai/assistant"
TOOL_API           = "https://api.vapi.ai/tool"


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

Background on {{contact_name}} (use naturally in conversation — do not recite):
{{recent_memories}}

---

STEP 1 — OPEN WITH PLEASANTRIES:
Greet warmly, introduce yourself, check if it's a good time, then ask how they're doing:

"Hi {{contact_name}}! This is an AI assistant calling on behalf of {{user_name}} — hope I'm not catching you at a bad time? How are you doing?"

- If it's not a good time: "No worries at all — I'll let {{user_name}} know. Take care!" Then end.
- Listen to their answer. Respond warmly — one brief, genuine response.

---

STEP 2 — WEAVE IN ONE MEMORY:
Bring in one specific thing from the background context above. It must come directly from that context — not invented.

"By the way, last time you mentioned [exact thing]. How did that go?"

- One natural follow-up at most.
- If the contact brings up a topic you want more detail on, call search_memory(contact_id="{{contact_id}}", query="<topic>") for a targeted lookup.

---

{{meeting_ask_section}}

STEP 4 — CLOSE:
"Great talking — I'll pass everything back to {{user_name}}. Take care, {{contact_name}}!"

---

MEMORY CAPTURE — save throughout the entire call, not just in Step 2:
Whenever the contact says anything worth remembering, call save_memory(contact_id="{{contact_id}}", text="...") silently. Save immediately — do not wait.

Worth saving: new job, promotion, move, health update, upcoming trip, project they're working on, a worry or decision they're facing, anything exciting or stressful, any commitment ("call me next month").
Not worth saving: small talk, pleasantries, things already in the background context.
Format: plain sentence — e.g. "Just started a new role at Google as a senior PM."

---

RULES:
- Total call: 2-3 minutes. Light and human.
- One question at a time, always.
- Never lead with a memory — pleasantries first.
- Be honest you are an AI assistant calling on {{user_name}}'s behalf.
- Never mention tools, databases, or that you are taking notes.
- Always use contact_id "{{contact_id}}" in every tool call.
- CRITICAL — NO HALLUCINATION: Only reference things that appear explicitly in the background context. If it is not there, do not say it. When uncertain, ask — never assert.
"""


# ---------------------------------------------------------------------------
# Phase 0a: Buy a Vapi-managed PSTN phone number
# ---------------------------------------------------------------------------

def ensure_pstn_number(client: httpx.Client) -> str:
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
            "areaCode": "415",
        },
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        print(f"  WARNING: Could not buy PSTN number: {resp.status_code} {resp.text}")
        print("  Buy one manually in the Vapi dashboard and set VAPI_PHONE_NUMBER_ID.")
        return ""

    data = resp.json()
    number_id = data["id"]
    print(f"  ✓ Bought PSTN number: {data.get('number', '(unknown)')}  id={number_id}")
    return number_id


# ---------------------------------------------------------------------------
# Phase 0b: Create a Vapi-native SIP number
# ---------------------------------------------------------------------------

def ensure_sip_number(client: httpx.Client) -> str:
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
    print(f"  ✓ Vapi SIP number: {data.get('sipUri') or data.get('number', '(unknown)')}  id={number_id}")
    return number_id


# ---------------------------------------------------------------------------
# Phase 1: Ensure tools (create or patch server URL)
# ---------------------------------------------------------------------------

def ensure_tools(client: httpx.Client) -> dict[str, str]:
    """
    For each tool definition:
      - If a tool with that name already exists → patch its server URL.
      - Otherwise → create it.
    Returns {tool_name: tool_id}.
    """
    print("\n=== Phase 1: Ensuring tools ===\n")

    resp = client.get(TOOL_API, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    existing = {t["function"]["name"]: t["id"] for t in resp.json() if t.get("type") == "function"}

    tool_ids: dict[str, str] = {}
    for defn in TOOL_DEFINITIONS:
        name = defn["function"]["name"]
        if name in existing:
            tool_id = existing[name]
            patch_resp = client.patch(
                f"{TOOL_API}/{tool_id}",
                headers=HEADERS,
                json={"server": defn["server"]},
                timeout=15,
            )
            if patch_resp.status_code not in (200, 201):
                print(f"  WARNING: Could not patch tool '{name}': {patch_resp.status_code} {patch_resp.text}")
            else:
                print(f"  ~ {name:35s} patched  id={tool_id}")
            tool_ids[name] = tool_id
        else:
            create_resp = client.post(TOOL_API, headers=HEADERS, json=defn, timeout=15)
            if create_resp.status_code not in (200, 201):
                sys.exit(f"ERROR creating tool '{name}': {create_resp.status_code} {create_resp.text}")
            tool_id = create_resp.json()["id"]
            tool_ids[name] = tool_id
            print(f"  ✓ {name:35s} created  id={tool_id}")

    return tool_ids


# ---------------------------------------------------------------------------
# Phase 2: Create or patch assistant
# ---------------------------------------------------------------------------

def patch_assistant(client: httpx.Client, assistant_id: str, tool_ids: dict[str, str]) -> None:
    """Patch existing assistant: update prompt, serverUrl, and tool associations."""
    print(f"\n=== Phase 2: Patching assistant {assistant_id} ===\n")

    resp = client.get(f"{ASSISTANT_API}/{assistant_id}", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    current_model = resp.json().get("model", {})

    patch_resp = client.patch(
        f"{ASSISTANT_API}/{assistant_id}",
        headers=HEADERS,
        json={
            "serverUrl": WEBHOOK_URL,
            "model": {
                "provider": current_model.get("provider", "openai"),
                "model": current_model.get("model", "gpt-4.1"),
                "toolIds": list(tool_ids.values()),
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}],
            },
        },
        timeout=15,
    )
    patch_resp.raise_for_status()
    restored = patch_resp.json().get("model", {}).get("toolIds") or []
    print(f"  ✓ Prompt + serverUrl updated — {len(restored)} tools on assistant {assistant_id}")


def create_assistant(client: httpx.Client, tool_ids: dict[str, str]) -> str:
    """Create a new Contacts Catch-up assistant. Returns the assistant ID."""
    print("\n=== Phase 2: Creating assistant ===\n")

    resp = client.post(
        ASSISTANT_API,
        headers=HEADERS,
        json={
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
        },
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
    existing_assistant_id = os.environ.get("VAPI_ASSISTANT_ID", "")

    print("Vapi provisioning (idempotent)")
    print(f"  APP_BASE      : {APP_BASE}")
    print(f"  USER_NAME     : {USER_NAME}")
    print(f"  Webhook URL   : {WEBHOOK_URL}")
    print(f"  Tool base URL : {TOOL_BASE_URL}")
    print(f"  Mode          : {'PATCH existing' if existing_assistant_id else 'CREATE new'}")

    with httpx.Client() as client:
        pstn_id = ""
        sip_trunk_id = ""

        if not skip_numbers:
            pstn_id = ensure_pstn_number(client)
            sip_trunk_id = ensure_sip_number(client)

        tool_ids = ensure_tools(client)

        if existing_assistant_id:
            patch_assistant(client, existing_assistant_id, tool_ids)
            assistant_id = existing_assistant_id
        else:
            assistant_id = create_assistant(client, tool_ids)

    print("\n=== Done ===\n")
    if not existing_assistant_id:
        print("New IDs — add these to your .env / Railway env vars:\n")
        if pstn_id:
            print(f"  VAPI_PHONE_NUMBER_ID={pstn_id}")
        if sip_trunk_id:
            print(f"  VAPI_SIP_TRUNK_ID={sip_trunk_id}")
        print(f"  VAPI_ASSISTANT_ID={assistant_id}")
    else:
        print(f"  Assistant {assistant_id} patched — no new IDs needed.")

    print("\nTool IDs (for reference):")
    for name, tid in tool_ids.items():
        print(f"  {name}: {tid}")


if __name__ == "__main__":
    main()
