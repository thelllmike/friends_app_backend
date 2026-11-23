"""Public endpoints: health, online count, static lists, safety tips."""
import pycountry
from fastapi import APIRouter
from sqlalchemy import text

from app.core.db import engine
from app.core.redis import redis
from app.schemas.common import INTERESTS
from app.services import app_settings, presence

router = APIRouter(tags=["meta"])

SAFETY_TIPS = [
    {"id": "personal_info", "title": "Keep personal info private",
     "body": "Never share your full name, address, school, workplace, passwords or financial details."},
    {"id": "money", "title": "Never send money",
     "body": "Anyone asking for money, gift cards or crypto is a scammer. Report them."},
    {"id": "links", "title": "Don't open unknown links",
     "body": "Links from strangers can lead to phishing or malware."},
    {"id": "recording", "title": "Assume you might be recorded",
     "body": "Don't do anything on camera you wouldn't want shared."},
    {"id": "report", "title": "Report and block",
     "body": "If someone makes you uncomfortable, tap Report or Block. Reports are private."},
    {"id": "minors", "title": "18+ only",
     "body": "Friends is for adults. Report anyone who appears to be under 18."},
    {"id": "meet", "title": "Be careful meeting in person",
     "body": "If you decide to meet someone, choose a public place and tell a friend."},
]


@router.api_route("/health", methods=["GET", "HEAD"])
async def health() -> dict:
    db_ok = redis_ok = True
    try:
        async with engine.connect() as c:
            await c.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    try:
        await redis.ping()
    except Exception:
        redis_ok = False
    return {"status": "ok" if db_ok and redis_ok else "degraded", "db": db_ok, "redis": redis_ok}


@router.get("/online-count")
async def online_count() -> dict:
    return {"online": await presence.cached_online_count()}


@router.get("/meta/interests")
async def interests() -> list[str]:
    return list(INTERESTS)


@router.get("/meta/countries")
async def countries() -> list[dict]:
    return sorted(({"code": c.alpha_2, "name": c.name} for c in pycountry.countries), key=lambda c: c["name"])


@router.get("/meta/languages")
async def languages() -> list[str]:
    return await app_settings.get("supported_languages")


@router.get("/safety-tips")
async def safety_tips() -> list[dict]:
    return SAFETY_TIPS
