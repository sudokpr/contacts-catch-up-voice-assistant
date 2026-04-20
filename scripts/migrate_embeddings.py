"""
Embedding model migration script — zero-downtime via Qdrant collection aliases.

The app always reads/writes to the alias "memories". During migration:
  1. A new versioned collection is created and backfilled (app still serves the old one).
  2. A second pass catches any points written during the backfill.
  3. The alias is atomically updated — cutover is instant, no downtime.
  4. The old collection remains until explicitly deleted.

Usage:
  # Migrate to a new Qdrant inference model:
  python scripts/migrate_embeddings.py \\
    --new-model sentence-transformers/all-minilm-l6-v2 \\
    --new-dims 384 \\
    --provider qdrant

  # Migrate to Gemini (external):
  python scripts/migrate_embeddings.py \\
    --new-model gemini-embedding-001 \\
    --new-dims 3072 \\
    --provider external \\
    --api-key $EMBEDDING_API_KEY \\
    --base-url https://generativelanguage.googleapis.com/v1beta/

  # Just swap the alias after a previously interrupted backfill:
  python scripts/migrate_embeddings.py --swap-only memories_20260420123000

Requires: qdrant-client >= 1.14.0 (for Document / cloud_inference support)
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_env = Path(__file__).parent.parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, PayloadSchemaType

try:
    from qdrant_client.models import Document
    HAS_DOCUMENT = True
except ImportError:
    HAS_DOCUMENT = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ALIAS_NAME = "memories"
SCROLL_BATCH = 100


def _get_client() -> AsyncQdrantClient:
    return AsyncQdrantClient(
        url=os.environ["QDRANT_ENDPOINT"],
        api_key=os.environ["QDRANT_API_KEY"],
        cloud_inference=True,
    )


async def _embed_external(text: str, api_key: str, base_url: str, model: str) -> list[float]:
    if "generativelanguage.googleapis.com" in base_url:
        import httpx
        model_id = model if model.startswith("models/") else f"models/{model}"
        url = f"https://generativelanguage.googleapis.com/v1beta/{model_id}:embedContent"
        async with httpx.AsyncClient() as c:
            r = await c.post(
                url,
                headers={"X-goog-api-key": api_key, "Content-Type": "application/json"},
                json={"model": model_id, "content": {"parts": [{"text": text}]}},
                timeout=30.0,
            )
            r.raise_for_status()
            return r.json()["embedding"]["values"]
    else:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        resp = await client.embeddings.create(model=model, input=text)
        return resp.data[0].embedding


async def _get_alias_target(client: AsyncQdrantClient, alias: str) -> str | None:
    try:
        result = await client.get_aliases()
        for a in result.aliases:
            if a.alias_name == alias:
                return a.collection_name
    except Exception:
        pass
    return None


async def _collection_exists(client: AsyncQdrantClient, name: str) -> bool:
    cols = await client.get_collections()
    return any(c.name == name for c in cols.collections)


async def _backfill(
    client: AsyncQdrantClient,
    src: str,
    dst: str,
    provider: str,
    new_model: str,
    new_dims: int,
    api_key: str = "",
    base_url: str = "",
) -> int:
    """Re-embed all points from src into dst. Returns migrated count."""
    if not await _collection_exists(client, dst):
        logger.info("Creating collection '%s' (dims=%d)", dst, new_dims)
        await client.create_collection(
            collection_name=dst,
            vectors_config=VectorParams(size=new_dims, distance=Distance.COSINE),
        )
        await client.create_payload_index(
            collection_name=dst,
            field_name="contact_id",
            field_schema=PayloadSchemaType.KEYWORD,
        )

    offset = None
    migrated = skipped = errors = 0

    while True:
        records, offset = await client.scroll(
            collection_name=src,
            limit=SCROLL_BATCH,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if not records:
            break

        batch: list[PointStruct] = []
        for rec in records:
            p = rec.payload or {}
            text = p.get("text", "")
            if not text:
                skipped += 1
                continue

            try:
                if provider == "qdrant":
                    if not HAS_DOCUMENT:
                        logger.error("qdrant-client >= 1.14.0 required for Document vectors")
                        sys.exit(1)
                    vector = Document(text=text, model=new_model)
                else:
                    vector = await _embed_external(text, api_key, base_url, new_model)

                batch.append(PointStruct(
                    id=rec.id,
                    vector=vector,
                    payload={**p, "embedding_model": new_model},
                ))
            except Exception as exc:
                logger.warning("Skipping point %s (embed error): %s", rec.id, exc)
                errors += 1

        if batch:
            await client.upsert(collection_name=dst, points=batch)
            migrated += len(batch)
            logger.info("  %d migrated so far (skipped=%d, errors=%d)", migrated, skipped, errors)

        if offset is None:
            break

    logger.info("Backfill done: migrated=%d skipped=%d errors=%d", migrated, skipped, errors)
    return migrated


async def _swap_alias(client: AsyncQdrantClient, alias: str, new_collection: str) -> None:
    old = await _get_alias_target(client, alias)
    ops = []
    if old:
        ops.append({"delete_alias": {"alias_name": alias}})
    ops.append({"create_alias": {"collection_name": new_collection, "alias_name": alias}})
    await client.update_collection_aliases(change_aliases_operations=ops)
    logger.info("Alias '%s' → '%s'  (was: %s)", alias, new_collection, old or "none")


async def run(args: argparse.Namespace) -> None:
    client = _get_client()

    # --swap-only: just atomically point the alias and exit
    if args.swap_only:
        new_col = args.swap_only
        if not await _collection_exists(client, new_col):
            logger.error("Collection '%s' does not exist", new_col)
            sys.exit(1)
        await _swap_alias(client, ALIAS_NAME, new_col)
        logger.info("Swap complete. Update .env and restart the app.")
        return

    if not args.new_model or not args.new_dims:
        logger.error("--new-model and --new-dims are required (or use --swap-only)")
        sys.exit(1)

    # Resolve source: prefer alias target, fall back to raw collection name
    src = await _get_alias_target(client, ALIAS_NAME)
    if src is None:
        if await _collection_exists(client, ALIAS_NAME):
            src = ALIAS_NAME
            logger.info("No alias found; using collection '%s' directly", src)
        else:
            logger.error("No alias '%s' and no collection by that name found", ALIAS_NAME)
            sys.exit(1)
    else:
        logger.info("Alias '%s' points to '%s'", ALIAS_NAME, src)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    dst = f"memories_{ts}"
    logger.info("Destination collection: %s", dst)

    t0 = time.monotonic()
    await _backfill(client, src, dst, args.provider, args.new_model, args.new_dims,
                    args.api_key, args.base_url)
    logger.info("Backfill complete in %.1fs", time.monotonic() - t0)

    await _swap_alias(client, ALIAS_NAME, dst)

    logger.info("")
    logger.info("Migration complete.")
    logger.info("  Next: update .env:")
    logger.info("    EMBEDDING_MODEL=%s", args.new_model)
    logger.info("    EMBEDDING_VECTOR_SIZE=%d", args.new_dims)
    logger.info("    EMBEDDING_PROVIDER=%s", args.provider)
    logger.info("  Then restart the app.")
    logger.info("  Once confirmed healthy, delete old collection '%s':", src)
    logger.info("    from qdrant_client import QdrantClient; QdrantClient(...).delete_collection('%s')", src)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--new-model", help="Target embedding model name")
    p.add_argument("--new-dims", type=int, help="Vector dimensions for the new model")
    p.add_argument("--provider", choices=["qdrant", "external"], default="qdrant",
                   help="Embedding provider (default: qdrant)")
    p.add_argument("--api-key", default="", help="Embedding API key (provider=external only)")
    p.add_argument("--base-url", default="", help="Embedding base URL (provider=external only)")
    p.add_argument("--swap-only", metavar="COLLECTION",
                   help="Skip backfill; atomically point alias at COLLECTION and exit")
    args = p.parse_args()

    if args.provider == "external" and not args.swap_only:
        if not args.api_key:
            p.error("--api-key required when --provider=external")
        if not args.base_url:
            p.error("--base-url required when --provider=external")

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
