import hashlib
import logging

from openai import AsyncOpenAI
from app.config import get_settings

logger = logging.getLogger(__name__)
VECTOR_SIZE = 768


def _deterministic_fallback_embedding(text: str, size: int = VECTOR_SIZE) -> list[float]:
    """
    Deterministic local embedding fallback used when remote embedding call fails.
    Produces a stable vector in [-1, 1] with fixed dimension.
    """
    values: list[float] = []
    seed = text.encode("utf-8")
    counter = 0

    while len(values) < size:
        digest = hashlib.sha256(seed + counter.to_bytes(8, "big")).digest()
        for i in range(0, len(digest), 4):
            chunk = digest[i:i + 4]
            if len(chunk) < 4:
                continue
            num = int.from_bytes(chunk, "big", signed=False)
            values.append((num / 2**31) - 1.0)
            if len(values) == size:
                break
        counter += 1

    return values


async def embed(text: str) -> list[float]:
    """
    Calls the configured embedding model via OpenAI-compatible embeddings endpoint.
    Supports Gemini (text-embedding-004), nomic-embed-text, or any compatible provider.
    Returns a float vector of dimension 768.
    """
    settings = get_settings()
    client = AsyncOpenAI(
        api_key=settings.EMBEDDING_API_KEY,
        base_url=settings.EMBEDDING_BASE_URL,
    )
    try:
        response = await client.embeddings.create(
            model=settings.EMBEDDING_MODEL,
            input=text,
        )
        return response.data[0].embedding
    except Exception as exc:
        logger.warning("Embedding API failed; using deterministic fallback embedding: %s", exc)
        return _deterministic_fallback_embedding(text)
