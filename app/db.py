import json
import os
import aiosqlite
from typing import Optional, Any
from app.models.contact import Contact, TimeWindow, SocialHandles

DATABASE_URL = os.environ.get("DATABASE_URL", "contacts.db")


async def get_db() -> aiosqlite.Connection:
    """Return an aiosqlite connection. Caller is responsible for closing."""
    db = await aiosqlite.connect(DATABASE_URL)
    db.row_factory = aiosqlite.Row
    return db


async def init_db() -> None:
    """Create all tables if they don't already exist."""
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                contact_id   TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                phone        TEXT NOT NULL,
                sip          TEXT,
                contact_method TEXT NOT NULL DEFAULT 'phone',
                tags         TEXT,
                timezone     TEXT NOT NULL,
                last_called  TEXT,
                last_spoken  TEXT,
                call_time_preference TEXT DEFAULT 'none',
                preferred_time_window TEXT,
                next_call_at TEXT,
                priority_boost REAL DEFAULT 0.0,
                last_call_outcome TEXT,
                last_call_note TEXT,
                call_started_at TEXT,
                social_handles TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS processed_calls (
                call_id TEXT PRIMARY KEY,
                processed_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS gift_orders (
                order_id      TEXT PRIMARY KEY,
                contact_id    TEXT NOT NULL,
                occasion      TEXT NOT NULL,
                gift_type     TEXT NOT NULL,
                vendor        TEXT,
                description   TEXT,
                tracking      TEXT,
                delivery_date TEXT,
                ordered_at    TEXT NOT NULL
            )
        """)
        # Add new columns to existing contacts table (safe to run on old DBs)
        for col, typedef in [
            ("birthday", "TEXT"),
            ("anniversary", "TEXT"),
            ("relationship_type", "TEXT DEFAULT 'personal'"),
        ]:
            try:
                await db.execute(f"ALTER TABLE contacts ADD COLUMN {col} {typedef}")
            except Exception:
                pass  # column already exists
        # Add delivered column to gift_orders (safe to run on old DBs)
        try:
            await db.execute("ALTER TABLE gift_orders ADD COLUMN delivered INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # column already exists
        await db.commit()


# --- JSON serialization helpers ---

def serialize_tags(tags: list[str]) -> Optional[str]:
    """Serialize tags list to JSON string for storage."""
    return json.dumps(tags) if tags is not None else None


def deserialize_tags(value: Optional[str]) -> list[str]:
    """Deserialize JSON string to tags list."""
    if value is None:
        return []
    return json.loads(value)


def serialize_time_window(window: Optional[TimeWindow]) -> Optional[str]:
    """Serialize TimeWindow to JSON string for storage."""
    if window is None:
        return None
    return window.model_dump_json()


def deserialize_time_window(value: Optional[str]) -> Optional[TimeWindow]:
    """Deserialize JSON string to TimeWindow."""
    if value is None:
        return None
    return TimeWindow.model_validate_json(value)


def serialize_social_handles(handles: SocialHandles) -> str:
    """Serialize SocialHandles to JSON string for storage."""
    return handles.model_dump_json()


def deserialize_social_handles(value: Optional[str]) -> SocialHandles:
    """Deserialize JSON string to SocialHandles."""
    if value is None:
        return SocialHandles()
    return SocialHandles.model_validate_json(value)


def contact_to_row(contact: Contact) -> dict[str, Any]:
    """Convert a Contact model to a flat dict suitable for DB insertion."""
    return {
        "contact_id": contact.contact_id,
        "name": contact.name,
        "phone": contact.phone,
        "sip": contact.sip,
        "contact_method": contact.contact_method,
        "tags": serialize_tags(contact.tags),
        "timezone": contact.timezone,
        "last_called": contact.last_called.isoformat() if contact.last_called else None,
        "last_spoken": contact.last_spoken.isoformat() if contact.last_spoken else None,
        "call_time_preference": contact.call_time_preference,
        "preferred_time_window": serialize_time_window(contact.preferred_time_window),
        "next_call_at": contact.next_call_at.isoformat() if contact.next_call_at else None,
        "priority_boost": contact.priority_boost,
        "last_call_outcome": contact.last_call_outcome,
        "last_call_note": contact.last_call_note,
        "call_started_at": contact.call_started_at.isoformat() if contact.call_started_at else None,
        "social_handles": serialize_social_handles(contact.social_handles),
        "birthday": contact.birthday,
        "anniversary": contact.anniversary,
        "relationship_type": contact.relationship_type,
    }


def row_to_contact(row: aiosqlite.Row) -> Contact:
    """Convert a DB row to a Contact model."""
    data = dict(row)
    data["tags"] = deserialize_tags(data.get("tags"))
    data["preferred_time_window"] = deserialize_time_window(data.get("preferred_time_window"))
    data["social_handles"] = deserialize_social_handles(data.get("social_handles"))
    return Contact.model_validate(data)
