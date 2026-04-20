from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(Exception):
    """Raised when a required environment variable is missing at startup."""
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Vapi
    VAPI_API_KEY: str
    VAPI_ASSISTANT_ID: str
    VAPI_PHONE_NUMBER_ID: str

    # Qdrant
    QDRANT_API_KEY: str
    QDRANT_ENDPOINT: str

    # Embedding
    # EMBEDDING_PROVIDER=qdrant  → Qdrant cloud inference (free, no external API needed)
    # EMBEDDING_PROVIDER=external → Gemini (native API) or any OpenAI-compatible endpoint
    EMBEDDING_PROVIDER: str = "qdrant"
    EMBEDDING_MODEL: str = "sentence-transformers/all-minilm-l6-v2"
    EMBEDDING_VECTOR_SIZE: int = 384  # 384 for all-minilm/e5-small; 3072 for Gemini

    # Required only when EMBEDDING_PROVIDER=external
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_BASE_URL: str = ""   # Gemini URL triggers native embedContent; others use OpenAI-compat

    # Auth — set APP_SECRET_KEY to protect the dashboard and API; empty = auth disabled
    APP_SECRET_KEY: str = ""

    # Google Calendar (optional)
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REFRESH_TOKEN: str = ""

    # Optional: Vapi SIP trunk ID for SIP-mode contacts (separate from PSTN phone number)
    VAPI_SIP_TRUNK_ID: str = ""

    # Public key for Vapi Web SDK (browser-safe; from Vapi dashboard → Account → Public Key)
    VAPI_PUBLIC_KEY: str = ""

    # App identity (used in assistant prompts and webhook URL)
    USER_NAME: str = "your friend"
    APP_BASE: str = ""


_REQUIRED_VARS = [
    "VAPI_API_KEY",
    "VAPI_ASSISTANT_ID",
    "VAPI_PHONE_NUMBER_ID",
    "QDRANT_API_KEY",
    "QDRANT_ENDPOINT",
]


def get_settings() -> Settings:
    import os

    for var in _REQUIRED_VARS:
        if not os.environ.get(var):
            raise ConfigurationError(
                f"Required environment variable '{var}' is missing. "
                f"Please set it before starting the application."
            )

    settings = Settings()

    if settings.EMBEDDING_PROVIDER == "external":
        for var in ("EMBEDDING_API_KEY", "EMBEDDING_BASE_URL"):
            if not getattr(settings, var):
                raise ConfigurationError(
                    f"EMBEDDING_PROVIDER=external requires '{var}' to be set."
                )

    return settings
