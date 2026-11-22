"""User lifecycle: sign up / sign in via identities, serialization, hard delete."""
import hashlib
import logging
import os
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.core.redis import redis
from app.models.base import utcnow
from app.models.user import AuthIdentity, BannedIdentity, User
from app.schemas.auth import Consent
from app.schemas.user import MeOut, PublicUser, photo_url
from app.services.identity import VerifiedIdentity

log = logging.getLogger("friends.users")


def identity_hash(provider: str, subject: str) -> str:
    return hashlib.sha256(f"{provider}:{subject}".encode()).hexdigest()


def me_out(user: User) -> MeOut:
    return MeOut(
        id=user.id, display_name=user.display_name, age=user.age, country=user.country, gender=user.gender,
        interests=list(user.interests or []), photo_url=photo_url(user.photo_path), language=user.language,
        email=user.email, phone=user.phone, onboarding_step=user.onboarding_step,
        onboarding_completed=user.onboarding_completed, profile_complete=user.profile_complete,
        is_premium=user.is_premium(), premium_until=user.premium_until, created_at=user.created_at,
    )


def public_out(user: User) -> PublicUser:
    return PublicUser(id=user.id, display_name=user.display_name, age=user.age, country=user.country,
                      photo_url=photo_url(user.photo_path))


def raise_if_banned(user: User) -> None:
    if user.is_banned():
        raise AppError(403, "banned", "Your account is suspended", {
            "until": None if user.permanently_banned else user.banned_until.isoformat(),
            "permanent": user.permanently_banned})


async def sign_in(db: AsyncSession, ident: VerifiedIdentity, consent: Consent,
                  display_name: str | None = None) -> tuple[User, bool]:
    """Return (user, is_new). Creates the account on first login if consent was given."""
    row = (await db.execute(select(AuthIdentity).where(AuthIdentity.provider == ident.provider,
                                                       AuthIdentity.subject == ident.subject))).scalar_one_or_none()
    if row:
        user = await db.get(User, row.user_id)
        assert user is not None
        raise_if_banned(user)
        return user, False

    # Ban evasion: deleted-while-banned identities can't sign up again until the ban ends.
    banned = await db.get(BannedIdentity, identity_hash(ident.provider, ident.subject))
    if banned and (banned.banned_until is None or banned.banned_until > utcnow()):
        raise AppError(403, "banned", "This account is suspended",
                       {"until": banned.banned_until.isoformat() if banned.banned_until else None,
                        "permanent": banned.banned_until is None})

    if not (consent.confirm_18_plus and consent.accept_terms):
        raise AppError(400, "consent_required",
                       "You must confirm you are 18+ and accept the Terms and Privacy Policy",
                       {"confirm_18_plus": consent.confirm_18_plus, "accept_terms": consent.accept_terms})
    now = utcnow()
    user = User(
        age_confirmed_at=now, terms_accepted_at=now, terms_version=consent.terms_version,
        email=ident.email, phone=ident.subject if ident.provider == "phone" else None,
        display_name=display_name, onboarding_step=2,  # step 1 (sign in) done -> profile setup is step 2 of 3
        interests=[],
    )
    db.add(user)
    await db.flush()
    db.add(AuthIdentity(user_id=user.id, provider=ident.provider, subject=ident.subject, email=ident.email))
    await db.flush()
    log.info("user created id=%s provider=%s", user.id, ident.provider)
    return user, True


async def link_identity(db: AsyncSession, user: User, ident: VerifiedIdentity) -> None:
    existing = (await db.execute(select(AuthIdentity).where(AuthIdentity.provider == ident.provider,
                                                            AuthIdentity.subject == ident.subject))).scalar_one_or_none()
    if existing:
        if existing.user_id == user.id:
            return
        raise AppError(409, "identity_in_use", "This login is already linked to another account")
    mine = (await db.execute(select(AuthIdentity).where(AuthIdentity.user_id == user.id,
                                                        AuthIdentity.provider == ident.provider))).scalar_one_or_none()
    if mine:
        raise AppError(409, "provider_already_linked", f"A {ident.provider} login is already linked")
    db.add(AuthIdentity(user_id=user.id, provider=ident.provider, subject=ident.subject, email=ident.email))
    if ident.provider == "phone":
        user.phone = ident.subject
    elif ident.email and not user.email:
        user.email = ident.email
    await db.commit()


async def unlink_identity(db: AsyncSession, user: User, provider: str) -> None:
    rows = (await db.execute(select(AuthIdentity).where(AuthIdentity.user_id == user.id))).scalars().all()
    target = next((r for r in rows if r.provider == provider), None)
    if not target:
        raise AppError(404, "not_found", "Provider not linked")
    if len(rows) == 1:
        raise AppError(400, "last_login_method", "You can't remove your only login method")
    await db.delete(target)
    if provider == "phone":
        user.phone = None
    await db.commit()


def delete_photo_file(path: str | None) -> None:
    if not path:
        return
    full = os.path.join(settings.media_dir, path)
    try:
        os.remove(full)
    except FileNotFoundError:
        pass


async def hard_delete(db: AsyncSession, user: User) -> None:
    """Permanently remove the user and their data (cascades to identities, tokens, blocks, subs)."""
    identities = (await db.execute(select(AuthIdentity).where(AuthIdentity.user_id == user.id))).scalars().all()
    if user.is_banned():
        until = None if user.permanently_banned else user.banned_until
        for i in identities:
            h = identity_hash(i.provider, i.subject)
            if not await db.get(BannedIdentity, h):
                db.add(BannedIdentity(identity_hash=h, banned_until=until))
    photo = user.photo_path
    uid = user.id
    await db.execute(delete(User).where(User.id == uid))
    await db.commit()
    delete_photo_file(photo)
    await purge_redis(uid)
    log.info("user hard-deleted id=%s", uid)


async def purge_redis(uid: uuid.UUID) -> None:
    await redis.delete(f"blocks:{uid}")
