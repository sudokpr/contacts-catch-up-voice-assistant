"""
Webhook handler for Vapi post-call events.

Vapi wraps the entire event under a top-level `message` key. All top-level
fields (type, call, transcript, etc.) are null — the real data lives at
message.type, message.call, message.transcript, etc.

Returns 200 immediately and offloads all processing to a background task
to avoid Vapi webhook timeouts.
"""

import logging
from datetime import datetime, UTC, timedelta
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel, Field

from app.services.vapi import mark_call_ended

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic models — structured to match the actual Vapi payload shape
# ---------------------------------------------------------------------------

class VapiCallMetadata(BaseModel):
    contact_id: Optional[str] = None
    model_config = {"extra": "allow"}


class VapiAssistantOverrides(BaseModel):
    variable_values: Optional[dict] = Field(None, alias="variableValues")
    model_config = {"extra": "allow", "populate_by_name": True}


class VapiCall(BaseModel):
    id: str = ""
    type: Optional[str] = None
    ended_reason: Optional[str] = Field(None, alias="endedReason")
    metadata: Optional[VapiCallMetadata] = None
    assistant_overrides: Optional[VapiAssistantOverrides] = Field(None, alias="assistantOverrides")
    model_config = {"extra": "allow", "populate_by_name": True}


class VapiAnalysis(BaseModel):
    summary: Optional[str] = None
    model_config = {"extra": "allow"}


class VapiMessage(BaseModel):
    """
    The actual Vapi event. Vapi wraps every webhook payload under a 'message'
    key — the real type, call, transcript, etc. live here.
    """
    type: Optional[str] = None
    call: Optional[VapiCall] = None
    transcript: Optional[str] = None
    summary: Optional[str] = None          # direct summary field on message
    analysis: Optional[VapiAnalysis] = None
    artifact: Optional[dict] = None
    ended_reason: Optional[str] = Field(None, alias="endedReason")
    started_at: Optional[str] = Field(None, alias="startedAt")
    ended_at: Optional[str] = Field(None, alias="endedAt")
    model_config = {"extra": "allow", "populate_by_name": True}


class VapiWebhookPayload(BaseModel):
    """
    Vapi wraps the event under 'message'. Top-level fields are mostly null.
    Use the convenience properties below to access data.
    """
    message: Optional[VapiMessage] = None
    model_config = {"extra": "allow"}

    # --- convenience properties ---

    @property
    def event_type(self) -> Optional[str]:
        return self.message.type if self.message else None

    @property
    def call_id(self) -> str:
        if self.message and self.message.call:
            return self.message.call.id
        return ""

    @property
    def ended_reason(self) -> Optional[str]:
        if self.message:
            # prefer message-level endedReason, fall back to call-level
            if self.message.ended_reason:
                return self.message.ended_reason
            if self.message.call and self.message.call.ended_reason:
                return self.message.call.ended_reason
        return None

    @property
    def transcript(self) -> Optional[str]:
        return self.message.transcript if self.message else None

    @property
    def artifact(self) -> Optional[dict]:
        return self.message.artifact if self.message else None

    @property
    def summary(self) -> Optional[str]:
        if not self.message:
            return None
        # Try analysis.summary first (Vapi analysisPlan), then direct message.summary
        if self.message.analysis and self.message.analysis.summary:
            return self.message.analysis.summary
        return self.message.summary

    @property
    def contact_id(self) -> Optional[str]:
        if not self.message or not self.message.call:
            return None

        call = self.message.call

        # 1. assistantOverrides.variableValues.contact_id — primary path for all calls
        #    (web calls, PSTN calls all inject this via variableValues)
        if call.assistant_overrides and call.assistant_overrides.variable_values:
            cid = call.assistant_overrides.variable_values.get("contact_id")
            if cid:
                logger.info("contact_id found via assistantOverrides.variableValues: %s", cid)
                return cid

        # 2. Raw model_extra — in case Pydantic aliasing missed a field
        raw = call.model_extra or {}
        for key in ("assistantOverrides", "assistant_overrides"):
            overrides = raw.get(key) or {}
            if isinstance(overrides, dict):
                vv = overrides.get("variableValues") or overrides.get("variable_values") or {}
                cid = vv.get("contact_id") if isinstance(vv, dict) else None
                if cid:
                    logger.info("contact_id found via raw model_extra[%s].variableValues", key)
                    return cid

        # 3. Web call registry — for WebRTC calls registered via /api/calls/web-register
        if call.id:
            from app.services.vapi import get_web_call_contact
            cid = get_web_call_contact(call.id)
            if cid:
                logger.info("contact_id found via web call registry for call %s", call.id)
                return cid

        # 4. metadata.contact_id — PSTN/SIP backend calls set this
        if call.metadata and call.metadata.contact_id:
            return call.metadata.contact_id

        logger.warning(
            "contact_id not found in webhook payload for call %s (call type=%s)",
            call.id, call.type,
        )
        return None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def is_call_already_processed(call_id: str) -> bool:
    """Return True if this call_id is already in processed_calls."""
    from app.db import get_db
    if not call_id:
        return False
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT 1 FROM processed_calls WHERE call_id = ?", (call_id,)
        )
        return await cursor.fetchone() is not None
    except Exception:
        return False
    finally:
        await db.close()


