"""
Mock gifting service — simulates ordering flowers, custom mugs, and sweet boxes.

Returns realistic order confirmations with fake tracking numbers.
Swap individual functions for real API calls (Printful, 1-800-Flowers, Zomato Gifts, etc.)
when ready to go live.
"""

import logging
import random
import uuid
from datetime import datetime, UTC, timedelta

logger = logging.getLogger(__name__)

_FLOWER_VENDORS = ["BloomBox", "PetalPost", "FloralExpress", "Garden & Co."]
_MUG_VENDORS = ["MugMagic", "PrintJoy", "CustomCraft", "MemoryPress"]
_SWEET_VENDORS = ["MithaiBazaar", "SweetCraft", "Royal Sweets", "Bombay Mithai"]

_BOUQUET_DESCRIPTIONS = [
    "Mixed seasonal bouquet with roses and lilies",
    "Premium sunflower and tulip arrangement",
    "Elegant white and pink mixed bouquet",
    "Cheerful wildflower arrangement",
]
_MUG_DESCRIPTIONS = [
    "11oz ceramic mug with personalised name print",
    "White ceramic mug with warm birthday message",
    "Photo-quality mug with celebration design",
]
_SWEET_DESCRIPTIONS = [
    "Premium assorted mithai box (1kg)",
    "Dry fruit and nut gift hamper",
    "Traditional festival sweet box with kaju katli and ladoo",
    "Handcrafted chocolate and mithai assortment",
]

FESTIVAL_OCCASIONS = {"diwali", "christmas", "eid", "new_year", "holi", "onam", "baisakhi"}


async def order_gift(contact_id: str, contact_name: str, occasion: str, gift_types: list[str]) -> dict:
    """
    Place a mock gift order and persist to gift_orders table.

    Args:
        contact_id:   contact being gifted
        contact_name: used in mug personalisation text
        occasion:     'birthday' | 'anniversary'
        gift_types:   list containing 'flowers' and/or 'mug'

    Returns:
        {
            "orders": [{"type", "vendor", "description", "tracking", "estimated_delivery"}],
            "summary": "human-readable one-liner for the AI to read out"
        }
    """
    from app.db import get_db

    orders = []
    now = datetime.now(UTC)

    for gift_type in gift_types:
        tracking = f"TRK{uuid.uuid4().hex[:8].upper()}"
        delivery_date = (now + timedelta(days=random.randint(1, 3))).strftime("%B %d")

        if gift_type == "flowers":
            order = {
                "type": "flowers",
                "vendor": random.choice(_FLOWER_VENDORS),
                "description": random.choice(_BOUQUET_DESCRIPTIONS),
                "tracking": tracking,
                "estimated_delivery": delivery_date,
            }
        elif gift_type == "mug":
            order = {
                "type": "mug",
                "vendor": random.choice(_MUG_VENDORS),
                "description": f"{random.choice(_MUG_DESCRIPTIONS)} for {contact_name}",
                "tracking": tracking,
                "estimated_delivery": delivery_date,
            }
        else:  # sweet_box
            order = {
                "type": "sweet_box",
                "vendor": random.choice(_SWEET_VENDORS),
                "description": random.choice(_SWEET_DESCRIPTIONS),
                "tracking": tracking,
                "estimated_delivery": delivery_date,
            }
        orders.append(order)

        # Persist to DB
        try:
            db = await get_db()
            try:
                await db.execute(
                    """INSERT INTO gift_orders
                       (order_id, contact_id, occasion, gift_type, vendor, description,
                        tracking, delivery_date, ordered_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()), contact_id, occasion, gift_type,
                        order["vendor"], order["description"],
                        tracking, delivery_date, now.isoformat(),
                    ),
                )
                await db.commit()
            finally:
                await db.close()
        except Exception as exc:
            logger.error("Failed to persist gift order for contact %s: %s", contact_id, exc)

    # Build a readable summary for the AI to mention during the call
    parts = []
    for o in orders:
        parts.append(f"a {o['description']} arriving by {o['estimated_delivery']}")
    summary = " and ".join(parts)

    logger.info(
        "Gift order placed for contact %s (%s): %s",
        contact_id, occasion, [o["tracking"] for o in orders]
    )
    return {"orders": orders, "summary": summary}


def choose_gifts_for_occasion(occasion: str) -> list[str]:
    """Return the default gift types for an occasion."""
    if occasion == "birthday":
        return ["flowers", "mug"]
    if occasion in FESTIVAL_OCCASIONS:
        return ["sweet_box"]
    return ["flowers"]  # anniversary, deal_congratulations, promotion_congratulations, crm_deal


async def get_gift_delivery_context(contact_id: str) -> str:
    """
    Check the most recent gift order for this contact.
    Returns a natural-language status string for the AI to use during the call,
    or "" if no recent gift was ordered.
    """
    from app.db import get_db

    try:
        db = await get_db()
        try:
            async with db.execute(
                "SELECT description, delivery_date FROM gift_orders WHERE contact_id = ? ORDER BY ordered_at DESC LIMIT 1",
                (contact_id,),
            ) as cursor:
                row = await cursor.fetchone()
        finally:
            await db.close()

        if not row:
            return ""

        description, delivery_date_str = row["description"], row["delivery_date"]
        if not delivery_date_str:
            return ""

        # delivery_date is stored as "Month DD" (e.g. "April 18")
        try:
            today = datetime.now(UTC)
            delivery_dt = datetime.strptime(f"{delivery_date_str} {today.year}", "%B %d %Y")
            if delivery_dt.date() >= today.date():
                return f"A gift is on its way — {description} — arriving by {delivery_date_str}!"
            else:
                return f"We recently sent you {description} — hope you enjoyed it!"
        except ValueError:
            return ""

    except Exception as exc:
        logger.error("get_gift_delivery_context error for contact %s: %s", contact_id, exc)
        return ""
