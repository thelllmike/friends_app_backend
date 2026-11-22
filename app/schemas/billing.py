"""Premium schemas."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

FEATURES = [
    {"id": "gender_filter", "title": "Gender filter", "description": "Choose who you meet"},
    {"id": "priority_matching", "title": "Priority matching", "description": "Skip the line and match faster"},
]


class PlanOut(BaseModel):
    code: str
    name: str
    duration_days: int
    price_cents: int
    currency: str
    google_product_id: str | None
    apple_product_id: str | None
    badge: str | None


class PremiumStatus(BaseModel):
    is_premium: bool
    premium_until: datetime | None
    platform: str | None
    plan: str | None
    auto_renew: bool


class VerifyGoogle(BaseModel):
    product_id: str
    purchase_token: str = Field(min_length=1, max_length=512)


class VerifyApple(BaseModel):
    transaction_id: str = Field(min_length=1, max_length=64)  # (original) transaction id from StoreKit
    product_id: str | None = None


class RestoreIn(BaseModel):
    platform: Literal["google", "apple"]
    # Google: purchase tokens from queryPurchasesAsync; Apple: original transaction ids from StoreKit
    purchases: list[VerifyGoogle] | None = None
    transaction_ids: list[str] | None = Field(default=None, max_length=20)
