"""
Vapi outbound call service.

Maintains an in-memory guard (_active_calls) to prevent double-calling a contact.
The polling loop calls sweep_stale_active_calls() to release entries older than
30 minutes in case a webhook is never delivered.
"""

import logging
from datetime import datetime, UTC
from uuid import UUID

import httpx

from app.models.contact import Contact
from app.services.gifting import FESTIVAL_OCCASIONS

logger = logging.getLogger(__name__)

# contact_id -> call_started_at  (dict, not set — needed for TTL sweep)
_active_calls: dict[str, datetime] = {}

# vapi_call_id -> contact_id  (populated by Web SDK calls via /api/calls/web-register)
_web_call_registry: dict[str, str] = {}


def register_web_call(call_id: str, contact_id: str) -> None:
    """Record a browser-initiated WebRTC call so the webhook can resolve contact_id."""
    _web_call_registry[call_id] = contact_id
    logger.info("Registered web call %s → contact %s", call_id, contact_id)


def get_web_call_contact(call_id: str) -> str | None:
    """Return the contact_id for a browser-initiated call, or None."""
    return _web_call_registry.get(call_id)

VAPI_CALL_URL = "https://api.vapi.ai/call"
VAPI_ASSISTANT_URL = "https://api.vapi.ai/assistant"


class AlreadyOnCallError(Exception):
    """Raised when initiate_call is called for a contact already in _active_calls."""


class VapiError(Exception):
    """Raised when the Vapi API returns an error."""


class VapiCallResponse:
    """Minimal wrapper around the Vapi /call response."""

    def __init__(self, call_id: str, raw: dict):
        self.call_id = call_id
        self.raw = raw


def _is_valid_uuid(value: str) -> bool:
    try:
        UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


def _build_variable_values(
    contact: Contact,
    user_name: str,
    occasion: str = "",
    gift_summary: str = "",
    gift_status: str = "",
    recent_memories: str = "",
) -> dict:
    """
    Build per-call variable values injected into the assistant's system prompt
    via Vapi's {{variable}} template substitution.

    Placeholders: {{user_name}}, {{contact_id}}, {{contact_name}}, {{contact_tags}},
                  {{last_call_note}}, {{occasion_context}}, {{tone_instructions}},
                  {{gift_status}}, {{recent_memories}}
    """
    # For occasion calls: skip memory weaving and meeting ask entirely — just greet, gift, close.
    _wishes_note = (
        "CRITICAL INSTRUCTIONS FOR THIS CALL: "
        "1. Skip Step 2 entirely — do NOT bring up any past memories or previous conversations. "
        "2. Skip the meeting ask — do NOT ask to schedule anything. "
        "3. Flow: warm greeting → mention gift if sent → brief warm close. Keep the call under 60 seconds."
    )

    occasion_context = ""
    if occasion == "birthday":
        gift_line = f" {user_name} also arranged a little gift for you: {gift_summary}." if gift_summary else ""
        occasion_context = (
            f"⚠️ BIRTHDAY CALL: Today is {contact.name}'s birthday. "
            f"Open with: 'Happy Birthday {contact.name}! This is an AI calling on behalf of {user_name} — "
            f"just wanted to wish you a wonderful day!'{gift_line} "
            f"Then close warmly: 'Hope you have a fantastic birthday — take care!' and end the call. "
            f"{_wishes_note}"
        )
    elif occasion == "anniversary":
        gift_line = f" {user_name} also sent a little something to celebrate: {gift_summary}." if gift_summary else ""
        occasion_context = (
            f"⚠️ ANNIVERSARY CALL: Today is {contact.name}'s anniversary. "
            f"Open with: 'Happy Anniversary {contact.name}! Calling on behalf of {user_name} to celebrate with you!'"
            f"{gift_line} Then close warmly and end the call. {_wishes_note}"
        )
    elif occasion == "deal_congratulations":
        gift_line = f" {user_name} also arranged a bouquet to be sent your way: {gift_summary}." if gift_summary else ""
        occasion_context = (
            f"⚠️ DEAL CONGRATULATIONS CALL: {contact.name} recently secured a major deal or funding. "
            f"Open with: 'Congratulations {contact.name}! We just heard the incredible news — absolutely thrilled for you and your team!'"
            f"{gift_line} Then close warmly and end the call. {_wishes_note}"
        )
    elif occasion == "promotion_congratulations":
        gift_line = f" {user_name} also sent you a little something to celebrate: {gift_summary}." if gift_summary else ""
        occasion_context = (
            f"⚠️ PROMOTION CONGRATULATIONS CALL: {contact.name} was recently promoted to a new role. "
            f"Open with: 'Congratulations on the promotion, {contact.name}! Well deserved — calling on behalf of {user_name} to celebrate with you!'"
            f"{gift_line} Then close warmly and end the call. {_wishes_note}"
        )
    elif occasion == "crm_deal":
        gift_line = f" {user_name} also arranged a small token of appreciation: {gift_summary}." if gift_summary else ""
        occasion_context = (
            f"⚠️ CRM DEAL CLOSURE CALL: {contact.name} just closed a deal with us. "
            f"Open with: 'Thank you so much, {contact.name}! We just saw the deal come through — "
            f"{user_name} wanted to personally call and say how much we value your partnership!'"
            f"{gift_line} Then close warmly and end the call. {_wishes_note}"
        )
    elif occasion:
        occasion_display = occasion.replace("_", " ").title()
        gift_line = f" {user_name} has also arranged a little gift: {gift_summary}." if gift_summary else ""
        occasion_context = (
            f"⚠️ FESTIVAL CALL ({occasion_display}): Wishing {contact.name} on {occasion_display}. "
            f"Open with: 'Happy {occasion_display}, {contact.name}! "
            f"Calling on behalf of {user_name} to wish you and your family a wonderful celebration!'"
            f"{gift_line} Then close warmly and end the call. {_wishes_note}"
        )

    tone_instructions = (
        "Use a casual, warm, friendly tone. Informal language is perfectly fine — speak like a friend."
        if contact.relationship_type == "personal"
        else
        "Use a professional, respectful, and warm tone. Be concise and mindful of their time. "
        "Maintain a business-friendly conversational style."
    )

    # Only regular catch-up calls and CRM deal calls warrant a meeting ask.
    # Birthday, anniversary, festival, and congratulations calls close warmly without it.
    _no_meeting_occasions = {
        "birthday", "anniversary", "deal_congratulations",
        "promotion_congratulations", *FESTIVAL_OCCASIONS,
    }
    if occasion in _no_meeting_occasions:
        meeting_ask_section = ""
    else:
        meeting_ask_section = (
            "STEP 4 — MEETING ASK:\n"
            f'"{user_name} was saying it\'d be great to catch up properly — '
            "would you be up for a time sometime soon?\"\n\n"
            f'- If yes: call get_calendar_slots(contact_id="{contact.contact_id}"), '
            "offer 2 slots, confirm one, call create_calendar_event.\n"
            "- If maybe/no: \"Totally fine — I'll pass that along.\"\n\n---"
        )

    return {
        "user_name": user_name,
        "contact_id": contact.contact_id,
        "contact_name": contact.name,
        "contact_tags": ", ".join(contact.tags) if contact.tags else "none",
        "last_call_note": contact.last_call_note or "No previous calls recorded.",
        "occasion_context": occasion_context,
        "tone_instructions": tone_instructions,
        "gift_status": gift_status,
        "recent_memories": recent_memories,
        "meeting_ask_section": meeting_ask_section,
    }


