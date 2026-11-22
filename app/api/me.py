"""Current-user profile endpoints."""
import io
import os
import uuid

from fastapi import APIRouter, Depends, File, UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user, get_current_user_allow_banned
from app.core.errors import AppError
from app.models.user import User
from app.schemas.user import MeOut, OnboardingStep, ProfileUpdate
from app.services import app_settings, users

router = APIRouter(prefix="/me", tags=["me"])
PHOTO_MAX_SIDE = 768


@router.get("", response_model=MeOut)
async def get_me(user: User = Depends(get_current_user_allow_banned)) -> MeOut:
    return users.me_out(user)


@router.patch("", response_model=MeOut)
async def update_profile(body: ProfileUpdate, user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)) -> MeOut:
    data = body.model_dump(exclude_unset=True)
    if "language" in data and data["language"] is not None:
        if data["language"] not in await app_settings.get("supported_languages"):
            raise AppError(422, "unsupported_language", "Language not supported")
    for field, value in data.items():
        if value is None and field in ("display_name", "age", "country", "gender", "language"):
            continue  # required profile fields can't be cleared
        setattr(user, field, value)
    # Saving the profile setup screen completes step 2 of 3.
    if user.profile_complete and user.onboarding_step < 3:
        user.onboarding_step = 3
    await db.commit()
    await db.refresh(user)
    return users.me_out(user)


@router.put("/onboarding", response_model=MeOut)
async def set_onboarding(body: OnboardingStep, user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)) -> MeOut:
    if body.completed and not user.profile_complete:
        raise AppError(400, "profile_incomplete", "Complete your profile first")
    user.onboarding_step = body.step
    user.onboarding_completed = body.completed
    await db.commit()
    await db.refresh(user)
    return users.me_out(user)


@router.post("/photo", response_model=MeOut)
async def upload_photo(file: UploadFile = File(...), user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)) -> MeOut:
    raw = await file.read(settings.max_photo_bytes + 1)
    if len(raw) > settings.max_photo_bytes:
        raise AppError(413, "file_too_large", f"Max photo size is {settings.max_photo_bytes // (1024 * 1024)} MB")
    try:
        img = Image.open(io.BytesIO(raw))
        if img.format not in ("JPEG", "PNG", "WEBP", "HEIF", "MPO"):
            raise AppError(415, "unsupported_media", "Use a JPEG, PNG or WebP image")
        img = ImageOps.exif_transpose(img).convert("RGB")  # re-encode: strips EXIF/GPS and any payload
    except (UnidentifiedImageError, OSError):
        raise AppError(415, "unsupported_media", "File is not a valid image")
    img.thumbnail((PHOTO_MAX_SIDE, PHOTO_MAX_SIDE))
    rel = f"avatars/{uuid.uuid4().hex}.jpg"
    full = os.path.join(settings.media_dir, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    img.save(full, "JPEG", quality=85, optimize=True)
    old = user.photo_path
    user.photo_path = rel
    await db.commit()
    users.delete_photo_file(old)
    await db.refresh(user)
    return users.me_out(user)


@router.delete("/photo", response_model=MeOut)
async def delete_photo(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> MeOut:
    old, user.photo_path = user.photo_path, None
    await db.commit()
    users.delete_photo_file(old)
    await db.refresh(user)
    return users.me_out(user)


@router.delete("", status_code=204)
async def delete_account(user: User = Depends(get_current_user_allow_banned), db: AsyncSession = Depends(get_db)):
    """Hard delete: removes the user and all personal data. Irreversible."""
    from app.ws.manager import manager

    await manager.force_disconnect(user.id, "account_deleted")
    await users.hard_delete(db, user)
