"""User/profile schemas."""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.core.config import settings
from app.schemas.common import INTERESTS, Gender, validate_country


def photo_url(path: str | None) -> str | None:
    return f"{settings.media_base_url.rstrip('/')}/{path}" if path else None


class ProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=2, max_length=40)
    age: int | None = Field(default=None, ge=18, le=120)
    country: str | None = None
    gender: Gender | None = None
    interests: list[str] | None = None
    language: str | None = Field(default=None, max_length=10)

    @field_validator("display_name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        return " ".join(v.split()) if v else v

    @field_validator("country")
    @classmethod
    def _country(cls, v: str | None) -> str | None:
        return validate_country(v)

    @field_validator("interests")
    @classmethod
    def _interests(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        lookup = {i.lower(): i for i in INTERESTS}
        out: list[str] = []
        for item in v:
            canon = lookup.get(item.strip().lower())
            if not canon:
                raise ValueError(f"Unknown interest '{item}'. Allowed: {', '.join(INTERESTS)}")
            if canon not in out:
                out.append(canon)
        if len(out) > 5:
            raise ValueError("Pick at most 5 interests")
        return out


class OnboardingStep(BaseModel):
    step: int = Field(ge=1, le=3)
    completed: bool = False


class PublicUser(BaseModel):
    """What a chat partner is allowed to see."""

    id: uuid.UUID
    display_name: str | None
    age: int | None
    country: str | None
    photo_url: str | None


class MeOut(BaseModel):
    id: uuid.UUID
    display_name: str | None
    age: int | None
    country: str | None
    gender: str | None
    interests: list[str]
    photo_url: str | None
    language: str
    email: str | None
    phone: str | None
    onboarding_step: int
    onboarding_total_steps: int = 3
    onboarding_completed: bool
    profile_complete: bool
    is_premium: bool
    premium_until: datetime | None
    created_at: datetime


class BlockedUserOut(BaseModel):
    user_id: uuid.UUID
    display_name: str | None
    photo_url: str | None
    blocked_at: datetime
