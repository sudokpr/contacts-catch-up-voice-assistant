#!/usr/bin/env python3
"""
seed_contacts.py — Populate the database with sample contacts, past call history,
and semantic memories in Qdrant for demo / hackathon purposes.

Usage:
    python scripts/seed_contacts.py

Reads QDRANT_API_KEY, QDRANT_ENDPOINT from .env (or environment).
Safe to run multiple times — skips contacts that already exist by name.
"""

import asyncio
import os
import sys
from datetime import datetime, UTC, timedelta
from pathlib import Path
from uuid import uuid4


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

# Add project root to path so app imports work
sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

CONTACTS = [
    # ── Personal contacts ──────────────────────────────────────────────────────
    {
        "name": "Maya Patel",
        "phone": "+12125550101",
        "contact_method": "phone",
        "tags": ["college", "close-friend", "tech"],
        "timezone": "America/New_York",
        "last_call_outcome": "answered",
        "last_call_note": "Maya just got promoted to Staff Engineer at her company. She's been leading a big migration to Kubernetes and is exhausted but proud. Her cat Luna had kittens recently.",
        "days_since_last_call": 18,
        "call_time_preference": "evening",
        "priority_boost": 1.0,
        "social_handles": {"twitter": "mayapatel_dev", "linkedin": "maya-patel-eng"},
        "memories": [
            ("highlight", "I finally got that promotion! Staff Engineer as of last month."),
            ("highlight", "Luna had four kittens, I've been drowning in cuteness honestly."),
            ("highlight", "The Kubernetes migration is killing me but we're almost done."),
            ("fact", "Maya was promoted to Staff Engineer in March 2026."),
            ("fact", "Maya has a cat named Luna who recently had kittens."),
            ("summary", "Caught up after 3 weeks. Maya is celebrating a promotion to Staff Engineer. Stressed about a Kubernetes migration at work but optimistic. Her cat Luna had kittens. She mentioned wanting to visit India to see her parents this summer."),
            ("social", "Maya tweeted: 'just merged 200 PRs worth of k8s migration. i need a vacation and possibly a therapist 🙃'"),
            ("social", "Maya posted on LinkedIn about her promotion to Staff Engineer at CloudBase."),
        ],
    },
    {
        "name": "David Okafor",
        "phone": "+14155550202",
        "contact_method": "phone",
        "tags": ["ex-colleague", "startup", "mentor"],
        "timezone": "America/Los_Angeles",
        "last_call_outcome": "answered",
        "last_call_note": "David left his job at Stripe and is now building his own startup in the climate-tech space. Very excited, a little terrified. He mentioned a demo day in May.",
        "days_since_last_call": 35,
        "call_time_preference": "morning",
        "priority_boost": 0.5,
        "social_handles": {"twitter": "davidokafor", "linkedin": "david-okafor"},
        "memories": [
            ("highlight", "I quit Stripe last month. Building something in climate tech now."),
            ("highlight", "We have a demo day in May, if we survive that long ha."),
            ("highlight", "It's scary but honestly I've never felt more alive at work."),
            ("fact", "David left Stripe in early 2026 to start a climate-tech company."),
            ("fact", "David has a demo day scheduled in May 2026."),
            ("summary", "Long overdue catch-up. David made a big leap — left Stripe after 4 years to start his own climate-tech company. Nervous but energised. Has a demo day in May he's laser-focused on. Wants to grab coffee next time he's in the Bay Area."),
            ("social", "David tweeted: 'Day 47 of the startup. Survived three investor rejections and one near-death pivot. Still standing.'"),
        ],
    },
    {
        "name": "Sarah Chen",
        "phone": "+12025550303",
        "contact_method": "phone",
        "tags": ["family-friend", "book-club", "london"],
        "timezone": "Europe/London",
        "last_call_outcome": "no_answer",
        "last_call_note": None,
        "days_since_last_call": 52,
        "call_time_preference": "morning",
        "priority_boost": 2.0,
        "social_handles": {"instagram": "sarahchen.reads", "linkedin": "sarah-chen-london"},
        "memories": [
            ("highlight", "We moved to London! James got a transfer so we just went for it."),
            ("highlight", "The kids are adjusting, it's been a bit rough for Lily but she's making friends."),
            ("highlight", "I've been doing a lot of writing, thinking about finally finishing the novel."),
            ("fact", "Sarah moved to London with her husband James and daughter Lily in late 2025."),
            ("fact", "Sarah is working on a novel and has been writing more seriously since the move."),
            ("summary", "First call since Sarah's big move to London. Her husband James got a job transfer and they took the leap. Kids adjusting — daughter Lily had a tough first month but settling in. Sarah is finally writing seriously and hopes to have a draft by the end of the year. Misses home but loves the adventure."),
            ("social", "Sarah posted on Instagram: 'Six months in London. Still pinching myself. 📚☕️ #expatlife'"),
        ],
    },
    {
        "name": "Raj Sundaram",
        # SIP contact — REPLACE sip URI before demoing or the call will fail immediately.
        # Use your Linphone address: sip:yourusername@sip.linphone.org
        # Or your Vapi SIP number: sip:contacts-catchup@sip.vapi.ai
        "phone": "+6500000001",       # placeholder — not dialled for SIP contacts
        "sip": "sip:REPLACE_ME@sip.linphone.org",
        "contact_method": "sip",
        "tags": ["university", "singapore", "chess"],
        "timezone": "Asia/Singapore",
        "last_call_outcome": "busy",
        "last_call_note": None,
        "days_since_last_call": 70,
        "call_time_preference": "evening",
        "priority_boost": 0.0,
        "social_handles": {"twitter": "rajsundaram_sg"},
        "memories": [
            ("highlight", "Just got back from a chess tournament in Vienna, came third!"),
            ("highlight", "My daughter just started walking, it's the best thing I've ever seen."),
            ("fact", "Raj competed in a chess tournament in Vienna in early 2026, finishing third."),
            ("fact", "Raj's daughter recently started walking."),
            ("summary", "Short call before Raj had to drop off. He's been travelling for chess tournaments — just came third in Vienna. Big news: his daughter started walking. Promised to catch up properly soon."),
        ],
    },
    {
        "name": "Priya Menon",
        # SIP contact — REPLACE sip URI before demoing or the call will fail immediately.
        # Use your Linphone address: sip:yourusername@sip.linphone.org
        "phone": "+910000000001",     # placeholder — not dialled for SIP contacts
        "sip": "sip:REPLACE_ME@sip.linphone.org",
        "contact_method": "sip",
        "tags": ["school-friend", "doctor", "chennai"],
        "timezone": "Asia/Kolkata",
        "last_call_outcome": "answered",
        "last_call_note": "Priya is considering a fellowship in the US. Conflicted about leaving Chennai and her parents. We talked for almost an hour.",
        "days_since_last_call": 8,
        "call_time_preference": "evening",
        "priority_boost": 0.0,
        "social_handles": {"linkedin": "dr-priya-menon"},
        "memories": [
            ("highlight", "I got shortlisted for a fellowship at Johns Hopkins. I don't know if I should go."),
            ("highlight", "My mother is getting older and I feel so guilty even thinking about leaving."),
            ("highlight", "But it's Johns Hopkins, you know? This doesn't come twice."),
            ("fact", "Priya was shortlisted for a fellowship at Johns Hopkins in 2026."),
            ("fact", "Priya is conflicted about leaving Chennai and her aging mother."),
            ("summary", "Long emotional conversation. Priya is at a crossroads — shortlisted for a prestigious fellowship at Johns Hopkins but deeply worried about leaving her mother in Chennai. Talked through the decision at length. She's leaning towards applying and figuring out the family side, but hasn't decided yet. Very close call to make."),
            ("social", "Priya posted on LinkedIn about a medical conference she spoke at in Bangalore."),
        ],
    },

    # ── Business partners ──────────────────────────────────────────────────────
    {
        "name": "Arjun Mehta",
        "phone": "+912250001001",
        "contact_method": "phone",
        "relationship_type": "business",
        "tags": ["founder", "saas", "mumbai", "send-gift"],
        "timezone": "Asia/Kolkata",
        "last_call_outcome": "answered",
        "last_call_note": "Arjun's SaaS platform just hit 500 paying customers. He's looking to expand into Southeast Asia next quarter. Very excited about the traction.",
        "days_since_last_call": 25,
        "call_time_preference": "morning",
        "priority_boost": 1.5,
        "birthday": "2000-04-17",   # today — triggers birthday call demo
        "social_handles": {"linkedin": "arjunmehta-founder"},
        "memories": [
            ("highlight", "We hit 500 paying customers last month — still can't believe it."),
            ("highlight", "Planning to expand into Southeast Asia, starting with Singapore."),
            ("fact", "Arjun Mehta is the founder of a SaaS startup based in Mumbai, India."),
            ("fact", "His startup crossed 500 customers in early 2026 and secured ₹5 crore in seed funding."),
            ("summary", "Strong quarterly check-in. Arjun's SaaS platform is growing fast — 500 customers, seed funding secured, and a Southeast Asia expansion in the works. He mentioned catching up next quarter to discuss potential partnership opportunities."),
        ],
    },
    {
        "name": "Priya Sharma",
        "phone": "+911140002002",
        "contact_method": "phone",
        "relationship_type": "business",
        "tags": ["business", "logistics", "delhi", "send-gift"],
        "timezone": "Asia/Kolkata",
        "last_call_outcome": "answered",
        "last_call_note": "Priya's team just closed a major ₹10 crore logistics contract. She's scaling the operations team rapidly and is looking for tech partners.",
        "days_since_last_call": 40,
        "call_time_preference": "morning",
        "priority_boost": 1.0,
        "social_handles": {"linkedin": "priyas-logistics"},
        "memories": [
            ("highlight", "Closed the biggest contract of my career last week — ₹10 crore with a national retailer."),
            ("highlight", "Hiring 50 people over the next quarter. The scale-up is real now."),
            ("fact", "Priya Sharma is VP Sales at a logistics firm based in Delhi."),
            ("fact", "Her team closed a ₹10 crore contract in early 2026 and is scaling operations."),
            ("summary", "Excellent catch-up. Priya's team closed a landmark deal and is in rapid scale-up mode. She's looking for reliable tech partners for fleet management. Left the door open for a follow-up call next month."),
        ],
    },
    {
        "name": "Marcus Weber",
        "phone": "+4930500003003",
        "contact_method": "phone",
        "relationship_type": "business",
        "tags": ["vc", "berlin", "investor", "fintech"],
        "timezone": "Europe/Berlin",
        "last_call_outcome": "answered",
        "last_call_note": "Marcus just made Managing Partner at Weber Capital. He's leading a new fintech fund and was open to meeting portfolio companies from South Asia.",
        "days_since_last_call": 55,
        "call_time_preference": "morning",
        "priority_boost": 0.5,
        "social_handles": {"linkedin": "marcus-weber-vc"},
        "memories": [
            ("highlight", "Just got promoted to Managing Partner — it's been a long journey."),
            ("highlight", "We're closing a new €50M fintech fund next quarter."),
            ("fact", "Marcus Weber was promoted to Managing Partner at Weber Capital in early 2026."),
            ("fact", "He is leading a new €50M fintech-focused fund based out of Berlin."),
            ("summary", "Great reconnection call. Marcus recently got promoted to Managing Partner and is excited about a new fintech fund. Very interested in South Asian market opportunities. Suggested reconnecting when he's next in Asia."),
        ],
    },
]


