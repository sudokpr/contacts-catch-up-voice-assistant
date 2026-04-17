from fastapi import APIRouter, HTTPException
from app.models.contact import Contact
from app.db import get_db, init_db, contact_to_row, row_to_contact
from app.services.qdrant import delete_contact_memories

router = APIRouter()


@router.post("", status_code=201, response_model=Contact)
async def create_contact(contact: Contact):
    """Create a new contact. Phone must be E.164 format."""
    db = await get_db()
    try:
        row = contact_to_row(contact)
        await db.execute(
            """
            INSERT INTO contacts (
                contact_id, name, phone, sip, contact_method, tags, timezone,
                last_called, last_spoken, call_time_preference, preferred_time_window,
                next_call_at, priority_boost, last_call_outcome, last_call_note,
                call_started_at, social_handles, birthday, anniversary, relationship_type
            ) VALUES (
                :contact_id, :name, :phone, :sip, :contact_method, :tags, :timezone,
                :last_called, :last_spoken, :call_time_preference, :preferred_time_window,
                :next_call_at, :priority_boost, :last_call_outcome, :last_call_note,
                :call_started_at, :social_handles, :birthday, :anniversary, :relationship_type
            )
            """,
            row,
        )
        await db.commit()
    finally:
        await db.close()
    return contact


@router.get("", response_model=list[Contact])
async def list_contacts():
    """Return all contacts."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM contacts")
        rows = await cursor.fetchall()
        return [row_to_contact(r) for r in rows]
    finally:
        await db.close()


@router.get("/{contact_id}", response_model=Contact)
async def get_contact(contact_id: str):
    """Return a single contact by ID."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM contacts WHERE contact_id = ?", (contact_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Contact not found")
        return row_to_contact(row)
    finally:
        await db.close()


@router.put("/{contact_id}", response_model=Contact)
async def update_contact(contact_id: str, contact: Contact):
    """Update an existing contact. Phone must be E.164 format."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT contact_id FROM contacts WHERE contact_id = ?", (contact_id,)
        )
        existing = await cursor.fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Contact not found")

        # Ensure the contact_id in the body matches the path param
        updated = contact.model_copy(update={"contact_id": contact_id})
        row = contact_to_row(updated)
        await db.execute(
            """
            UPDATE contacts SET
                name = :name, phone = :phone, sip = :sip,
                contact_method = :contact_method, tags = :tags, timezone = :timezone,
                last_called = :last_called, last_spoken = :last_spoken,
                call_time_preference = :call_time_preference,
                preferred_time_window = :preferred_time_window,
                next_call_at = :next_call_at, priority_boost = :priority_boost,
                last_call_outcome = :last_call_outcome, last_call_note = :last_call_note,
                call_started_at = :call_started_at, social_handles = :social_handles,
                birthday = :birthday, anniversary = :anniversary,
                relationship_type = :relationship_type
            WHERE contact_id = :contact_id
            """,
            row,
        )
        await db.commit()
        return updated
    finally:
        await db.close()


@router.get("/{contact_id}/memories")
async def get_contact_memories(contact_id: str, limit: int = 30):
    """Return recent memories for a contact."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT contact_id FROM contacts WHERE contact_id = ?", (contact_id,)
        )
        if await cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Contact not found")
    finally:
        await db.close()

    try:
        from app.services.qdrant import search_memory
        entries = await search_memory(contact_id, contact_id, top_k=limit)
        return [{"text": e.text, "type": e.type, "timestamp": e.timestamp.isoformat()} for e in entries]
    except Exception as exc:
        return []


@router.delete("/{contact_id}", status_code=204)
async def delete_contact(contact_id: str):
    """Delete a contact and all associated memory entries."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT contact_id FROM contacts WHERE contact_id = ?", (contact_id,)
        )
        existing = await cursor.fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Contact not found")

        # Remove memories first, then the DB record
        await delete_contact_memories(contact_id)

        await db.execute(
            "DELETE FROM contacts WHERE contact_id = ?", (contact_id,)
        )
        await db.commit()
    finally:
        await db.close()
