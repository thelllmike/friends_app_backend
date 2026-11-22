"""Users, linked login identities and refresh tokens."""
import uuid
from datetime import datetime

from sqlalchemy import ARRAY, Boolean, DateTime, ForeignKey, Integer, SmallInteger, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import TimestampMixin, new_uuid, utcnow


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    # Profile
    display_name: Mapped[str | None] = mapped_column(String(40))
    age: Mapped[int | None] = mapped_column(SmallInteger)
    country: Mapped[str | None] = mapped_column(String(2), index=True)  # ISO 3166-1 alpha-2
    gender: Mapped[str | None] = mapped_column(String(10))  # male | female | other
    interests: Mapped[list[str]] = mapped_column(ARRAY(String(20)), default=list, server_default="{}")
    photo_path: Mapped[str | None] = mapped_column(String(255))
    language: Mapped[str] = mapped_column(String(10), default="en", server_default="en")
    onboarding_step: Mapped[int] = mapped_column(SmallInteger, default=1, server_default="1")
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Contact (from identity providers)
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    phone: Mapped[str | None] = mapped_column(String(20), index=True)
    # Consent (required on signup)
    age_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terms_accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terms_version: Mapped[str] = mapped_column(String(20), default="1", server_default="1")
    # Moderation
    banned_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    permanently_banned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Premium cache (source of truth: subscriptions)
    premium_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Bumped to invalidate every outstanding token (logout-all, ban, delete)
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_platform: Mapped[str | None] = mapped_column(String(10))  # android | ios | web
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    identities: Mapped[list["AuthIdentity"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    def is_banned(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return self.permanently_banned or (self.banned_until is not None and self.banned_until > now)

    def is_premium(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return self.premium_until is not None and self.premium_until > now

    @property
    def profile_complete(self) -> bool:
        return all([self.display_name, self.age, self.country, self.gender])


class AuthIdentity(TimestampMixin, Base):
    """A linked login method (google / apple / phone)."""

    __tablename__ = "auth_identities"
    __table_args__ = (UniqueConstraint("provider", "subject", name="uq_identity_provider_subject"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(10))  # google | apple | phone
    subject: Mapped[str] = mapped_column(String(255))  # provider user id / E.164 phone
    email: Mapped[str | None] = mapped_column(String(255))

    user: Mapped[User] = relationship(back_populates="identities")


class RefreshToken(TimestampMixin, Base):
    """Rotating refresh tokens. `family_id` groups a rotation chain for reuse detection."""

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)  # = jti
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_agent: Mapped[str | None] = mapped_column(String(255))


class BannedIdentity(TimestampMixin, Base):
    """SHA-256 of provider:subject of users who deleted their account while banned (prevents ban evasion)."""

    __tablename__ = "banned_identities"

    identity_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    banned_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # null = permanent