# ---------------------------------------------------------------------------
# Seed logic
# ---------------------------------------------------------------------------

async def seed() -> None:
    from app.db import init_db, get_db, contact_to_row
    from app.models.contact import Contact, SocialHandles
    from app.models.memory import MemoryEntry
    from app.services.qdrant import ensure_collection_exists, store_memory

    print("Initialising database...")
    await init_db()

    print("Ensuring Qdrant collection exists...")
    try:
        await ensure_collection_exists()
    except Exception as e:
        print(f"  WARNING: Qdrant setup failed: {e}")
        print("  Contacts will be seeded to SQLite but memories will be skipped.")

    # Load existing contact names to avoid duplicates
    db = await get_db()
    try:
        cursor = await db.execute("SELECT name FROM contacts")
        existing_names = {row[0] for row in await cursor.fetchall()}
    finally:
        await db.close()

    now = datetime.now(UTC)

    for data in CONTACTS:
        name = data["name"]
        if name in existing_names:
            print(f"  Skipping {name} (already exists)")
            continue

        contact_id = str(uuid4())
        days_ago = data["days_since_last_call"]
        last_called = now - timedelta(days=days_ago)
        last_spoken = last_called if data["last_call_outcome"] == "answered" else None

        sh = data.get("social_handles", {})
        contact = Contact(
            contact_id=contact_id,
            name=name,
            phone=data["phone"],
            sip=data.get("sip"),
            contact_method=data["contact_method"],
            tags=data["tags"],
            timezone=data["timezone"],
            last_called=last_called,
            last_spoken=last_spoken,
            last_call_outcome=data["last_call_outcome"],
            last_call_note=data.get("last_call_note"),
            call_time_preference=data["call_time_preference"],
            priority_boost=data["priority_boost"],
            social_handles=SocialHandles(
                twitter=sh.get("twitter"),
                instagram=sh.get("instagram"),
                linkedin=sh.get("linkedin"),
            ),
            birthday=data.get("birthday"),
            anniversary=data.get("anniversary"),
            relationship_type=data.get("relationship_type", "personal"),
        )

        db = await get_db()
        try:
            await db.execute(
                """INSERT INTO contacts (
                    contact_id, name, phone, sip, contact_method, tags, timezone,
                    last_called, last_spoken, call_time_preference, preferred_time_window,
                    next_call_at, priority_boost, last_call_outcome, last_call_note,
                    call_started_at, social_handles, birthday, anniversary, relationship_type
                ) VALUES (
                    :contact_id, :name, :phone, :sip, :contact_method, :tags, :timezone,
                    :last_called, :last_spoken, :call_time_preference, :preferred_time_window,
                    :next_call_at, :priority_boost, :last_call_outcome, :last_call_note,
                    :call_started_at, :social_handles, :birthday, :anniversary, :relationship_type
                )""",
                contact_to_row(contact),
            )
            await db.commit()
        finally:
            await db.close()

        print(f"  Created contact: {name} (last called {days_ago}d ago, outcome: {data['last_call_outcome']})")

        # Seed memories into Qdrant
        memories = data.get("memories", [])
        if memories:
            stored = 0
            for mem_type, mem_text in memories:
                # Spread timestamps over the past few weeks for realism
                age_days = days_ago + (len(memories) - stored) * 2
                mem_time = now - timedelta(days=age_days, hours=stored)
                entry = MemoryEntry(
                    contact_id=contact_id,
                    type=mem_type,
                    text=mem_text,
                    timestamp=mem_time,
                )
                try:
                    await store_memory(entry)
                    stored += 1
                except Exception as e:
                    print(f"    WARNING: Could not store memory for {name}: {e}")
                    break
            if stored:
                print(f"    Stored {stored}/{len(memories)} memories in Qdrant")

    print("\nDone. Run the server and open http://localhost:8000 to see your contacts.")


if __name__ == "__main__":
    asyncio.run(seed())
