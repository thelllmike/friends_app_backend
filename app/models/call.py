"""Call rooms (one per match) with timing for stats."""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import new_uuid


class CallRoom(Base):
    __tablename__ = "call_rooms"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    caller_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    callee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    matched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # first SDP answer relayed
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    end_reason: Mapped[str | None] = mapped_column(String(20))  # next|leave|disconnect|report|block|replaced|banned
    ended_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    caller_platform: Mapped[str | None] = mapped_column(String(10))
    callee_platform: Mapped[str | None] = mapped_column(String(10))
