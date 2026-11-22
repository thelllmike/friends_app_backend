"""Periodic background jobs. Each job takes a short Redis leader lock so only one worker runs it."""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from sqlalchemy import select, update

from app.core.db import SessionLocal
from app.core.redis import redis
from app.models.admin import OnlineSnapshot
from app.models.base import utcnow
from app.models.billing import Subscription
from app.models.call import CallRoom
from app.services import billing, presence, stores

log = logging.getLogger("friends.tasks")


async def expire_premium() -> None:
    async with SessionLocal() as db:
        n = await billing.expire_due(db)
        if n:
            log.info("expired %s subscriptions", n)


async def recheck_renewals() -> None:
    """Without store webhooks, re-verify auto-renewing subs about to lapse so renewals extend premium."""
    async with SessionLocal() as db:
        subs = (await db.execute(select(Subscription).where(
            Subscription.status == "active", Subscription.auto_renew.is_(True),
            Subscription.platform.in_(("google", "apple")),
            Subscription.expires_at <= utcnow() + timedelta(hours=1)).limit(100))).scalars().all()
        for sub in subs:
            if (sub.raw or {}).get("fake"):
                continue
            try:
                p = await (stores.verify_google(sub.external_id, sub.product_id or "") if sub.platform == "google"
                           else stores.verify_apple(sub.external_id))
                sub.status, sub.expires_at, sub.auto_renew, sub.updated_at = p.status, p.expires_at, p.auto_renew, utcnow()
                await billing.recompute_premium(db, sub.user_id)
                await db.commit()
            except Exception as e:  # store hiccup: try again next round
                await db.rollback()
                log.warning("renewal check failed sub=%s err=%s", sub.id, e)


async def refresh_online() -> None:
    await presence.cleanup()
    await presence.refresh_cached_count()


async def snapshot_online() -> None:
    c = await presence.counts()
    async with SessionLocal() as db:
        db.add(OnlineSnapshot(taken_at=utcnow(), total=c["total"], android=c["android"], ios=c["ios"], web=c["web"]))
        await db.commit()


async def close_stale_rooms() -> None:
    """Rooms whose Redis state vanished (worker crash) get closed in the DB."""
    async with SessionLocal() as db:
        await db.execute(update(CallRoom).where(CallRoom.ended_at.is_(None),
                                                CallRoom.matched_at < utcnow() - timedelta(hours=12))
                         .values(ended_at=utcnow(), end_reason="stale", duration_seconds=0))
        await db.commit()


JOBS: list[tuple[str, int, Callable[[], Awaitable[None]]]] = [
    ("online", 5, refresh_online),
    ("expire_premium", 60, expire_premium),
    ("renewals", 600, recheck_renewals),
    ("snapshot", 300, snapshot_online),
    ("stale_rooms", 3600, close_stale_rooms),
]


async def _loop(name: str, every: int, fn: Callable[[], Awaitable[None]]) -> None:
    while True:
        try:
            # Leader lock lives slightly less than the interval -> exactly one worker per tick.
            if await redis.set(f"job:{name}", "1", nx=True, ex=max(every - 1, 1)):
                await fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("job %s failed", name)
        await asyncio.sleep(every)


def start() -> list[asyncio.Task]:
    return [asyncio.create_task(_loop(n, e, f), name=f"job-{n}") for n, e, f in JOBS]
