from qdrant_client import AsyncQdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    PayloadSchemaType,
    PointIdsList,
)
from app.models.memory import MemoryEntry
from app.config import get_settings

COLLECTION_NAME = "memories"
VECTOR_SIZE = 768


def _get_client() -> AsyncQdrantClient:
    settings = get_settings()
    return AsyncQdrantClient(
        url=settings.QDRANT_ENDPOINT,
        api_key=settings.QDRANT_API_KEY,
    )


async def ensure_collection_exists() -> None:
    """
    Called at startup. Creates the 'memories' collection with vector_size=768
    and distance=Cosine if it does not already exist. Safe to call repeatedly.
    """
    client = _get_client()
    existing = await client.get_collections()
    names = [c.name for c in existing.collections]
    if COLLECTION_NAME not in names:
        await client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
    # Required by some Qdrant deployments for filtered delete operations.
    # Safe to call repeatedly.
    await client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="contact_id",
        field_schema=PayloadSchemaType.KEYWORD,
    )


async def store_memory(entry: MemoryEntry) -> str:
    """Embeds entry.text and upserts into Qdrant. Returns point ID."""
    from app.services.embedding import embed

    vector = await embed(entry.text)
    client = _get_client()
    payload = {
        "contact_id": entry.contact_id,
        "type": entry.type,
        "text": entry.text,
        "timestamp": entry.timestamp.isoformat(),
    }
    await client.upsert(
        collection_name=COLLECTION_NAME,
        points=[
            PointStruct(
                id=entry.entry_id,
                vector=vector,
                payload=payload,
            )
        ],
    )
    return entry.entry_id


async def search_memory(contact_id: str, query: str, top_k: int = 5) -> list[MemoryEntry]:
    """
    Embeds query, performs cosine similarity search scoped to contact_id.
    Returns top_k results.
    """
    from app.services.embedding import embed
    from datetime import datetime

    vector = await embed(query)
    client = _get_client()
    contact_filter = Filter(
        must=[
            FieldCondition(
                key="contact_id",
                match=MatchValue(value=contact_id),
            )
        ]
    )
    results = None
    query_points_fn = getattr(client, "query_points", None)
    if callable(query_points_fn):
        response = await query_points_fn(
            collection_name=COLLECTION_NAME,
            query=vector,
            query_filter=contact_filter,
            limit=top_k,
        )
        points = getattr(response, "points", None)
        if isinstance(points, list):
            results = points

    if results is None and hasattr(client, "search"):
        # Backward compatibility with older qdrant-client versions.
        results = await client.search(  # type: ignore[attr-defined]
            collection_name=COLLECTION_NAME,
            query_vector=vector,
            query_filter=contact_filter,
            limit=top_k,
        )

    if results is None:
        results = []
    entries = []
    for hit in results:
        p = hit.payload
        entries.append(
            MemoryEntry(
                entry_id=str(hit.id),
                contact_id=p["contact_id"],
                type=p["type"],
                text=p["text"],
                timestamp=datetime.fromisoformat(p["timestamp"]),
            )
        )
    return entries


async def delete_contact_memories(contact_id: str) -> None:
    """Deletes all memory entries for a given contact_id."""
    client = _get_client()
    contact_filter = Filter(
        must=[
            FieldCondition(
                key="contact_id",
                match=MatchValue(value=contact_id),
            )
        ]
    )
    try:
        await client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=contact_filter,
        )
        return
    except UnexpectedResponse as exc:
        # Fallback for clusters that require an index for filtered deletes.
        if getattr(exc, "status_code", None) != 400:
            raise

    ids: list[str] = []
    offset = None
    while True:
        records, offset = await client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=contact_filter,
            limit=100,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        ids.extend([str(record.id) for record in records])
        if offset is None:
            break

    if ids:
        await client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=PointIdsList(points=ids),
        )


async def delete_memory(entry_id: str) -> None:
    """Delete a single memory entry by its Qdrant point ID."""
    from uuid import UUID
    client = _get_client()
    await client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=PointIdsList(points=[UUID(entry_id)]),
    )