async def mark_call_as_processing(call_id: str) -> None:
    """Insert call_id into processed_calls to claim idempotency."""
    from app.db import get_db
    if not call_id:
        return
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO processed_calls (call_id, processed_at) VALUES (?, ?)",
            (call_id, datetime.now(UTC).isoformat()),
        )
        await db.commit()
    except Exception as exc:
        logger.error("Failed to mark call %s as processing: %s", call_id, exc)
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------

_ANSWERED_REASONS = {
    "customer-ended-call", "assistant-ended-call", "silence-timed-out",
    "max-duration-exceeded", "pipeline-error-openai-voice-failed",
    "hangup", "completed",
}
_BUSY_REASONS = {
    "customer-busy", "voicemail", "no-answer",
}
_NO_ANSWER_REASONS = {
    "customer-did-not-answer", "failed", "error", "cancelled",
    "customer-did-not-give-microphone-permission",
}


def classify_outcome(ended_reason: Optional[str]) -> str:
    if ended_reason in _ANSWERED_REASONS:
        return "answered"
    if ended_reason in _BUSY_REASONS:
        return "busy"
    return "no_answer"


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------



async def process_call_webhook(payload: VapiWebhookPayload) -> None:
    """
    Background task: full post-call webhook processing.

    Steps:
    1. Classify outcome (rule-based; LLM fallback if ambiguous)
    2. If answered: read summary from Vapi analysis, highlights from artifact.messages
    3. Store highlights as MemoryEntry objects in Qdrant
    4. Update contact fields in DB
    5. If commitment phrase detected: schedule follow-up call
    6. Release active-call guard and notify SSE clients
    """
    from app.db import get_db
    from app.models.memory import MemoryEntry
    from app.services.qdrant import store_memory

    call_id = payload.call_id
    contact_id = payload.contact_id
    ended_reason = payload.ended_reason

    logger.info(
        "process_call_webhook: call_id=%s contact_id=%s ended_reason=%s transcript_len=%s",
        call_id, contact_id, ended_reason,
        len(payload.transcript) if payload.transcript else 0,
    )

    outcome = classify_outcome(ended_reason)
    logger.info("process_call_webhook: outcome=%s for call %s", outcome, call_id)

    summary = ""

    if outcome == "answered":
        summary = payload.summary or ""
        if summary:
            logger.info("process_call_webhook: got summary (%d chars)", len(summary))
        elif payload.transcript:
            summary = payload.transcript[:1000]
            logger.info("process_call_webhook: no Vapi summary, using transcript snippet")

        if contact_id:
            if summary:
                try:
                    await store_memory(MemoryEntry(contact_id=contact_id, type="summary", text=summary))
                except Exception as exc:
                    logger.error("Failed to store summary memory for contact %s: %s", contact_id, exc)

    if contact_id:
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        try:
            db = await get_db()
            try:
                if outcome == "answered":
                    await db.execute(
                        """UPDATE contacts SET
                            last_called = ?, last_spoken = ?, last_call_outcome = ?,
                            last_call_note = ?, call_started_at = NULL
                        WHERE contact_id = ?""",
                        (now_iso, now_iso, outcome, summary or None, contact_id),
                    )
                else:
                    await db.execute(
                        """UPDATE contacts SET
                            last_called = ?, last_call_outcome = ?,
                            last_call_note = NULL, call_started_at = NULL
                        WHERE contact_id = ?""",
                        (now_iso, outcome, contact_id),
                    )
                await db.commit()
                logger.info("Updated contact %s after call: outcome=%s", contact_id, outcome)
            finally:
                await db.close()
        except Exception as exc:
            logger.error("Failed to update contact %s after webhook: %s", contact_id, exc)

        # Commitment phrase detection → schedule follow-up
        _COMMITMENT_PHRASES = [
            ("next quarter", 90), ("next month", 30), ("in a few weeks", 21),
            ("catch up soon", 21), ("next week", 7), ("let's reconnect", 60),
            ("talk soon", 14), ("call me after an hour", 1),
        ]
        if summary and outcome == "answered":
            for phrase, days in _COMMITMENT_PHRASES:
                if phrase.lower() in summary.lower():
                    follow_up_at = now + timedelta(days=days)
                    try:
                        await store_memory(MemoryEntry(
                            contact_id=contact_id,
                            type="commitment",
                            text=f"Follow-up commitment: ~{days} days (detected: '{phrase}')",
                        ))
                        from app.workers.scheduler import schedule_one_off_call
                        schedule_one_off_call(contact_id, follow_up_at)
                        logger.info(
                            "Commitment scheduled for contact %s: '%s' → %d days",
                            contact_id, phrase, days,
                        )
                    except Exception as exc:
                        logger.error("Failed to schedule commitment for contact %s: %s", contact_id, exc)
                    break

    # Release active-call guard and notify SSE
    if contact_id:
        mark_call_ended(contact_id)
        from app.sse_bus import publish
        await publish(contact_id, {
            "type": "call-ended",
            "outcome": outcome,
            "summary": summary or None,
            "transcript": payload.transcript,
            "call_id": call_id,
        })
        logger.info("SSE published call-ended for contact %s", contact_id)
    else:
        logger.warning(
            "process_call_webhook: no contact_id resolved for call %s — "
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
    Returns 200 immediately; processing is offloaded to a background task.
    """
    call_id = payload.call_id
    event_type = payload.event_type

    logger.info(
        "vapi_webhook: type=%s call_id=%s contact_id=%s",
        event_type, call_id, payload.contact_id,
    )

    contact_id = payload.contact_id
    msg = payload.message
    extra = (msg.model_extra or {}) if msg else {}

    # --- assistant.started: call connected, agent is live ---
    if event_type == "assistant-started":
        if contact_id:
            from app.sse_bus import publish
            await publish(contact_id, {"type": "call-connected", "call_id": call_id})
        return {"status": "ok"}

    # --- speech-update: speaking indicator ---
    if event_type == "speech-update":
        if contact_id:
            from app.sse_bus import publish
            await publish(contact_id, {
                "type": "speech-update",
                "role": extra.get("role"),
                "status": extra.get("status"),
            })
        return {"status": "ok"}

    # --- transcript: final transcript turn from Vapi ---
    if event_type == "transcript":
        if contact_id and extra.get("transcriptType") == "final":
            from app.sse_bus import publish
            await publish(contact_id, {
                "type": "transcript-update",
                "role": extra.get("role"),
                "transcript": extra.get("transcript") or "",
            })
        return {"status": "ok"}

    # --- conversation-update: full message list updated ---
    if event_type == "conversation-update":
        if contact_id:
            messages = extra.get("messages") or []
            if messages:
                last = messages[-1]
                role = last.get("role")
                text = last.get("message") or last.get("content") or ""
                if text:
                    from app.sse_bus import publish
                    await publish(contact_id, {
                        "type": "transcript-update",
                        "role": role,
                        "transcript": text,
                    })
        return {"status": "ok"}

    # --- status-update: call status changed (ringing, in-progress, etc.) ---
    if event_type == "status-update":
        if contact_id:
            from app.sse_bus import publish
            await publish(contact_id, {
                "type": "status-update",
                "status": extra.get("status"),
                "call_id": call_id,
            })
        return {"status": "ok"}

    # Only do heavy processing for end-of-call-report
    if event_type and event_type != "end-of-call-report":
        logger.info("vapi_webhook: ignoring event type=%s", event_type)
        return {"status": "ignored", "reason": f"event type '{event_type}' not processed"}

    # Idempotency guard
    if call_id and await is_call_already_processed(call_id):
        logger.info("vapi_webhook: call_id=%s already processed, skipping", call_id)
        return {"status": "already_processed"}

    if call_id:
        await mark_call_as_processing(call_id)

    background_tasks.add_task(process_call_webhook, payload)
    return {"status": "accepted"}
