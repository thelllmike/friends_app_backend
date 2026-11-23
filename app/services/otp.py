"""Phone OTP: pluggable SMS provider interface + Redis-backed code storage."""
import hashlib
import hmac
import logging
import re
import secrets
from typing import Protocol

import httpx

from app.core.config import settings
from app.core.errors import AppError
from app.core.redis import redis

log = logging.getLogger("friends.otp")
E164 = re.compile(r"^\+[1-9]\d{6,14}$")


class OTPProvider(Protocol):
    async def send(self, phone: str, code: str) -> None: ...


class ConsoleOTPProvider:
    """Dev provider: writes the code to the log instead of sending an SMS."""

    async def send(self, phone: str, code: str) -> None:
        log.warning("OTP for %s: %s", phone, code)


class TextLKProvider:
    """Text.lk SMS gateway (Sri Lanka, international numbers too): POST {base}/sms/send."""

    async def send(self, phone: str, code: str) -> None:
        minutes = max(1, settings.otp_ttl_seconds // 60)
        payload = {
            "recipient": phone.lstrip("+"),  # Text.lk wants 94771234567, no plus
            "sender_id": settings.textlk_sender_id,
            "type": "plain",
            "message": f"{code} is your Friends code. It expires in {minutes} min. Never share it with anyone.",
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(f"{settings.textlk_api_base.rstrip('/')}/sms/send", json=payload,
                                      headers={"Authorization": f"Bearer {settings.textlk_api_token}",
                                               "Accept": "application/json"})
            body = r.json() if r.content else {}
        except (httpx.HTTPError, ValueError) as e:
            log.error("Text.lk request failed: %s", e.__class__.__name__)
            raise AppError(502, "sms_failed", "Couldn't send the code right now. Try again in a moment.")
        # Text.lk sometimes reports errors with HTTP 200, so the body's status decides.
        if r.status_code >= 400 or body.get("status") != "success":
            log.error("Text.lk rejected SMS: http=%s message=%s", r.status_code, body.get("message"))
            raise AppError(502, "sms_failed", "Couldn't send the code right now. Try again in a moment.")


PROVIDERS: dict[str, OTPProvider] = {"console": ConsoleOTPProvider(), "textlk": TextLKProvider()}


def get_provider() -> OTPProvider:
    try:
        return PROVIDERS[settings.otp_provider]
    except KeyError:
        raise AppError(500, "otp_not_configured", "OTP provider not configured")


def normalize_phone(phone: str) -> str:
    p = re.sub(r"[\s\-()]", "", phone)
    if not E164.match(p):
        raise AppError(422, "invalid_phone", "Phone must be in international format, e.g. +14155550123")
    return p


def _hash(phone: str, code: str) -> str:
    return hmac.new(settings.jwt_secret.encode(), f"{phone}:{code}".encode(), hashlib.sha256).hexdigest()


async def request_code(phone: str) -> str:
    code = f"{secrets.randbelow(1_000_000):06d}"
    key = f"otp:{phone}"
    await redis.hset(key, mapping={"h": _hash(phone, code), "attempts": 0})
    await redis.expire(key, settings.otp_ttl_seconds)
    try:
        await get_provider().send(phone, code)
    except Exception:
        await redis.delete(key)  # never leave a code behind that the user didn't receive
        raise
    return code


async def verify_code(phone: str, code: str) -> None:
    key = f"otp:{phone}"
    data = await redis.hgetall(key)
    if not data:
        raise AppError(400, "otp_expired", "Code expired, request a new one")
    attempts = await redis.hincrby(key, "attempts", 1)
    if attempts > settings.otp_max_attempts:
        await redis.delete(key)
        raise AppError(429, "otp_too_many_attempts", "Too many attempts, request a new code")
    if not hmac.compare_digest(data["h"], _hash(phone, code)):
        raise AppError(400, "otp_invalid", "Incorrect code")
    await redis.delete(key)