async def ensure_assistant_server_url(api_key: str, assistant_id: str, app_base: str) -> None:
    """
    Patch the Vapi assistant to set serverUrl if it's not already configured.
    The serverUrl is required for Vapi to send end-of-call webhooks.
    """
    if not app_base:
        logger.warning("APP_BASE not set — Vapi will not send end-of-call webhooks")
        return

    webhook_url = f"{app_base.rstrip('/')}/webhook/vapi"

    try:
        async with httpx.AsyncClient() as client:
            # Fetch current assistant
            resp = await client.get(
                f"{VAPI_ASSISTANT_URL}/{assistant_id}",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10.0,
            )
            resp.raise_for_status()
            assistant = resp.json()

            current_url = assistant.get("serverUrl") or ""
            if current_url == webhook_url:
                logger.info("Vapi assistant serverUrl already set to %s", webhook_url)
                return

            # Patch with the correct serverUrl
            patch_resp = await client.patch(
                f"{VAPI_ASSISTANT_URL}/{assistant_id}",
                json={"serverUrl": webhook_url},
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=10.0,
            )
            patch_resp.raise_for_status()
            logger.info("Patched Vapi assistant serverUrl to %s", webhook_url)
    except Exception as exc:
        logger.error("Failed to patch Vapi assistant serverUrl: %s", exc)


