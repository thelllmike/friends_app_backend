"""JWT creation/verification and password hashing."""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import settings
from app.core.errors import AppError

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def _encode(payload: dict[str, Any], minutes: float) -> str:
    now = datetime.now(timezone.utc)
    payload = {**payload, "iat": now, "exp": now + timedelta(minutes=minutes)}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: uuid.UUID, token_version: int) -> str:
    return _encode({"sub": str(user_id), "typ": "access", "tv": token_version}, settings.access_token_minutes)


def create_refresh_token(user_id: uuid.UUID, jti: uuid.UUID, family_id: uuid.UUID) -> str:
    return _encode(
        {"sub": str(user_id), "typ": "refresh", "jti": str(jti), "fam": str(family_id)},
        settings.refresh_token_days * 24 * 60,
    )


def create_admin_token(admin_id: uuid.UUID, role: str) -> str:
    return _encode({"sub": str(admin_id), "typ": "admin", "role": role}, settings.admin_token_minutes)


def decode_token(token: str, expected_type: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError:
        raise AppError(401, "token_expired", "Token expired")
    except jwt.PyJWTError:
        raise AppError(401, "invalid_token", "Invalid token")
    if payload.get("typ") != expected_type:
        raise AppError(401, "invalid_token", "Wrong token type")
    return payload
