"""Admin API schemas."""
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field

from app.models.admin import ADMIN_ROLES


class AdminLogin(BaseModel):
    email: str = Field(max_length=255)
    password: str


class AdminCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12)
    role: Literal[ADMIN_ROLES]  # type: ignore[valid-type]


class BanIn(BaseModel):
    hours: int | None = Field(default=None, ge=1, le=24 * 365)  # None = permanent
    reason: str = Field(min_length=1, max_length=255)


class GrantPremium(BaseModel):
    days: int = Field(ge=1, le=3650)
    plan_code: str | None = None


class ReviewIn(BaseModel):
    action: Literal["dismiss", "resolve", "ban"]
    ban_hours: int | None = Field(default=None, ge=1, le=24 * 365)  # with action=ban; None = permanent
    note: str | None = Field(default=None, max_length=1000)


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


class PlanUpdate(BaseModel):
    name: str | None = None
    price_cents: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    duration_days: int | None = Field(default=None, ge=1)
    google_product_id: str | None = None
    apple_product_id: str | None = None
    badge: str | None = None
    active: bool | None = None
    sort_order: int | None = None
