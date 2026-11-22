"""Issue / rotate / revoke refresh tokens with reuse detection."""
import uuid
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.core.security import create_access_token, create_refresh_token, decode_token
from app.models.base import utcnow
from app.models.user import RefreshToken, User


async def issue_pair(db: AsyncSession, user: User, family_id: uuid.UUID | None = None,
                     user_agent: str | None = None) -> dict[str, str | int]:
    jti, fam = uuid.uuid4(), family_id or uuid.uuid4()
    db.add(RefreshToken(id=jti, user_id=user.id, family_id=fam, user_agent=(user_agent or "")[:255],
                        expires_at=utcnow() + timedelta(days=settings.refresh_token_days)))
    await db.flush()
    return {
        "access_token": create_access_token(user.id, user.token_version),
        "refresh_token": create_refresh_token(user.id, jti, fam),
        "token_type": "bearer",
        "expires_in": settings.access_token_minutes * 60,
    }


async def revoke_family(db: AsyncSession, family_id: uuid.UUID) -> None:
    await db.execute(update(RefreshToken).where(RefreshToken.family_id == family_id,
                                                RefreshToken.revoked_at.is_(None)).values(revoked_at=utcnow()))


async def rotate(db: AsyncSession, refresh_token: str, user_agent: str | None) -> tuple[User, dict]:
    payload = decode_token(refresh_token, "refresh")
    row = await db.get(RefreshToken, uuid.UUID(payload["jti"]))
    if not row:
        raise AppError(401, "invalid_token", "Unknown refresh token")
    if row.revoked_at is not None:
        # A revoked token was replayed: assume theft, kill the whole chain.
        await revoke_family(db, row.family_id)
        await db.commit()
        raise AppError(401, "token_reused", "Refresh token reuse detected, please sign in again")
    if row.expires_at < utcnow():
        raise AppError(401, "token_expired", "Refresh token expired")
    user = await db.get(User, row.user_id)
    if not user:
        raise AppError(401, "invalid_token", "User not found")
    pair = await issue_pair(db, user, family_id=row.family_id, user_agent=user_agent)
    row.revoked_at = utcnow()
    row.replaced_by = uuid.UUID(decode_token(str(pair["refresh_token"]), "refresh")["jti"])
    await db.commit()
    return user, pair


async def revoke(db: AsyncSession, refresh_token: str) -> None:
    payload = decode_token(refresh_token, "refresh")
    await revoke_family(db, uuid.UUID(payload["fam"]))
    await db.commit()
