"""
Webhook handler for Vapi post-call events.

Returns 200 immediately and offloads all processing to a background task
to avoid Vapi webhook timeouts.
"""

import logging
from datetime import datetime, timedelta, UTC
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel, Field

from app.services.vapi import mark_call_ended

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic models for the Vapi webhook payload
# ---------------------------------------------------------------------------

class VapiCallMetadata(BaseModel):
    contact_id: Optional[str] = None

    model_config = {"extra": "allow"}


class VapiCall(BaseModel):
    id: str = ""
    ended_reason: Optional[str] = Field(None, alias="endedReason")
    metadata: Optional[VapiCallMetadata] = None

    model_config = {"extra": "allow", "populate_by_name": True}


class VapiCustomer(BaseModel):
    number: Optional[str] = None

    model_config = {"extra": "allow"}


class VapiAssistant(BaseModel):
    id: Optional[str] = None

    model_config = {"extra": "allow"}


class VapiWebhookPayload(BaseModel):
    type: Optional[str] = None           # e.g. "end-of-call-report", "status-update", etc.
    call: Optional[VapiCall] = None
    transcript: Optional[str] = None
    customer: Optional[VapiCustomer] = None
    assistant: Optional[VapiAssistant] = None

    model_config = {"extra": "allow"}

    @property
    def call_id(self) -> str:
        return self.call.id if self.call else ""

    @property
    def contact_id(self) -> Optional[str]:
        if self.call and self.call.metadata:
            return self.call.metadata.contact_id
        return None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def is_call_already_processed(call_id: str) -> bool:
    """Return True if this call_id is already in processed_calls."""
    from app.db import get_db

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT 1 FROM processed_calls WHERE call_id = ?", (call_id,)
        )
        row = await cursor.fetchone()
        return row is not None
    finally:
        await db.close()


