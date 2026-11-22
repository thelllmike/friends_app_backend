"""Premium screen: plans, status, receipt verification and restore."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.errors import AppError
from app.core.rate_limit import enforce
from app.models.billing import Plan, Subscription
from app.models.user import User
from app.schemas.billing import FEATURES, PlanOut, PremiumStatus, RestoreIn, VerifyApple, VerifyGoogle
from app.services import billing

router = APIRouter(prefix="/premium", tags=["premium"])


async def _status(db: AsyncSession, user: User) -> PremiumStatus:
    await db.refresh(user)
    row = (await db.execute(select(Subscription, Plan.code).outerjoin(Plan, Plan.id == Subscription.plan_id)
                            .where(Subscription.user_id == user.id, Subscription.status == "active")
                            .order_by(Subscription.expires_at.desc()).limit(1))).first()
    sub, code = (row[0], row[1]) if row else (None, None)
    return PremiumStatus(is_premium=user.is_premium(), premium_until=user.premium_until,
                         platform=sub.platform if sub else None, plan=code, auto_renew=bool(sub and sub.auto_renew))


@router.get("/plans")
async def plans(db: AsyncSession = Depends(get_db)) -> dict:
    return {"plans": [PlanOut.model_validate(p, from_attributes=True) for p in await billing.active_plans(db)],
            "features": FEATURES}


@router.get("/status", response_model=PremiumStatus)
async def status(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _status(db, user)


@router.post("/verify/google", response_model=PremiumStatus)
async def verify_google(body: VerifyGoogle, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await enforce(f"billing:{user.id}", 20, 3600)
    await billing.verify(db, user, "google", body.purchase_token, body.product_id)
    return await _status(db, user)


@router.post("/verify/apple", response_model=PremiumStatus)
async def verify_apple(body: VerifyApple, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await enforce(f"billing:{user.id}", 20, 3600)
    await billing.verify(db, user, "apple", body.transaction_id, body.product_id)
    return await _status(db, user)


@router.post("/restore", response_model=PremiumStatus)
async def restore(body: RestoreIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await enforce(f"billing:{user.id}", 20, 3600)
    items: list[tuple[str, str | None]] = (
        [(p.purchase_token, p.product_id) for p in body.purchases or []] if body.platform == "google"
        else [(t, None) for t in body.transaction_ids or []])
    if not items:
        raise AppError(422, "validation_error", "Nothing to restore")
    errors = 0
    for token, product in items:
        try:
            await billing.verify(db, user, body.platform, token, product, restore=True)
        except AppError:
            errors += 1
            await db.rollback()
    if errors == len(items):
        raise AppError(400, "nothing_restored", "No valid purchases found")
    return await _status(db, user)
