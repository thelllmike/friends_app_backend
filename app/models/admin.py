"""Admin accounts, app settings and stats snapshots."""
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import TimestampMixin, new_uuid

ADMIN_ROLES = ("superadmin", "admin", "moderator", "analyst")


class AdminUser(TimestampMixin, Base):
    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(15))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AppSetting(Base):
    """Key/value runtime settings editable from the admin dashboard (feature flags, thresholds)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[object] = mapped_column(JSON)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OnlineSnapshot(Base):
    """Periodic online counts so the dashboard can compare with last week."""

    __tablename__ = "online_snapshots"

    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    total: Mapped[int] = mapped_column(Integer)
    android: Mapped[int] = mapped_column(Integer)
    ios: Mapped[int] = mapped_column(Integer)
    web: Mapped[int] = mapped_column(Integer)
