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

    # Embedding (OpenAI-compatible; supports Gemini, nomic, etc.)
    EMBEDDING_API_KEY: str
    EMBEDDING_BASE_URL: str
    EMBEDDING_MODEL: str = "text-embedding-004"

    # Google Calendar (optional)
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REFRESH_TOKEN: str = ""

    # Optional: Vapi SIP trunk ID for SIP-mode contacts (separate from PSTN phone number)
    # Create one in Vapi dashboard → Phone Numbers → Add → SIP Trunk
    VAPI_SIP_TRUNK_ID: str = ""

    # Public key for Vapi Web SDK (browser-safe; from Vapi dashboard → Account → Public Key)
    # Enables browser-based WebRTC calls — free, no PSTN charges, passes variableValues correctly.
    VAPI_PUBLIC_KEY: str = ""

    # App identity (used in assistant prompts and webhook URL)
    USER_NAME: str = "your friend"          # Name of the person on whose behalf calls are made
    APP_BASE: str = ""                      # Public base URL, e.g. https://xyz.ngrok-free.dev


_REQUIRED_VARS = [
    "VAPI_API_KEY",
    "VAPI_ASSISTANT_ID",
    "VAPI_PHONE_NUMBER_ID",
    "QDRANT_API_KEY",
    "QDRANT_ENDPOINT",
    "EMBEDDING_API_KEY",
    "EMBEDDING_BASE_URL",
]


def get_settings() -> Settings:
    """
    Load and validate settings. Raises ConfigurationError with the variable name
    if any required env var is missing.
    """
    import os

    for var in _REQUIRED_VARS:
        if not os.environ.get(var):
            raise ConfigurationError(
                f"Required environment variable '{var}' is missing. "
                f"Please set it before starting the application."
            )

    return Settings()
