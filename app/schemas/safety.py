"""Report/block schemas."""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

ReportReason = Literal["nudity_or_sexual", "harassment_or_bullying", "spam_or_scam", "underage", "other"]


class ReportIn(BaseModel):
    room_id: uuid.UUID
    reason: ReportReason
    details: str | None = Field(default=None, max_length=1000)
    block: bool = False  # also block the user ("Report and block")


class ReportOut(BaseModel):
    id: uuid.UUID
    room_id: uuid.UUID | None
    reason: str
    created_at: datetime
    call_ended: bool = False
    blocked: bool = False


class BlockIn(BaseModel):
    room_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one(self) -> "BlockIn":
        if not (self.room_id or self.user_id):
            raise ValueError("room_id or user_id is required")
        return self
