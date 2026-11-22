"""Shared Redis client."""
from redis.asyncio import Redis

from app.core.config import settings

redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)


def get_redis() -> Redis:
    return redis
