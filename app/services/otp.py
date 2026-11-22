"""Phone OTP: pluggable SMS provider interface + Redis-backed code storage."""
import hashlib
import hmac
import logging
import re
import secrets
from typing import Protocol

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


# Register real SMS providers here, e.g. {"console": ..., "my_sms_gateway": MySmsGatewayProvider()}
PROVIDERS: dict[str, OTPProvider] = {"console": ConsoleOTPProvider()}


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
    await get_provider().send(phone, code)
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