async def mark_call_as_processing(call_id: str) -> None:
    """Insert call_id into processed_calls to claim idempotency."""
    from app.db import get_db

    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO processed_calls (call_id, processed_at) VALUES (?, ?)",
            (call_id, datetime.now(UTC).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------

_ANSWERED_REASONS = {"customer-ended-call", "assistant-ended-call", "hangup"}
_BUSY_REASONS = {"busy", "line-busy"}
_NO_ANSWER_REASONS = {"no-answer", "voicemail", "failed", "error"}


async def classify_outcome(ended_reason: Optional[str]) -> str:
    """
    Classify call outcome as 'answered', 'busy', or 'no_answer'.
    Uses rule-based logic first; falls back to LLM for ambiguous/unknown reasons.
    Always returns one of the three valid values.
    """
    if ended_reason in _ANSWERED_REASONS:
        return "answered"
    if ended_reason in _BUSY_REASONS:
        return "busy"
    if ended_reason in _NO_ANSWER_REASONS:
        return "no_answer"

    # Ambiguous / unknown — try LLM fallback
    if ended_reason:
        try:
            from app.config import get_settings
            from openai import AsyncOpenAI

            settings = get_settings()
            client = AsyncOpenAI(
                base_url=settings.OPENAI_BASE_URL,
                api_key=settings.OPENAI_API_KEY,
            )
            prompt = (
                f"A phone call ended with reason: '{ended_reason}'. "
                "Classify this as exactly one of: answered, busy, no_answer. "
                "Reply with only the single word."
            )
            response = await client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            result = (response.choices[0].message.content or "").strip().lower()
            if result in {"answered", "busy", "no_answer"}:
                return result
        except Exception as exc:
            logger.warning("LLM outcome classification failed for reason '%s': %s", ended_reason, exc)

    return "no_answer"


# ---------------------------------------------------------------------------
# Callback parsing
# ---------------------------------------------------------------------------

def _parse_relative_delta(value: str) -> Optional[timedelta]:
    """Parse strings like '1 hour', '30 minutes', '2 days' into a timedelta."""
    import re

    value = value.strip().lower()
    patterns = [
        (r"(\d+)\s*day", lambda n: timedelta(days=int(n))),
        (r"(\d+)\s*hour", lambda n: timedelta(hours=int(n))),
        (r"(\d+)\s*minute", lambda n: timedelta(minutes=int(n))),
        (r"(\d+)\s*week", lambda n: timedelta(weeks=int(n))),
    ]
    for pattern, builder in patterns:
        m = re.search(pattern, value)
        if m:
            return builder(m.group(1))
    return None


def parse_callback_run_at(callback_type: str, callback_value: str) -> Optional[datetime]:
    """
    Parse a callback intent into an absolute UTC datetime.
    Returns None if parsing fails.
    """
    now = datetime.now(UTC)
    if callback_type == "relative":
        delta = _parse_relative_delta(callback_value)
        if delta is not None:
            return now + delta
        logger.warning("Could not parse relative callback value: '%s'", callback_value)
        return None
    elif callback_type == "absolute":
        try:
            dt = datetime.fromisoformat(callback_value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except ValueError as exc:
            logger.warning("Could not parse absolute callback value '%s': %s", callback_value, exc)
            return None
    return None


# ---------------------------------------------------------------------------
# Background task — full implementation
# ---------------------------------------------------------------------------

async def process_call_webhook(payload: VapiWebhookPayload) -> None:
    """
    Background task: full post-call webhook processing.

    Steps:
    1. Classify outcome (rule-based; LLM fallback if ambiguous)
    2. If answered: extract from transcript via LLM
    3. Store highlights and facts as MemoryEntry objects
    4. Update contact fields in DB
    5. If callback requested: schedule one-off call
    6. Release active-call guard via mark_call_ended
    """
    from app.db import get_db
    from app.models.memory import MemoryEntry
    from app.services.qdrant import store_memory
    from app.services.llm import extract_from_transcript
    from app.workers.scheduler import schedule_one_off_call

    call_id = payload.call_id
    contact_id = payload.contact_id
    ended_reason = payload.call.ended_reason if payload.call else None

    logger.info(
        "process_call_webhook: call_id=%s contact_id=%s ended_reason=%s transcript_length=%s",
        call_id,
        contact_id,
        ended_reason,
        len(payload.transcript) if payload.transcript else 0,
    )

    # --- Step 1: Classify outcome ---
    outcome = await classify_outcome(ended_reason)
    logger.info("process_call_webhook: classified outcome=%s for call %s", outcome, call_id)

    # --- Step 2: Extract from transcript if answered ---
    summary = ""
    callback = None
    call_time_preference = "none"

    if outcome == "answered" and payload.transcript:
        extraction = await extract_from_transcript(payload.transcript)
        summary = extraction.summary
        callback = extraction.callback
        call_time_preference = extraction.call_time_preference

        # --- Step 3: Store highlights and facts as memory entries ---
        if contact_id:
            for highlight in extraction.highlights:
                entry = MemoryEntry(
                    contact_id=contact_id,
                    type="highlight",
                    text=highlight,
                )
                try:
                    await store_memory(entry)
                except Exception as exc:
                    logger.error("Failed to store highlight for contact %s: %s", contact_id, exc)

            for fact in extraction.facts:
                # facts are dicts like {"key": "...", "value": "..."}
                fact_text = f"{fact.get('key', '')}: {fact.get('value', '')}" if isinstance(fact, dict) else str(fact)
                entry = MemoryEntry(
                    contact_id=contact_id,
                    type="fact",
                    text=fact_text,
                )
                try:
                    await store_memory(entry)
                except Exception as exc:
                    logger.error("Failed to store fact for contact %s: %s", contact_id, exc)
    else:
        extraction = None

    # --- Step 4: Update contact in DB ---
    if contact_id:
        now = datetime.now(UTC)
        now_iso = now.isoformat()

        try:
            db = await get_db()
            try:
                if outcome == "answered":
                    await db.execute(
                        """UPDATE contacts SET
                            last_called = ?,
                            last_spoken = ?,
                            last_call_outcome = ?,
                            last_call_note = ?,
                            call_time_preference = ?,
                            call_started_at = NULL
                        WHERE contact_id = ?""",
                        (
                            now_iso,
                            now_iso,
                            outcome,
                            summary or None,
                            call_time_preference,
                            contact_id,
                        ),
                    )
                else:
                    await db.execute(
                        """UPDATE contacts SET
                            last_called = ?,
                            last_call_outcome = ?,
                            last_call_note = NULL,
                            call_started_at = NULL
                        WHERE contact_id = ?""",
                        (now_iso, outcome, contact_id),
                    )
                await db.commit()
                logger.info("Updated contact %s after call: outcome=%s", contact_id, outcome)
            finally:
                await db.close()
        except Exception as exc:
            logger.error("Failed to update contact %s after webhook: %s", contact_id, exc)

        # --- Step 5: Schedule callback if requested ---
        if callback and callback.type != "none" and callback.value:
            run_at = parse_callback_run_at(callback.type, callback.value)
            if run_at is not None:
                try:
                    db = await get_db()
                    try:
                        await db.execute(
                            "UPDATE contacts SET next_call_at = ? WHERE contact_id = ?",
                            (run_at.isoformat(), contact_id),
                        )
                        await db.commit()
                    finally:
                        await db.close()
                    schedule_one_off_call(contact_id, run_at)
                    logger.info(
                        "Scheduled callback for contact %s at %s (type=%s value=%s)",
                        contact_id,
                        run_at.isoformat(),
                        callback.type,
                        callback.value,
                    )
                except Exception as exc:
                    logger.error("Failed to schedule callback for contact %s: %s", contact_id, exc)
        else:
            try:
                db = await get_db()
                try:
                    await db.execute(
                        "UPDATE contacts SET next_call_at = NULL WHERE contact_id = ?",
                        (contact_id,),
                    )
                    await db.commit()
                finally:
                    await db.close()
            except Exception as exc:
                logger.error("Failed to clear next_call_at for contact %s: %s", contact_id, exc)

    # --- Step 6: Release active-call guard ---
    if contact_id:
        mark_call_ended(contact_id)
    else:
        logger.warning(
            "process_call_webhook: no contact_id in payload for call %s — "
            "active-call guard not released",
            call_id,
        )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.post("/webhook/vapi")
async def vapi_webhook(
    payload: VapiWebhookPayload,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """
    Receive a post-call event from Vapi.

    Returns 200 immediately; all processing is offloaded to a background task
    to avoid Vapi webhook timeouts.
    """
    call_id = payload.call_id
    event_type = payload.type or "unknown"

    logger.info(
        "vapi_webhook received: type=%s call_id=%s contact_id=%s",
        event_type,
        call_id,
        payload.contact_id,
    )

    # Only process end-of-call events — ignore status-updates, speech-updates, etc.
    if payload.type and payload.type != "end-of-call-report":
        logger.debug("vapi_webhook: ignoring event type=%s", payload.type)
        return {"status": "ignored", "reason": f"event type '{payload.type}' not processed"}

    # Idempotency guard — skip duplicate deliveries
    if call_id and await is_call_already_processed(call_id):
        logger.info("vapi_webhook: call_id=%s already processed, skipping", call_id)
        return {"status": "already_processed"}

    # Claim this call_id before handing off to background task
    if call_id:
        await mark_call_as_processing(call_id)

    background_tasks.add_task(process_call_webhook, payload)

    return {"status": "accepted"}
