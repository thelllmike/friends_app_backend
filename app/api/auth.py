"""Auth endpoints: Google, Apple, phone OTP, refresh, logout, linked accounts."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user, get_current_user_allow_banned
from app.core.errors import AppError
from app.core.rate_limit import client_ip, enforce
from app.models.user import AuthIdentity, User
from app.schemas import auth as s
from app.services import identity, otp, tokens, users
from app.services.identity import VerifiedIdentity

router = APIRouter(prefix="/auth", tags=["auth"])


async def _login_rate_limit(request: Request) -> None:
    await enforce(f"login:{client_ip(request)}", settings.login_rate_per_minute, 60)


async def _finish(db: AsyncSession, request: Request, ident: VerifiedIdentity, consent: s.Consent,
                  display_name: str | None = None) -> s.AuthOut:
    user, is_new = await users.sign_in(db, ident, consent, display_name)
    pair = await tokens.issue_pair(db, user, user_agent=request.headers.get("user-agent"))
    await db.commit()
    await db.refresh(user)
    return s.AuthOut(**pair, is_new_user=is_new, user=users.me_out(user))


@router.post("/google", response_model=s.AuthOut, dependencies=[Depends(_login_rate_limit)])
async def login_google(body: s.GoogleLogin, request: Request, db: AsyncSession = Depends(get_db)) -> s.AuthOut:
    return await _finish(db, request, await identity.verify_google(body.id_token), body)


@router.post("/apple", response_model=s.AuthOut, dependencies=[Depends(_login_rate_limit)])
async def login_apple(body: s.AppleLogin, request: Request, db: AsyncSession = Depends(get_db)) -> s.AuthOut:
    return await _finish(db, request, await identity.verify_apple(body.identity_token), body, body.display_name)


@router.post("/otp/request", response_model=s.OtpRequestOut, dependencies=[Depends(_login_rate_limit)])
async def otp_request(body: s.OtpRequest) -> s.OtpRequestOut:
    phone = otp.normalize_phone(body.phone)
    await enforce(f"otp:{phone}", settings.otp_requests_per_10min, 600)
    code = await otp.request_code(phone)
    dev = code if settings.otp_dev_echo and not settings.is_production else None
    return s.OtpRequestOut(sent=True, expires_in=settings.otp_ttl_seconds, dev_code=dev)


@router.post("/otp/verify", response_model=s.AuthOut, dependencies=[Depends(_login_rate_limit)])
async def otp_verify(body: s.OtpVerify, request: Request, db: AsyncSession = Depends(get_db)) -> s.AuthOut:
    phone = otp.normalize_phone(body.phone)
    await otp.verify_code(phone, body.code)
    return await _finish(db, request, VerifiedIdentity("phone", phone, None), body)


@router.post("/refresh", response_model=s.TokenPair, dependencies=[Depends(_login_rate_limit)])
async def refresh(body: s.RefreshIn, request: Request, db: AsyncSession = Depends(get_db)) -> s.TokenPair:
    user, pair = await tokens.rotate(db, body.refresh_token, request.headers.get("user-agent"))
    users.raise_if_banned(user)
    return s.TokenPair(**pair)


@router.post("/logout", status_code=204)
async def logout(body: s.RefreshIn, db: AsyncSession = Depends(get_db)) -> None:
    await tokens.revoke(db, body.refresh_token)


@router.post("/logout-all", status_code=204)
async def logout_all(user: User = Depends(get_current_user_allow_banned), db: AsyncSession = Depends(get_db)) -> None:
    user.token_version += 1  # invalidates every access token immediately
    from sqlalchemy import update

    from app.models.base import utcnow
    from app.models.user import RefreshToken

    await db.execute(update(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
                     .values(revoked_at=utcnow()))
    await db.commit()


# ---- Linked accounts ------------------------------------------------------------------------------

@router.get("/linked-accounts", response_model=list[s.LinkedAccount])
async def linked_accounts(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(AuthIdentity).where(AuthIdentity.user_id == user.id)
                             .order_by(AuthIdentity.created_at))).scalars().all()
    return [s.LinkedAccount(provider=r.provider, email=r.email, phone=r.subject if r.provider == "phone" else None,
                            linked_at=r.created_at.isoformat()) for r in rows]


@router.post("/link/google", status_code=204)
async def link_google(body: s.LinkGoogle, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await users.link_identity(db, user, await identity.verify_google(body.id_token))


@router.post("/link/apple", status_code=204)
async def link_apple(body: s.LinkApple, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await users.link_identity(db, user, await identity.verify_apple(body.identity_token))


@router.post("/link/phone", status_code=204)
async def link_phone(body: s.LinkPhone, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    phone = otp.normalize_phone(body.phone)
    await otp.verify_code(phone, body.code)
    await users.link_identity(db, user, VerifiedIdentity("phone", phone, None))


@router.delete("/link/{provider}", status_code=204)
async def unlink(provider: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if provider not in ("google", "apple", "phone"):
        raise AppError(404, "not_found", "Unknown provider")
    await users.unlink_identity(db, user, provider)
