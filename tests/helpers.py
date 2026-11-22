"""Helpers for the live-server integration tests (server must run with OTP_DEV_ECHO=true)."""
import os
import random

import httpx

BASE = os.environ.get("API_URL", "http://127.0.0.1:8000")
WS_BASE = BASE.replace("http", "ws", 1)


def client() -> httpx.Client:
    return httpx.Client(base_url=BASE, timeout=10)


def random_phone() -> str:
    return "+1555" + "".join(random.choices("0123456789", k=7))


def signup(c: httpx.Client, phone: str | None = None, profile: dict | None = None) -> dict:
    """Create a user via phone OTP and optionally fill the profile. Returns the auth response + headers."""
    phone = phone or random_phone()
    r = c.post("/auth/otp/request", json={"phone": phone}, headers={"x-forwarded-for": phone})
    assert r.status_code == 200, r.text
    code = r.json()["dev_code"]
    r = c.post("/auth/otp/verify", json={"phone": phone, "code": code, "confirm_18_plus": True,
                                         "accept_terms": True}, headers={"x-forwarded-for": phone})
    assert r.status_code == 200, r.text
    data = r.json()
    data["headers"] = {"Authorization": f"Bearer {data['access_token']}"}
    data["phone"] = phone
    if profile is not None:
        body = {"display_name": "Tester", "age": 25, "country": "US", "gender": "male", "interests": ["Music"]}
        body.update(profile)
        r = c.patch("/me", json=body, headers=data["headers"])
        assert r.status_code == 200, r.text
        data["user"] = r.json()
    return data


def set_setting(key: str, value) -> None:
    """Write a runtime setting straight to the DB (tests only) and drop the Redis cache."""
    import asyncio
    import json

    from redis.asyncio import Redis
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import settings

    async def _go():
        eng = create_async_engine(settings.database_url, poolclass=NullPool)
        async with eng.begin() as c:
            await c.execute(text("INSERT INTO app_settings(key, value, updated_at) VALUES (:k, CAST(:v AS json), now()) "
                                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"),
                            {"k": key, "v": json.dumps(value)})
        await eng.dispose()
        r = Redis.from_url(settings.redis_url)
        await r.delete("app_settings")
        await r.aclose()

    asyncio.run(_go()) if not _in_loop() else _run_in_thread(_go)


def _in_loop() -> bool:
    import asyncio
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _run_in_thread(fn) -> None:
    import asyncio
    import threading
    t = threading.Thread(target=lambda: asyncio.run(fn()))
    t.start()
    t.join()
