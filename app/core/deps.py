"""FastAPI dependencies: current user / current admin with role checks."""
import uuid
from collections.abc import Callable

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.errors import AppError
from app.core.security import decode_token
from app.models.admin import AdminUser
from app.models.base import utcnow
from app.models.user import User


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AppError(401, "unauthorized", "Missing bearer token")
    return authorization[7:].strip()


async def user_from_access_token(token: str, db: AsyncSession, allow_banned: bool = False) -> User:
    payload = decode_token(token, "access")
    user = await db.get(User, uuid.UUID(payload["sub"]))
    if not user or user.token_version != payload.get("tv"):
        raise AppError(401, "invalid_token", "Session is no longer valid")
    if not allow_banned and user.is_banned():
        raise AppError(
            403, "banned", "Your account is suspended",
            {"until": user.banned_until.isoformat() if user.banned_until and not user.permanently_banned else None,
             "permanent": user.permanently_banned},
        )
    return user


async def get_current_user(
    authorization: str | None = Header(default=None), db: AsyncSession = Depends(get_db)
) -> User:
    return await user_from_access_token(_bearer(authorization), db)


async def get_current_user_allow_banned(
    authorization: str | None = Header(default=None), db: AsyncSession = Depends(get_db)
) -> User:
    """Used by endpoints a banned user may still call (view profile, delete account, logout)."""
    return await user_from_access_token(_bearer(authorization), db, allow_banned=True)


# Role -> permissions. Higher roles include everything below them.
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "analyst": {"stats:read"},
    "moderator": {"stats:read", "users:read", "users:ban", "reports:manage"},
    "admin": {"stats:read", "users:read", "users:ban", "reports:manage", "premium:manage", "settings:manage"},
    "superadmin": {"stats:read", "users:read", "users:ban", "reports:manage", "premium:manage", "settings:manage",
                   "admins:manage"},
}


async def get_current_admin(
    authorization: str | None = Header(default=None), db: AsyncSession = Depends(get_db)
) -> AdminUser:
    payload = decode_token(_bearer(authorization), "admin")
    admin = await db.get(AdminUser, uuid.UUID(payload["sub"]))
    if not admin or not admin.active:
        raise AppError(401, "invalid_token", "Admin account disabled")
    return admin


def require_permission(permission: str) -> Callable:
    async def _dep(admin: AdminUser = Depends(get_current_admin)) -> AdminUser:
        if permission not in ROLE_PERMISSIONS.get(admin.role, set()):
            raise AppError(403, "forbidden", f"Requires permission {permission}")
        return admin

    return _dep


async def touch_active(user: User, db: AsyncSession, platform: str | None = None) -> None:
    user.last_active_at = utcnow()
    if platform:
        user.last_platform = platform
    await db.commit()
