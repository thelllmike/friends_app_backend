"""Runtime settings (feature flags, thresholds) stored in DB, cached in Redis."""
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import redis
from app.models.admin import AppSetting
from app.models.base import utcnow

# Defaults used when a key is missing from the DB. Admins can override any of these.
DEFAULTS: dict[str, Any] = {
    "gender_filter_enabled": True,
    "country_filter_enabled": True,
    "priority_matching_enabled": True,
    "chat_enabled": True,
    "maintenance_mode": False,
    "report_threshold": 3,  # distinct reporters ...
    "report_window_hours": 24,  # ... within this window -> auto temp ban
    "auto_ban_hours": 24,
    "recent_match_minutes": 10,  # don't rematch the same pair within N minutes
    "supported_languages": ["en", "es", "pt", "fr", "de", "it", "ru", "tr", "ar", "hi", "id", "ja", "ko", "zh"],
}
_CACHE_KEY = "app_settings"
_CACHE_TTL = 30


async def get_all(db: AsyncSession | None = None) -> dict[str, Any]:
    cached = await redis.get(_CACHE_KEY)
    if cached:
        return {**DEFAULTS, **json.loads(cached)}
    if db is None:
        from app.core.db import SessionLocal

        async with SessionLocal() as s:
            rows = (await s.execute(select(AppSetting))).scalars().all()
    else:
        rows = (await db.execute(select(AppSetting))).scalars().all()
    stored = {r.key: r.value for r in rows}
    await redis.set(_CACHE_KEY, json.dumps(stored), ex=_CACHE_TTL)
    return {**DEFAULTS, **stored}


async def get(key: str) -> Any:
    return (await get_all())[key]


async def update(db: AsyncSession, values: dict[str, Any]) -> dict[str, Any]:
    for k, v in values.items():
        row = await db.get(AppSetting, k)
        if row:
            row.value, row.updated_at = v, utcnow()
        else:
            db.add(AppSetting(key=k, value=v, updated_at=utcnow()))
    await db.commit()
    await redis.delete(_CACHE_KEY)
    return await get_all(db)
