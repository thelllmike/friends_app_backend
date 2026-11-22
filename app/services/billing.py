"""Subscriptions: store-verified purchases, manual grants, premium status, expiry."""
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.base import utcnow
from app.models.billing import Plan, Subscription
from app.models.user import User
from app.services import stores

log = logging.getLogger("friends.billing")


async def active_plans(db: AsyncSession) -> list[Plan]:
    return list((await db.execute(select(Plan).where(Plan.active.is_(True)).order_by(Plan.sort_order))).scalars())


async def plan_for_product(db: AsyncSession, platform: str, product_id: str) -> Plan:
    col = Plan.google_product_id if platform == "google" else Plan.apple_product_id
    plan = (await db.execute(select(Plan).where(col == product_id))).scalar_one_or_none()
    if not plan:
        raise AppError(400, "unknown_product", f"Unknown product {product_id}")
    return plan


async def recompute_premium(db: AsyncSession, user_id: uuid.UUID) -> datetime | None:
    """users.premium_until = latest expiry among active subscriptions."""
    until = (await db.execute(select(func.max(Subscription.expires_at)).where(
        Subscription.user_id == user_id, Subscription.status == "active",
        Subscription.expires_at > utcnow()))).scalar_one()
    await db.execute(update(User).where(User.id == user_id).values(premium_until=until))
    return until


async def apply_store_purchase(db: AsyncSession, user: User, p: stores.StorePurchase, restore: bool) -> Subscription:
    plan = await plan_for_product(db, p.platform, p.product_id)
    sub = (await db.execute(select(Subscription).where(Subscription.platform == p.platform,
                                                       Subscription.external_id == p.external_id))
           ).scalar_one_or_none()
    previous_owner = None
    if sub and sub.user_id != user.id:
        if not restore:
            raise AppError(409, "purchase_in_use", "This purchase belongs to another account. Use Restore purchase.")
        previous_owner = sub.user_id  # restore moves the subscription to the signed-in account
    if not sub:
        sub = Subscription(platform=p.platform, external_id=p.external_id, started_at=utcnow(), user_id=user.id)
        db.add(sub)
    sub.user_id, sub.plan_id, sub.product_id = user.id, plan.id, p.product_id
    sub.status, sub.expires_at, sub.auto_renew, sub.raw, sub.updated_at = (
        p.status, p.expires_at, p.auto_renew, p.raw, utcnow())
    await db.flush()
    await recompute_premium(db, user.id)
    if previous_owner:
        await recompute_premium(db, previous_owner)
    await db.commit()
    log.info("purchase applied user=%s platform=%s status=%s until=%s", user.id, p.platform, p.status, p.expires_at)
    return sub


async def verify(db: AsyncSession, user: User, platform: str, token: str, product_id: str | None,
                 restore: bool = False) -> Subscription:
    if platform == "google":
        if not product_id:
            raise AppError(422, "validation_error", "product_id is required for Google Play")
        plan = await plan_for_product(db, "google", product_id)
        p = stores.fake_mode_purchase("google", token, product_id, plan.duration_days) or \
            await stores.verify_google(token, product_id)
    else:
        fake_product = product_id or ""
        fake = None
        if token.startswith("test_") and fake_product:
            plan = await plan_for_product(db, "apple", fake_product)
            fake = stores.fake_mode_purchase("apple", token, fake_product, plan.duration_days)
        p = fake or await stores.verify_apple(token)
    return await apply_store_purchase(db, user, p, restore)


async def grant_manual(db: AsyncSession, user: User, days: int, admin_id: uuid.UUID, plan_code: str | None) -> Subscription:
    plan = None
    if plan_code:
        plan = (await db.execute(select(Plan).where(Plan.code == plan_code))).scalar_one_or_none()
        if not plan:
            raise AppError(404, "not_found", "Plan not found")
    start = max(utcnow(), user.premium_until or utcnow())  # extend from current expiry
    sub = Subscription(user_id=user.id, plan_id=plan.id if plan else None, platform="manual", status="active",
                       external_id=f"manual:{uuid.uuid4()}", started_at=utcnow(),
                       expires_at=start + timedelta(days=days), granted_by=admin_id, updated_at=utcnow())
    db.add(sub)
    await db.flush()
    await recompute_premium(db, user.id)
    await db.commit()
    return sub


async def revoke_all(db: AsyncSession, user: User) -> int:
    res = await db.execute(update(Subscription).where(Subscription.user_id == user.id,
                                                      Subscription.status == "active")
                           .values(status="revoked", updated_at=utcnow()))
    await recompute_premium(db, user.id)
    await db.commit()
    return res.rowcount or 0


async def expire_due(db: AsyncSession) -> int:
    """Background task: mark lapsed subscriptions expired and refresh users.premium_until."""
    now = utcnow()
    rows = (await db.execute(update(Subscription).where(Subscription.status == "active",
                                                        Subscription.expires_at <= now)
                             .values(status="expired", updated_at=now).returning(Subscription.user_id))).all()
    stale = (await db.execute(select(User.id).where(User.premium_until.is_not(None),
                                                    User.premium_until <= now))).scalars().all()
    for uid in {r[0] for r in rows} | set(stale):
        await recompute_premium(db, uid)
    await db.commit()
    return len(rows)
