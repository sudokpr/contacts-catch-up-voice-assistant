import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Flexible Vapi tool-call request body
# ---------------------------------------------------------------------------

class ToolCallRequest(BaseModel):
    """
    Flexible model for Vapi tool-call payloads.
    Vapi sends: { "message": { "toolCallList": [...] }, ... }
    We also accept flat payloads with contact_id at the top level.
    All extra fields are allowed so we don't reject unexpected Vapi fields.
    """
    contact_id: Optional[str] = None
    query: Optional[str] = None
    text: Optional[str] = None
    # Calendar event fields
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    # Allow arbitrary extra fields from Vapi
    model_config = {"extra": "allow"}


def _dict_from_unknown(value: Any) -> dict[str, Any]:
    """Best-effort conversion of unknown tool argument container to dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _extract_tool_call_id(payload: dict[str, Any]) -> Optional[str]:
    """
    Extract the toolCallId from a Vapi tool-call envelope.
    Checks message.toolCallList[0].id and message.toolWithToolCallList[0].toolCall.id.
    """
    message = _dict_from_unknown(payload.get("message"))
    if not message:
        return None

    # Preferred: toolWithToolCallList[0].toolCall.id (most specific to this endpoint)
    tool_with_list = message.get("toolWithToolCallList") or []
    if isinstance(tool_with_list, list) and tool_with_list:
        tool_call = _dict_from_unknown(tool_with_list[0]).get("toolCall")
        if tool_call:
            call_id = _dict_from_unknown(tool_call).get("id")
            if call_id:
                return str(call_id)

    # Fallback: toolCallList[0].id
    tool_call_list = message.get("toolCallList") or message.get("toolCalls") or []
    if isinstance(tool_call_list, list) and tool_call_list:
        call_id = _dict_from_unknown(tool_call_list[0]).get("id")
        if call_id:
            return str(call_id)

    return None


def _vapi_response(tool_call_id: Optional[str], result: Any) -> dict[str, Any]:
    """
    Wrap a tool result in the Vapi-expected response envelope:
    { "results": [{ "toolCallId": "...", "result": ... }] }
    If no toolCallId is available (e.g. direct test calls), returns the result unwrapped.
    """
    if tool_call_id:
        return {"results": [{"toolCallId": tool_call_id, "result": result}]}
    return result if isinstance(result, dict) else {"result": result}


def _extract_tool_request(payload: dict[str, Any]) -> ToolCallRequest:
    """
    Extract tool arguments from either:
    1) flat payloads: {contact_id, query, ...}
    2) nested Vapi envelopes: {message: {toolCallList: [...]}}
    """
    candidate_dicts: list[dict[str, Any]] = []

    # Flat / direct forms
    candidate_dicts.append(payload)
    for key in ("arguments", "parameters", "args", "input"):
        candidate_dicts.append(_dict_from_unknown(payload.get(key)))

    # Nested single tool call forms
    for key in ("toolCall", "tool_call"):
        call_obj = _dict_from_unknown(payload.get(key))
        if call_obj:
            candidate_dicts.append(call_obj)
            for k in ("arguments", "parameters", "args", "input"):
                candidate_dicts.append(_dict_from_unknown(call_obj.get(k)))
            function_obj = _dict_from_unknown(call_obj.get("function"))
            if function_obj:
                candidate_dicts.append(function_obj)
                candidate_dicts.append(_dict_from_unknown(function_obj.get("arguments")))

    # Vapi message envelope
    message = _dict_from_unknown(payload.get("message"))
    if message:
        candidate_dicts.append(message)
        # toolWithToolCallList gives the arguments for this specific tool endpoint
        tool_with_list = message.get("toolWithToolCallList") or []
        if isinstance(tool_with_list, list) and tool_with_list:
            item = _dict_from_unknown(tool_with_list[0])
            tool_call = _dict_from_unknown(item.get("toolCall"))
            if tool_call:
                function_obj = _dict_from_unknown(tool_call.get("function"))
                if function_obj:
                    candidate_dicts.append(_dict_from_unknown(function_obj.get("arguments")))

        tool_calls = message.get("toolCallList") or message.get("toolCalls") or []
        if isinstance(tool_calls, list):
            for item in tool_calls:
                item_dict = _dict_from_unknown(item)
                if not item_dict:
                    continue
                candidate_dicts.append(item_dict)
                for k in ("arguments", "parameters", "args", "input"):
                    candidate_dicts.append(_dict_from_unknown(item_dict.get(k)))
                function_obj = _dict_from_unknown(item_dict.get("function"))
                if function_obj:
                    candidate_dicts.append(function_obj)
                    candidate_dicts.append(_dict_from_unknown(function_obj.get("arguments")))

    merged: dict[str, Any] = {}
    for d in candidate_dicts:
        for k, v in d.items():
            if k not in merged or merged[k] in (None, ""):
                merged[k] = v

    return ToolCallRequest.model_validate(merged)


# ---------------------------------------------------------------------------
# DB helper (shared)
# ---------------------------------------------------------------------------

async def _get_contact(contact_id: str):
    """Fetch a contact by ID or raise 404."""
    from app.db import get_db, row_to_contact

    db = await get_db()
    try:
        async with db.execute(
            "SELECT * FROM contacts WHERE contact_id = ?", (contact_id,)
        ) as cursor:
            row = await cursor.fetchone()
    finally:
        await db.close()

    if row is None:
        raise HTTPException(status_code=404, detail=f"Contact '{contact_id}' not found")

    return row_to_contact(row)


# ---------------------------------------------------------------------------
# Manual trigger endpoint (task 5)
# ---------------------------------------------------------------------------

@router.get("/config", summary="Public configuration for the browser client")
async def get_config():
    """Returns browser-safe config values (no secrets). Used by the Web SDK integration."""
    from app.config import get_settings
    settings = get_settings()
    return {
        "vapi_public_key": settings.VAPI_PUBLIC_KEY,
        "vapi_assistant_id": settings.VAPI_ASSISTANT_ID,
        "user_name": settings.USER_NAME,
    }


@router.post("/web-register", summary="Register a browser WebRTC call ID to a contact")
async def web_register_call(body: dict[str, Any]):
    """
    Called by the browser after vapi.start() resolves with a call ID.
    Stores vapi_call_id → contact_id so the webhook can resolve it reliably.
    """
    call_id = body.get("call_id")
    contact_id = body.get("contact_id")
    if not call_id or not contact_id:
        return {"status": "ignored"}
    from app.services.vapi import register_web_call
    register_web_call(str(call_id), str(contact_id))
    return {"status": "registered"}


@router.get("/active", summary="Return currently active call contact IDs")
async def active_calls():
    from app.services.vapi import _active_calls
    return {"active": list(_active_calls.keys())}


@router.get("/live/{contact_id}", summary="SSE stream of live call events for a contact")
async def call_live_stream(contact_id: str):
    """Server-Sent Events stream. Connect before or after triggering a call."""
    from app.sse_bus import sse_generator
    return StreamingResponse(
        sse_generator(contact_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/trigger/{contact_id}", summary="Manually trigger an outbound call for a contact")
async def trigger_call(contact_id: str, body: dict[str, Any] = {}):
    """
    Manual trigger endpoint — initiates an immediate outbound call via Vapi.
    Accepts optional JSON body: { "occasion": "birthday"|"diwali"|"deal_congratulations"|... }
    Useful for demo/smoke testing.
    """
    from app.services.vapi import initiate_call, AlreadyOnCallError, VapiError
    from app.services.gifting import choose_gifts_for_occasion, order_gift

    contact = await _get_contact(contact_id)
    occasion = (body or {}).get("occasion", "")

    gift_summary = ""
    if occasion and "send-gift" in (contact.tags or []):
        gift_types = choose_gifts_for_occasion(occasion)
        orders = []
        for gt in gift_types:
            order = await order_gift(contact, gt, occasion)
            if order:
                orders.append(order.description)
        gift_summary = "; ".join(orders)

    try:
        result = await initiate_call(contact, occasion=occasion, gift_summary=gift_summary)
    except AlreadyOnCallError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except VapiError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return {"status": "initiated", "call_id": result.call_id, "occasion": occasion}


# ---------------------------------------------------------------------------
# Tool endpoints — called by Vapi during a live call
# ---------------------------------------------------------------------------

@router.post("/tools/get_contact_context", summary="Return contact name, last interaction summary, and tags")
async def get_contact_context(body: dict[str, Any]):
    """
    Requirement 4.3: Returns the contact's name, last_call_note, and tags.
    """
    tool_call_id = _extract_tool_call_id(body)
    request = _extract_tool_request(body)
    if not request.contact_id:
        logger.warning("get_contact_context missing contact_id. payload_keys=%s", list(body.keys()))
        raise HTTPException(status_code=400, detail="contact_id is required")

    contact = await _get_contact(request.contact_id)

    return _vapi_response(tool_call_id, {
        "name": contact.name,
        "last_interaction_summary": contact.last_call_note,
        "tags": contact.tags,
    })


@router.post("/tools/get_memory", summary="Retrieve top semantic memories for a contact")
async def get_memory(body: dict[str, Any]):
    """
    Requirement 4.4: Performs a semantic search using an enriched default context
    query built from the contact's name, tags, and last_call_note.
    """
    tool_call_id = _extract_tool_call_id(body)
    request = _extract_tool_request(body)
    if not request.contact_id:
        logger.warning("get_memory missing contact_id. payload_keys=%s", list(body.keys()))
        raise HTTPException(status_code=400, detail="contact_id is required")

    contact = await _get_contact(request.contact_id)

    from app.services.qdrant import search_memory

    tags_joined = " ".join(contact.tags) if contact.tags else ""
    last_note = contact.last_call_note or ""
    query = f"{contact.name} {tags_joined} {last_note}".strip()

    try:
        entries = await search_memory(contact.contact_id, query)
    except Exception as exc:
        logger.error(
            "get_memory backend failure for contact_id=%s: %s",
            contact.contact_id,
            exc,
        )
        return _vapi_response(tool_call_id, {
            "memories": [],
            "status": "degraded",
            "note": "memory backend unavailable",
        })
    # Sort by recency so the AI sees the latest updates first
    entries.sort(key=lambda e: e.timestamp, reverse=True)
    return _vapi_response(tool_call_id, {
        "memories": [{"text": e.text, "type": e.type, "timestamp": e.timestamp.isoformat()} for e in entries],
    })


@router.post("/tools/search_memory", summary="Targeted semantic search in memory for a contact")
async def search_memory_tool(body: dict[str, Any]):
    """
    Requirement 4.5 / 4.6: Performs a targeted semantic search using the query
    string from the request body. Must respond within ~2 seconds.
    """
    tool_call_id = _extract_tool_call_id(body)
    request = _extract_tool_request(body)
    if not request.contact_id:
        logger.warning("search_memory missing contact_id. payload_keys=%s", list(body.keys()))
        raise HTTPException(status_code=400, detail="contact_id is required")
    if not request.query:
        logger.warning("search_memory missing query. payload_keys=%s", list(body.keys()))
        raise HTTPException(status_code=400, detail="query is required")

    # Validate contact exists (returns 404 if not)
    await _get_contact(request.contact_id)

    from app.services.qdrant import search_memory

    try:
        entries = await search_memory(request.contact_id, request.query)
        entries.sort(key=lambda e: e.timestamp, reverse=True)
    except Exception as exc:
        logger.error(
            "search_memory backend failure for contact_id=%s: %s",
            request.contact_id,
            exc,
        )
        return _vapi_response(tool_call_id, {
            "memories": [],
            "status": "degraded",
            "note": "memory backend unavailable",
        })
    return _vapi_response(tool_call_id, {
        "memories": [{"text": e.text, "type": e.type, "timestamp": e.timestamp.isoformat()} for e in entries],
    })


@router.post("/tools/save_memory", summary="Store a memory entry for a contact")
async def save_memory(body: dict[str, Any]):
    """
    Requirement 4.7: Stores the provided text as a memory entry in the Memory_Store.
    """
    tool_call_id = _extract_tool_call_id(body)
    request = _extract_tool_request(body)
    if not request.contact_id:
        logger.warning("save_memory missing contact_id. payload_keys=%s", list(body.keys()))
        raise HTTPException(status_code=400, detail="contact_id is required")
    if not request.text:
        logger.warning("save_memory missing text. payload_keys=%s", list(body.keys()))
        raise HTTPException(status_code=400, detail="text is required")

    # Validate contact exists (returns 404 if not)
    await _get_contact(request.contact_id)

    from app.services.qdrant import store_memory
    from app.models.memory import MemoryEntry

    entry = MemoryEntry(
        contact_id=request.contact_id,
        type="highlight",
        text=request.text,
    )
    try:
        entry_id = await store_memory(entry)
    except Exception as exc:
        logger.error(
            "save_memory backend failure for contact_id=%s: %s",
            request.contact_id,
            exc,
        )
        return _vapi_response(tool_call_id, {
            "status": "degraded",
            "note": "memory backend unavailable",
        })
    return _vapi_response(tool_call_id, {"status": "saved", "entry_id": entry_id})


@router.post("/tools/get_calendar_slots", summary="Return available calendar time slots")
async def get_calendar_slots(body: dict[str, Any]):
    """
    Requirement 4.8: Delegates to Calendar Service to return available slots.
    """
    tool_call_id = _extract_tool_call_id(body)
    request = _extract_tool_request(body)
    if not request.contact_id:
        logger.warning("get_calendar_slots missing contact_id. payload_keys=%s", list(body.keys()))
        raise HTTPException(status_code=400, detail="contact_id is required")

    # Validate contact exists (returns 404 if not)
    await _get_contact(request.contact_id)

    try:
        from app.services.calendar import get_free_slots
        slots = await get_free_slots()
        return _vapi_response(tool_call_id, {
            "slots": [s.model_dump() if hasattr(s, "model_dump") else s for s in slots],
        })
    except ImportError:
        return _vapi_response(tool_call_id, {"slots": [], "note": "Calendar service not yet available"})


@router.post("/tools/create_calendar_event", summary="Create a calendar event for a contact")
async def create_calendar_event(body: dict[str, Any]):
    """
    Requirement 4.8: Delegates to Calendar Service to create a calendar event.
    """
    tool_call_id = _extract_tool_call_id(body)
    request = _extract_tool_request(body)
    if not request.contact_id:
        logger.warning("create_calendar_event missing contact_id. payload_keys=%s", list(body.keys()))
        raise HTTPException(status_code=400, detail="contact_id is required")

    contact = await _get_contact(request.contact_id)

    try:
        from app.services.calendar import create_event
        from datetime import datetime

        start = datetime.fromisoformat(request.start_time) if request.start_time else None
        end = datetime.fromisoformat(request.end_time) if request.end_time else None

        event = await create_event(start=start, end=end, contact=contact)
        return _vapi_response(tool_call_id, {
            "status": "created",
            "event": event.model_dump() if hasattr(event, "model_dump") else event,
        })
    except ImportError:
        return _vapi_response(tool_call_id, {"status": "unavailable", "note": "Calendar service not yet available"})
