"""
Webhook handler for Vapi post-call events.

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
# Pydantic models for the Vapi webhook payload
# ---------------------------------------------------------------------------

class VapiCallMetadata(BaseModel):
    contact_id: Optional[str] = None

    model_config = {"extra": "allow"}


class VapiAssistantOverrides(BaseModel):
    variable_values: Optional[dict] = Field(None, alias="variableValues")

    model_config = {"extra": "allow", "populate_by_name": True}


class VapiCall(BaseModel):
    id: str = ""
    ended_reason: Optional[str] = Field(None, alias="endedReason")
    metadata: Optional[VapiCallMetadata] = None
    assistant_overrides: Optional[VapiAssistantOverrides] = Field(None, alias="assistantOverrides")

    model_config = {"extra": "allow", "populate_by_name": True}


class VapiCustomer(BaseModel):
    number: Optional[str] = None

    model_config = {"extra": "allow"}


class VapiAssistant(BaseModel):
    id: Optional[str] = None

    model_config = {"extra": "allow"}


class VapiAnalysis(BaseModel):
    summary: Optional[str] = None

    model_config = {"extra": "allow"}


class VapiWebhookPayload(BaseModel):
    type: Optional[str] = None           # e.g. "end-of-call-report", "status-update", etc.
    call: Optional[VapiCall] = None
    transcript: Optional[str] = None
    analysis: Optional[VapiAnalysis] = None
    artifact: Optional[dict] = None      # raw artifact blob (messages, recordings, etc.)
    customer: Optional[VapiCustomer] = None
    assistant: Optional[VapiAssistant] = None

    model_config = {"extra": "allow"}

    @property
    def call_id(self) -> str:
        return self.call.id if self.call else ""

    @property
    def contact_id(self) -> Optional[str]:
        if not self.call:
            return None

        # 1. metadata.contact_id — set when backend initiates PSTN/SIP call
        if self.call.metadata and self.call.metadata.contact_id:
            return self.call.metadata.contact_id

        # 2. Web call registry — browser registers call_id → contact_id via /api/calls/web-register
        if self.call.id:
            from app.services.vapi import get_web_call_contact
            cid = get_web_call_contact(self.call.id)
            if cid:
                return cid

        # 3. assistantOverrides.variableValues — Pydantic-parsed path
        if self.call.assistant_overrides:
            vv = self.call.assistant_overrides.variable_values or {}
            cid = vv.get("contact_id")
            if cid:
                return cid

        # 4. Raw model_extra — bypass Pydantic in case aliasing missed the field
        raw = self.call.model_extra or {}
        for key in ("assistantOverrides", "assistant_overrides"):
            overrides = raw.get(key) or {}
            if isinstance(overrides, dict):
                vv = overrides.get("variableValues") or overrides.get("variable_values") or {}
                cid = vv.get("contact_id") if isinstance(vv, dict) else None
                if cid:
                    logger.info("contact_id found via raw model_extra[%s]", key)
                    return cid

        logger.warning(
            "contact_id not found in webhook payload for call %s — "
            "metadata=%s extra_keys=%s",
            self.call.id,
            self.call.metadata,
            list(raw.keys()),
        )
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
# Background task — full implementation
# ---------------------------------------------------------------------------

def _extract_contact_turns(artifact: Optional[dict]) -> list[str]:
    """
    Pull out the contact's (user role) spoken lines from artifact.messages.
    These are stored as highlights — no LLM needed.
    """
    if not artifact:
        return []
    messages = artifact.get("messages") or []
    turns = []
    for msg in messages:
        if msg.get("role") == "user" and msg.get("message", "").strip():
            text = msg["message"].strip()
            if len(text) > 10:  # skip very short utterances
                turns.append(text)
    return turns


async def process_call_webhook(payload: VapiWebhookPayload) -> None:
    """
    Background task: full post-call webhook processing.

    Steps:
    1. Classify outcome (rule-based; LLM fallback if ambiguous)
    2. If answered: read summary from Vapi analysis, highlights from artifact.messages
    3. Store highlights as MemoryEntry objects in Qdrant
    4. Update contact fields in DB
    5. If callback requested: schedule one-off call
    6. Release active-call guard via mark_call_ended
    """
    from app.db import get_db
    from app.models.memory import MemoryEntry
    from app.services.qdrant import store_memory
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

    # --- Step 2: Read summary from Vapi analysis; highlights from artifact ---
    summary = ""
    call_time_preference = "none"

    if outcome == "answered":
        # Summary generated by Vapi's summaryPlan (GPT-4, no local LLM needed)
        if payload.analysis and payload.analysis.summary:
            summary = payload.analysis.summary
            logger.info("process_call_webhook: got Vapi summary (%d chars)", len(summary))
        elif payload.transcript:
            # Fallback: use raw transcript as the note if no Vapi summary
            summary = payload.transcript[:1000]
            logger.info("process_call_webhook: no Vapi summary, using transcript snippet")

        # --- Step 3: Store contact's spoken turns as highlights in Qdrant ---
        if contact_id:
            highlights = _extract_contact_turns(payload.artifact)
            logger.info(
                "process_call_webhook: storing %d highlight turns for contact %s",
                len(highlights), contact_id,
            )
            for text in highlights:
                entry = MemoryEntry(
                    contact_id=contact_id,
                    type="highlight",
                    text=text,
                )
                try:
                    await store_memory(entry)
                except Exception as exc:
                    logger.error("Failed to store highlight for contact %s: %s", contact_id, exc)

            # Also store the summary itself as a fact entry for future retrieval
            if summary:
                try:
                    await store_memory(MemoryEntry(
                        contact_id=contact_id,
                        type="summary",
                        text=summary,
                    ))
                except Exception as exc:
                    logger.error("Failed to store summary memory for contact %s: %s", contact_id, exc)

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

        # --- Step 5: Detect commitment phrases → schedule follow-up + store commitment memory ---
        _COMMITMENT_PHRASES = [
            ("next quarter", 90),
            ("next month", 30),
            ("in a few weeks", 21),
            ("catch up soon", 21),
            ("next week", 7),
            ("let's reconnect", 60),
            ("talk soon", 14),
        ]
        if summary and outcome == "answered" and contact_id:
            for phrase, days in _COMMITMENT_PHRASES:
                if phrase.lower() in summary.lower():
                    follow_up_at = now + timedelta(days=days)
                    try:
                        await store_memory(MemoryEntry(
                            contact_id=contact_id,
                            type="commitment",
                            text=f"Follow-up commitment: ~{days} days out (detected phrase: '{phrase}')",
                        ))
                        from app.workers.scheduler import schedule_one_off_call
                        schedule_one_off_call(contact_id, follow_up_at)
                        logger.info(
                            "Commitment detected for contact %s: '%s' → follow-up in %d days (%s)",
                            contact_id, phrase, days, follow_up_at.isoformat()
                        )
                    except Exception as exc:
                        logger.error("Failed to schedule commitment follow-up for contact %s: %s", contact_id, exc)
                    break
        else:
            # Clear next_call_at if no commitment detected
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

    # --- Step 6: Release active-call guard and notify SSE clients ---
    if contact_id:
        mark_call_ended(contact_id)
        from app.sse_bus import publish
        await publish(contact_id, {
            "type": "call-ended",
            "outcome": outcome,
            "summary": summary or None,
            "call_id": call_id,
        })
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

    # Temporary diagnostic: log full payload to track down contact_id extraction failures
    try:
        import json as _json
        raw_body = payload.model_dump(mode="json")
        logger.info("vapi_webhook RAW PAYLOAD:\n%s", _json.dumps(raw_body, indent=2, default=str))
        if payload.call:
            logger.info(
                "vapi_webhook call fields: id=%s metadata=%s assistant_overrides=%s extra_keys=%s",
                payload.call.id,
                payload.call.metadata,
                getattr(payload.call, "assistant_overrides", None),
                list((payload.call.model_extra or {}).keys()),
            )
    except Exception as _dbg_exc:
        logger.warning("vapi_webhook debug dump failed: %s", _dbg_exc)

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

    # Publish status event to SSE bus so the UI live view updates immediately
    if payload.contact_id:
        from app.sse_bus import publish
        await publish(payload.contact_id, {
            "type": "status-update",
            "ended_reason": payload.call.ended_reason if payload.call else None,
            "call_id": call_id,
        })

    background_tasks.add_task(process_call_webhook, payload)

    return {"status": "accepted"}
