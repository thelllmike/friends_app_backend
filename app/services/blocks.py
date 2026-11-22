"""Block relationships, cached per user in Redis as a set of ids blocked in EITHER direction."""
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import redis
from app.models.safety import Block

_LOADED = "_"  # sentinel member: set has been loaded from DB


async def excluded_ids(db: AsyncSession, uid: uuid.UUID) -> set[str]:
    key = f"blocks:{uid}"
    members = await redis.smembers(key)
    if members:
        return members - {_LOADED}
    rows = (await db.execute(select(Block.blocker_id, Block.blocked_id).where(
        or_(Block.blocker_id == uid, Block.blocked_id == uid)))).all()
    ids = {str(b if a == uid else a) for a, b in rows}
    await redis.sadd(key, _LOADED, *ids)
    await redis.expire(key, 3600)
    return ids


async def invalidate(*uids: uuid.UUID) -> None:
    await redis.delete(*[f"blocks:{u}" for u in uids])
