from pydantic import BaseModel, Field
from typing import Literal
from datetime import datetime, UTC
from uuid import uuid4


class MemoryEntry(BaseModel):
    entry_id: str = Field(default_factory=lambda: str(uuid4()))
    contact_id: str
    type: Literal["summary", "highlight", "fact", "social", "commitment"]
    text: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CallbackIntent(BaseModel):
    type: Literal["relative", "absolute", "none"]
    value: str = ""


class ExtractionResult(BaseModel):
    summary: str = ""
    highlights: list[str] = []
    facts: list[dict] = []
    followups: list[str] = []
    callback: CallbackIntent = Field(default_factory=lambda: CallbackIntent(type="none"))
    call_time_preference: Literal["morning", "evening", "specific_time", "none"] = "none"
