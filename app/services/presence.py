"""Online presence in Redis.

online:all            ZSET uid -> last_seen (unix ts)
online:p:{platform}   ZSET uid -> last_seen
online:meta           HASH uid -> "platform|country"
online_count          cached total for the Home screen
"""
import time
import uuid

from app.core.redis import redis

PRESENCE_TTL = 90  # seconds without heartbeat before a user counts as offline
PLATFORMS = ("android", "ios", "web")


async def mark_online(uid: uuid.UUID, platform: str, country: str | None) -> None:
    now = time.time()
    async with redis.pipeline(transaction=False) as p:
        p.zadd("online:all", {str(uid): now})
        for pf in PLATFORMS:  # a user is online on one platform at a time (newest session wins)
            if pf != platform:
                p.zrem(f"online:p:{pf}", str(uid))
        p.zadd(f"online:p:{platform}", {str(uid): now})
        p.hset("online:meta", str(uid), f"{platform}|{country or ''}")
        await p.execute()


async def mark_offline(uid: uuid.UUID) -> None:
    async with redis.pipeline(transaction=False) as p:
        p.zrem("online:all", str(uid))
        for pf in PLATFORMS:
            p.zrem(f"online:p:{pf}", str(uid))
        p.hdel("online:meta", str(uid))
        await p.execute()


async def counts() -> dict[str, int]:
    cutoff = time.time() - PRESENCE_TTL
    async with redis.pipeline(transaction=False) as p:
        p.zcount("online:all", cutoff, "+inf")
        for pf in PLATFORMS:
            p.zcount(f"online:p:{pf}", cutoff, "+inf")
        total, *per = await p.execute()
    return {"total": int(total), **{pf: int(n) for pf, n in zip(PLATFORMS, per)}}


async def by_country() -> dict[str, int]:
    cutoff = time.time() - PRESENCE_TTL
    live = set(await redis.zrangebyscore("online:all", cutoff, "+inf"))
    out: dict[str, int] = {}
    for uid, meta in (await redis.hgetall("online:meta")).items():
        if uid in live:
            cc = meta.split("|", 1)[1] or "??"
            out[cc] = out.get(cc, 0) + 1
    return out


async def cleanup() -> None:
    """Drop users whose heartbeat expired (e.g. worker crashed)."""
    cutoff = time.time() - PRESENCE_TTL
    stale = await redis.zrangebyscore("online:all", "-inf", cutoff)
    async with redis.pipeline(transaction=False) as p:
        p.zremrangebyscore("online:all", "-inf", cutoff)
        for pf in PLATFORMS:
            p.zremrangebyscore(f"online:p:{pf}", "-inf", cutoff)
        if stale:
            p.hdel("online:meta", *stale)
        await p.execute()


async def refresh_cached_count() -> int:
    total = (await counts())["total"]
    await redis.set("online_count", total, ex=60)
    return total


async def cached_online_count() -> int:
    v = await redis.get("online_count")
    return int(v) if v is not None else await refresh_cached_count()
