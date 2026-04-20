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
    Document,
)
from app.models.memory import MemoryEntry
from app.config import get_settings

COLLECTION_NAME = "memories"


def _get_client() -> AsyncQdrantClient:
    settings = get_settings()
    return AsyncQdrantClient(
        url=settings.QDRANT_ENDPOINT,
        api_key=settings.QDRANT_API_KEY,
        cloud_inference=True,  # no-op when not using Document vectors
    )


def _use_qdrant_inference() -> bool:
    try:
        return get_settings().EMBEDDING_PROVIDER == "qdrant"
    except Exception:
        return True


async def _get_alias_target(client: AsyncQdrantClient, alias: str) -> str | None:
    """Return the real collection name the alias points to, or None."""
    try:
        result = await client.get_aliases()
        for a in result.aliases:
            if a.alias_name == alias:
                return a.collection_name
    except Exception:
        pass
    return None


async def ensure_collection_exists() -> None:
    """
    Called at startup. Creates the 'memories' collection (or alias) if it does not exist.
    Fresh installs: creates 'memories_main' and aliases 'memories' → 'memories_main'.
    Legacy installs with a raw 'memories' collection: left unchanged (no alias created).
    The alias setup enables zero-downtime model migrations via scripts/migrate_embeddings.py.
    """
    settings = get_settings()
    vector_size = settings.EMBEDDING_VECTOR_SIZE
    client = _get_client()

    alias_target = await _get_alias_target(client, COLLECTION_NAME)
    if alias_target is not None:
        # Alias already set up — ensure the payload index exists and return.
        await client.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name="contact_id",
            field_schema=PayloadSchemaType.KEYWORD,
        )
        return

    existing = await client.get_collections()
    names = [c.name for c in existing.collections]

    if COLLECTION_NAME in names:
        # Legacy raw collection — leave it alone; alias can be added later via migrate script.
        await client.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name="contact_id",
            field_schema=PayloadSchemaType.KEYWORD,
        )
        return

    # Fresh install: create timestamped collection + alias.
    from datetime import datetime, timezone
    real_name = f"{COLLECTION_NAME}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    if real_name not in names:
        await client.create_collection(
            collection_name=real_name,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )
    await client.create_payload_index(
        collection_name=real_name,
        field_name="contact_id",
        field_schema=PayloadSchemaType.KEYWORD,
    )
    await client.update_collection_aliases(change_aliases_operations=[
        {"create_alias": {"collection_name": real_name, "alias_name": COLLECTION_NAME}}
    ])


async def store_memory(entry: MemoryEntry) -> str:
    """Embeds entry.text and upserts into Qdrant. Returns point ID."""
    settings = get_settings()
    client = _get_client()
    payload = {
        "contact_id": entry.contact_id,
        "type": entry.type,
        "text": entry.text,
        "timestamp": entry.timestamp.isoformat(),
        "embedding_model": settings.EMBEDDING_MODEL,
    }

    if _use_qdrant_inference():
        vector = Document(text=entry.text, model=settings.EMBEDDING_MODEL)
    else:
        from app.services.embedding import embed
        vector = await embed(entry.text)

    await client.upsert(
        collection_name=COLLECTION_NAME,
        points=[PointStruct(id=entry.entry_id, vector=vector, payload=payload)],
    )
    return entry.entry_id


async def search_memory(contact_id: str, query: str, top_k: int = 5) -> list[MemoryEntry]:
    """
    Searches Qdrant for top_k memories for contact_id matching query.
    Uses Qdrant cloud inference or external embedding based on EMBEDDING_PROVIDER.
    """
    from datetime import datetime
    settings = get_settings()
    client = _get_client()

    if _use_qdrant_inference():
        query_vector = Document(text=query, model=settings.EMBEDDING_MODEL)
    else:
        from app.services.embedding import embed
        query_vector = await embed(query)

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
            query=query_vector,
            query_filter=contact_filter,
            limit=top_k,
        )
        points = getattr(response, "points", None)
        if isinstance(points, list):
            results = points

    if results is None and hasattr(client, "search"):
        results = await client.search(  # type: ignore[attr-defined]
            collection_name=COLLECTION_NAME,
            query_vector=query_vector,
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