async def initiate_call(contact: Contact, *, occasion: str = "", gift_summary: str = "") -> VapiCallResponse:
    """
    Calls POST /call on the Vapi API.
    Routes to phone (PSTN) or SIP based on contact.contact_method.
    Raises AlreadyOnCallError if contact is already active.
    On Vapi API error: logs, sets last_call_outcome = no_answer, does not raise.
    Persists call_started_at to the Contact DB record.
    """
    from app.config import get_settings
    from app.db import get_db

    if contact.contact_id in _active_calls:
        raise AlreadyOnCallError(
            f"Contact {contact.contact_id} ({contact.name}) is already on an active call."
        )

    settings = get_settings()

    if not _is_valid_uuid(settings.VAPI_PHONE_NUMBER_ID):
        logger.error(
            "Invalid VAPI_PHONE_NUMBER_ID configured: '%s'. Expected UUID.",
            settings.VAPI_PHONE_NUMBER_ID,
        )
        await _set_no_answer(contact)
        return None  # type: ignore[return-value]

    # Fetch gift delivery status for this contact
    from app.services.gifting import get_gift_delivery_context
    gift_status = await get_gift_delivery_context(contact.contact_id)

    # Pre-fetch top memories from Qdrant and inject into variableValues so the
    # assistant has them even if the get_memory tool call fails mid-call.
    recent_memories_text = ""
    try:
        from app.services.qdrant import search_memory
        tags_joined = " ".join(contact.tags) if contact.tags else ""
        query = f"{contact.name} {tags_joined} {contact.last_call_note or ''}".strip()
        entries = await search_memory(contact.contact_id, query, top_k=8)
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        if entries:
            lines = [f"- [{e.type}] {e.text}" for e in entries]
            recent_memories_text = "\n".join(lines)
            logger.info("Pre-fetched %d memories for contact %s", len(entries), contact.contact_id)
    except Exception as exc:
        logger.warning("Could not pre-fetch memories for contact %s: %s", contact.contact_id, exc)

    # Per-call variable values injected into the assistant's {{variable}} placeholders
    variable_values = _build_variable_values(
        contact, settings.USER_NAME, occasion=occasion, gift_summary=gift_summary,
        gift_status=gift_status, recent_memories=recent_memories_text,
    )

    assistant_overrides = {
        "variableValues": variable_values,
    }

    # metadata carries contact_id so the end-of-call webhook can identify the contact
    metadata = {"contact_id": contact.contact_id, "contact_name": contact.name}

    # Build the Vapi payload based on contact method
    if contact.contact_method == "sip":
        # SIP calls require a SIP trunk phone number ID, not a PSTN one.
        # Free-tier PSTN numbers block all non-US calls including SIP.
        # Set VAPI_SIP_TRUNK_ID in .env (Vapi dashboard → Phone Numbers → Add → SIP Trunk).
        sip_trunk_id = settings.VAPI_SIP_TRUNK_ID or settings.VAPI_PHONE_NUMBER_ID
        payload = {
            "assistantId": settings.VAPI_ASSISTANT_ID,
            "assistantOverrides": assistant_overrides,
            "metadata": metadata,
            "phoneNumberId": sip_trunk_id,
            "customer": {"sipUri": contact.sip},
        }
    else:
        payload = {
            "assistantId": settings.VAPI_ASSISTANT_ID,
            "assistantOverrides": assistant_overrides,
            "metadata": metadata,
            "phoneNumberId": settings.VAPI_PHONE_NUMBER_ID,
            "customer": {"number": contact.phone},
        }

    call_started_at = datetime.now(UTC)

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                VAPI_CALL_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.VAPI_API_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text
        logger.error(
            "Vapi API error for contact %s: %s %s",
            contact.contact_id,
            exc.response.status_code,
            body,
        )
        await _set_no_answer(contact)
        raise VapiError(body) from exc
    except httpx.RequestError as exc:
        logger.error("Vapi request error for contact %s: %s", contact.contact_id, exc)
        await _set_no_answer(contact)
        raise VapiError(str(exc)) from exc

    # Register in active-call guard
    _active_calls[contact.contact_id] = call_started_at

    # Persist call_started_at to the Contact DB record
    db = await get_db()
    try:
        await db.execute(
            "UPDATE contacts SET call_started_at = ? WHERE contact_id = ?",
            (call_started_at.isoformat(), contact.contact_id),
        )
        await db.commit()
    finally:
        await db.close()

    call_id = data.get("id", "")
    logger.info("Initiated call %s for contact %s", call_id, contact.contact_id)
    return VapiCallResponse(call_id=call_id, raw=data)


def mark_call_ended(contact_id: str) -> None:
    """Called by the webhook handler when a call ends to release the guard."""
    removed = _active_calls.pop(contact_id, None)
    if removed is not None:
        logger.info("Released active-call guard for contact %s", contact_id)
    else:
        logger.debug("mark_call_ended called for contact %s but it was not in _active_calls", contact_id)


def sweep_stale_active_calls(max_age_minutes: int = 30) -> None:
    """
    Called by the polling loop. Releases any contact stuck in _active_calls
    for longer than max_age_minutes (handles missed webhooks).
    """
    now = datetime.now(UTC)
    stale = [
        contact_id
        for contact_id, started_at in list(_active_calls.items())
        if (now - started_at).total_seconds() > max_age_minutes * 60
    ]
    for contact_id in stale:
        _active_calls.pop(contact_id, None)
        logger.warning(
            "Swept stale active call for contact %s (older than %d minutes)",
            contact_id,
            max_age_minutes,
        )


async def _set_no_answer(contact: Contact) -> None:
    """Helper: set last_call_outcome = no_answer in the DB."""
    from app.db import get_db

    db = await get_db()
    try:
        await db.execute(
            "UPDATE contacts SET last_call_outcome = 'no_answer' WHERE contact_id = ?",
            (contact.contact_id,),
        )
        await db.commit()
    except Exception as exc:
        logger.error("Failed to update last_call_outcome for contact %s: %s", contact.contact_id, exc)
    finally:
        await db.close()
