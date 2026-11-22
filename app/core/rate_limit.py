"""Fixed-window rate limiter backed by Redis."""
from fastapi import Request

from app.core.errors import AppError
from app.core.redis import redis


async def hit(key: str, limit: int, window_seconds: int) -> bool:
    """Count one hit; return True if still within the limit."""
    full = f"rl:{key}"
    async with redis.pipeline(transaction=True) as p:
        p.incr(full)
        p.expire(full, window_seconds, nx=True)
        count, _ = await p.execute()
    return int(count) <= limit


async def enforce(key: str, limit: int, window_seconds: int) -> None:
    if not await hit(key, limit, window_seconds):
        ttl = await redis.ttl(f"rl:{key}")
        raise AppError(429, "rate_limited", "Too many requests, slow down", {"retry_after": max(ttl, 1)})


def client_ip(request: Request) -> str:
    # Caddy sets X-Forwarded-For; take the first hop.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
