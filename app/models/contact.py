import re
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Literal
from datetime import datetime
from uuid import uuid4

E164_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$")


class TimeWindow(BaseModel):
    start: str  # "HH:MM"
    end: str    # "HH:MM"


class SocialHandles(BaseModel):
    twitter: Optional[str] = None
    instagram: Optional[str] = None
    linkedin: Optional[str] = None


class Contact(BaseModel):
    contact_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    phone: str                          # E.164 format

    @field_validator("phone")
    @classmethod
    def validate_phone_e164(cls, v: str) -> str:
        if not E164_PATTERN.match(v):
            raise ValueError("phone must be in E.164 format (e.g. +12125551234)")
        return v
    sip: Optional[str] = None
    contact_method: Literal["phone", "sip"] = "phone"
    tags: list[str] = []
    timezone: str                       # IANA timezone string
    last_called: Optional[datetime] = None
    last_spoken: Optional[datetime] = None
    call_time_preference: Literal["morning", "evening", "specific_time", "none"] = "none"
    preferred_time_window: Optional[TimeWindow] = None
    next_call_at: Optional[datetime] = None
    priority_boost: float = 0.0
    last_call_outcome: Optional[Literal["answered", "busy", "no_answer"]] = None
    last_call_note: Optional[str] = None
    call_started_at: Optional[datetime] = None  # set when call is initiated; cleared on webhook receipt
    social_handles: SocialHandles = Field(default_factory=SocialHandles)
    birthday: Optional[str] = None    # YYYY-MM-DD
    anniversary: Optional[str] = None  # YYYY-MM-DD
    relationship_type: Literal["personal", "business"] = "personal"
