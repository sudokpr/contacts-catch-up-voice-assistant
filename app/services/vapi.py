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

logger = logging.getLogger(__name__)

# contact_id -> call_started_at  (dict, not set — needed for TTL sweep)
_active_calls: dict[str, datetime] = {}

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


def _build_variable_values(contact: Contact, user_name: str) -> dict:
    """
    Build per-call variable values that are injected into the assistant's system prompt
    via Vapi's {{variable}} template substitution.

    The assistant's base prompt uses these placeholders:
      {{user_name}}, {{contact_id}}, {{contact_name}}, {{contact_tags}}, {{last_call_note}}
    """
    return {
        "user_name": user_name,
        "contact_id": contact.contact_id,
        "contact_name": contact.name,
        "contact_tags": ", ".join(contact.tags) if contact.tags else "none",
        "last_call_note": contact.last_call_note or "No previous calls recorded.",
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


async def initiate_call(contact: Contact) -> VapiCallResponse:
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

    if contact.contact_method != "sip" and not _is_valid_uuid(settings.VAPI_PHONE_NUMBER_ID):
        logger.error(
            "Invalid VAPI_PHONE_NUMBER_ID configured: '%s'. Expected UUID.",
            settings.VAPI_PHONE_NUMBER_ID,
        )
        await _set_no_answer(contact)
        return None  # type: ignore[return-value]

    # Per-call variable values injected into the assistant's {{variable}} placeholders
    variable_values = _build_variable_values(contact, settings.USER_NAME)

    assistant_overrides = {
        "variableValues": variable_values,
    }

    # metadata carries contact_id so the end-of-call webhook can identify the contact
    metadata = {"contact_id": contact.contact_id, "contact_name": contact.name}

    # Build the Vapi payload based on contact method
    if contact.contact_method == "sip":
        payload = {
            "assistantId": settings.VAPI_ASSISTANT_ID,
            "assistantOverrides": assistant_overrides,
            "metadata": metadata,
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
        logger.error(
            "Vapi API error for contact %s: %s %s",
            contact.contact_id,
            exc.response.status_code,
            exc.response.text,
        )
        await _set_no_answer(contact)
        return None  # type: ignore[return-value]
    except httpx.RequestError as exc:
        logger.error("Vapi request error for contact %s: %s", contact.contact_id, exc)
        await _set_no_answer(contact)
        return None  # type: ignore[return-value]

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
