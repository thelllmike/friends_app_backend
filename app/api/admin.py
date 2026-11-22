"""Admin dashboard API (role-based)."""
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import ROLE_PERMISSIONS, get_current_admin, require_permission
from app.core.errors import AppError
from app.core.rate_limit import client_ip, hit
from app.core.redis import redis
from app.core.security import create_admin_token, hash_password, verify_password
from app.models.admin import AdminUser
from app.models.base import utcnow
from app.models.billing import Plan, Subscription
from app.models.call import CallRoom
from app.models.safety import Ban, Report
from app.models.user import User
from app.schemas import admin as s
from app.schemas.user import photo_url
from app.services import app_settings, billing, moderation, presence, stats

router = APIRouter(prefix="/admin", tags=["admin"])
STATS = Depends(require_permission("stats:read"))
USERS_READ = Depends(require_permission("users:read"))
USERS_BAN = Depends(require_permission("users:ban"))
REPORTS = Depends(require_permission("reports:manage"))
PREMIUM = Depends(require_permission("premium:manage"))
SETTINGS = Depends(require_permission("settings:manage"))
ADMINS = Depends(require_permission("admins:manage"))


# ---- auth ----------------------------------------------------------------------------------------

@router.post("/auth/login")
async def login(body: s.AdminLogin, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    # Only failed attempts count: per IP and per account (brute-force protection).
    ip_key, email_key = f"admin_fail_ip:{client_ip(request)}", f"admin_fail_email:{body.email.lower()}"
    for key, limit in ((ip_key, 10), (email_key, 5)):
        if int(await redis.get(f"rl:{key}") or 0) >= limit:
            raise AppError(429, "rate_limited", "Too many failed attempts, try again later")
    admin = (await db.execute(select(AdminUser).where(AdminUser.email == body.email.lower()))).scalar_one_or_none()
    if not admin or not admin.active or not verify_password(body.password, admin.password_hash):
        await hit(ip_key, 10, 900)
        await hit(email_key, 5, 900)
        raise AppError(401, "invalid_credentials", "Wrong email or password")
    admin.last_login_at = utcnow()
    await db.commit()
    return {"access_token": create_admin_token(admin.id, admin.role), "token_type": "bearer",
            "admin": {"id": str(admin.id), "email": admin.email, "role": admin.role}}


@router.get("/me")
async def me(admin: AdminUser = Depends(get_current_admin)) -> dict:
    return {"id": str(admin.id), "email": admin.email, "role": admin.role,
            "permissions": sorted(ROLE_PERMISSIONS.get(admin.role, set()))}


@router.get("/admins", dependencies=[ADMINS])
async def list_admins(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(AdminUser).order_by(AdminUser.created_at))).scalars().all()
    return [{"id": str(a.id), "email": a.email, "role": a.role, "active": a.active,
             "last_login_at": a.last_login_at} for a in rows]


@router.post("/admins", dependencies=[ADMINS], status_code=201)
async def create_admin(body: s.AdminCreate, db: AsyncSession = Depends(get_db)) -> dict:
    if (await db.execute(select(AdminUser).where(AdminUser.email == body.email.lower()))).scalar_one_or_none():
        raise AppError(409, "exists", "Admin already exists")
    a = AdminUser(email=body.email.lower(), password_hash=hash_password(body.password), role=body.role)
    db.add(a)
    await db.commit()
    return {"id": str(a.id), "email": a.email, "role": a.role}


# ---- stats ---------------------------------------------------------------------------------------

@router.get("/overview", dependencies=[STATS])
async def overview(db: AsyncSession = Depends(get_db)) -> dict:
    return await stats.overview(db)


@router.get("/stats/calls", dependencies=[STATS])
async def calls_per_day(days: int = 14, db: AsyncSession = Depends(get_db)) -> dict:
    if days not in (14, 30, 90):
        raise AppError(422, "validation_error", "days must be 14, 30 or 90")
    return {"days": days, "series": await stats.calls_per_day(db, days)}


@router.get("/stats/top-countries", dependencies=[STATS])
async def top_countries(days: int = Query(7, ge=1, le=90), limit: int = Query(10, ge=1, le=50),
                        db: AsyncSession = Depends(get_db)) -> list[dict]:
    return await stats.top_countries(db, days, limit)


@router.get("/stats/online", dependencies=[STATS])
async def online_by_platform() -> dict:
    return await presence.counts()


@router.get("/stats/online-history", dependencies=[STATS])
async def online_history(hours: int = Query(24, ge=1, le=24 * 14), points: int = Query(48, ge=2, le=500),
                         db: AsyncSession = Depends(get_db)) -> dict:
    """Online-count snapshots for sparklines, bucketed to at most `points` points."""
    return {"hours": hours, "series": await stats.online_history(db, hours, points)}


@router.get("/calls", dependencies=[USERS_READ])
async def recent_calls(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                       user_id: uuid.UUID | None = None, db: AsyncSession = Depends(get_db)) -> dict:
    base = select(CallRoom)
    if user_id:
        base = base.where(or_(CallRoom.caller_id == user_id, CallRoom.callee_id == user_id))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(base.order_by(CallRoom.matched_at.desc()).limit(limit).offset(offset))).scalars().all()
    ids = {i for r in rows for i in (r.caller_id, r.callee_id) if i}
    users = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(ids)))).scalars()} if ids else {}

    def brief(uid):
        u = users.get(uid)
        return {"id": str(uid), "display_name": u.display_name, "country": u.country} if u else None

    return {"total": total, "items": [
        {"id": str(r.id), "matched_at": r.matched_at, "connected_at": r.connected_at, "ended_at": r.ended_at,
         "duration_seconds": r.duration_seconds, "end_reason": r.end_reason,
         "caller": brief(r.caller_id), "callee": brief(r.callee_id),
         "caller_platform": r.caller_platform, "callee_platform": r.callee_platform} for r in rows]}


@router.get("/countries", dependencies=[STATS])
async def countries(db: AsyncSession = Depends(get_db)) -> list[dict]:
    return await stats.countries(db)


# ---- users ---------------------------------------------------------------------------------------

def _user_row(u: User, reports: int = 0) -> dict:
    return {"id": str(u.id), "display_name": u.display_name, "age": u.age, "country": u.country, "gender": u.gender,
            "email": u.email, "phone": u.phone, "photo_url": photo_url(u.photo_path), "created_at": u.created_at,
            "last_active_at": u.last_active_at, "last_platform": u.last_platform, "is_premium": u.is_premium(),
            "premium_until": u.premium_until, "is_banned": u.is_banned(), "banned_until": u.banned_until,
            "permanently_banned": u.permanently_banned, "reports_received": reports}


@router.get("/users", dependencies=[USERS_READ])
async def list_users(q: str | None = None, banned: bool | None = None, premium: bool | None = None,
                     country: str | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                     db: AsyncSession = Depends(get_db)) -> dict:
    now = utcnow()
    stmt = select(User)
    if q:
        like = f"%{q.strip()}%"
        conds = [User.display_name.ilike(like), User.email.ilike(like), User.phone.ilike(like)]
        try:
            conds.append(User.id == uuid.UUID(q.strip()))
        except ValueError:
            pass
        stmt = stmt.where(or_(*conds))
    if banned is not None:
        cond = or_(User.permanently_banned.is_(True), User.banned_until > now)
        stmt = stmt.where(cond if banned else ~cond)
    if premium is not None:
        cond = User.premium_until > now
        stmt = stmt.where(cond if premium else or_(User.premium_until.is_(None), User.premium_until <= now))
    if country:
        stmt = stmt.where(User.country == country.upper())
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    users = (await db.execute(stmt.order_by(User.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    counts = dict((await db.execute(select(Report.reported_id, func.count()).where(
        Report.reported_id.in_([u.id for u in users])).group_by(Report.reported_id))).all()) if users else {}
    return {"total": total, "items": [_user_row(u, counts.get(u.id, 0)) for u in users]}


async def _get_user(db: AsyncSession, user_id: uuid.UUID) -> User:
    u = await db.get(User, user_id)
    if not u:
        raise AppError(404, "not_found", "User not found")
    return u


@router.get("/users/{user_id}", dependencies=[USERS_READ])
async def user_detail(user_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict:
    u = await _get_user(db, user_id)
    reports = (await db.execute(select(Report).where(Report.reported_id == u.id)
                                .order_by(Report.created_at.desc()).limit(50))).scalars().all()
    bans = (await db.execute(select(Ban).where(Ban.user_id == u.id).order_by(Ban.created_at.desc()))).scalars().all()
    subs = (await db.execute(select(Subscription).where(Subscription.user_id == u.id)
                             .order_by(Subscription.created_at.desc()))).scalars().all()
    calls = (await db.execute(select(func.count()).select_from(CallRoom).where(
        or_(CallRoom.caller_id == u.id, CallRoom.callee_id == u.id)))).scalar_one()
    return {**_user_row(u, len(reports)), "interests": u.interests, "language": u.language, "total_calls": calls,
            "online": await _is_online(u.id),
            "reports": [{"id": str(r.id), "reason": r.reason, "status": r.status, "room_id": str(r.room_id) if r.room_id
                         else None, "created_at": r.created_at} for r in reports],
            "bans": [{"id": str(b.id), "reason": b.reason, "source": b.source, "expires_at": b.expires_at,
                      "lifted_at": b.lifted_at, "created_at": b.created_at} for b in bans],
            "subscriptions": [{"id": str(x.id), "platform": x.platform, "status": x.status, "product_id": x.product_id,
                               "expires_at": x.expires_at, "auto_renew": x.auto_renew} for x in subs]}


async def _is_online(uid: uuid.UUID) -> bool:
    from app.core.redis import redis
    import time

    score = await redis.zscore("online:all", str(uid))
    return score is not None and score > time.time() - presence.PRESENCE_TTL


@router.post("/users/{user_id}/ban")
async def ban_user(user_id: uuid.UUID, body: s.BanIn, admin: AdminUser = USERS_BAN,
                   db: AsyncSession = Depends(get_db)) -> dict:
    u = await _get_user(db, user_id)
    await moderation.ban(db, u, body.hours, body.reason, "admin", admin.id)
    return _user_row(u)


@router.post("/users/{user_id}/unban")
async def unban_user(user_id: uuid.UUID, admin: AdminUser = USERS_BAN, db: AsyncSession = Depends(get_db)) -> dict:
    u = await _get_user(db, user_id)
    await moderation.unban(db, u, admin.id)
    return _user_row(u)


@router.post("/users/{user_id}/premium")
async def grant_premium(user_id: uuid.UUID, body: s.GrantPremium, admin: AdminUser = PREMIUM,
                        db: AsyncSession = Depends(get_db)) -> dict:
    u = await _get_user(db, user_id)
    await billing.grant_manual(db, u, body.days, admin.id, body.plan_code)
    await db.refresh(u)
    return _user_row(u)


@router.delete("/users/{user_id}/premium", dependencies=[PREMIUM])
async def revoke_premium(user_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict:
    u = await _get_user(db, user_id)
    n = await billing.revoke_all(db, u)
    await db.refresh(u)
    return {**_user_row(u), "revoked_subscriptions": n}


# ---- reports -------------------------------------------------------------------------------------

@router.get("/reports", dependencies=[REPORTS])
async def reports_queue(status: Literal["open", "dismissed", "actioned"] = "open",
                        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                        db: AsyncSession = Depends(get_db)) -> dict:
    base = select(Report).where(Report.status == status)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(base.order_by(Report.created_at.asc() if status == "open" else Report.created_at.desc())
                             .limit(limit).offset(offset))).scalars().all()
    ids = {r.reported_id for r in rows} | {r.reporter_id for r in rows}
    users = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(ids)))).scalars()} if ids else {}
    open_counts = dict((await db.execute(select(Report.reported_id, func.count()).where(
        Report.reported_id.in_(ids), Report.status == "open").group_by(Report.reported_id))).all()) if ids else {}

    def brief(uid):
        u = users.get(uid)
        return {"id": str(uid), "display_name": u.display_name, "country": u.country,
                "photo_url": photo_url(u.photo_path), "is_banned": u.is_banned()} if u else None

    return {"total": total, "items": [
        {"id": str(r.id), "reason": r.reason, "details": r.details, "status": r.status, "created_at": r.created_at,
         "room_id": str(r.room_id) if r.room_id else None, "reporter": brief(r.reporter_id),
         "reported": brief(r.reported_id), "reported_open_reports": open_counts.get(r.reported_id, 0)}
        for r in rows]}


@router.get("/reports/{report_id}", dependencies=[REPORTS])
async def report_detail(report_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict:
    r = await db.get(Report, report_id)
    if not r:
        raise AppError(404, "not_found", "Report not found")
    room = await db.get(CallRoom, r.room_id) if r.room_id else None
    others = (await db.execute(select(Report).where(Report.reported_id == r.reported_id, Report.id != r.id)
                               .order_by(Report.created_at.desc()).limit(20))).scalars().all() if r.reported_id else []
    reported = await db.get(User, r.reported_id) if r.reported_id else None
    return {"id": str(r.id), "reason": r.reason, "details": r.details, "status": r.status,
            "created_at": r.created_at, "review_note": r.review_note, "reviewed_at": r.reviewed_at,
            "reported": _user_row(reported, (await db.execute(select(func.count()).select_from(Report).where(
                Report.reported_id == reported.id))).scalar_one()) if reported else None,
            "room": {"id": str(room.id), "matched_at": room.matched_at, "connected_at": room.connected_at,
                     "ended_at": room.ended_at, "duration_seconds": room.duration_seconds,
                     "end_reason": room.end_reason, "caller_platform": room.caller_platform,
                     "callee_platform": room.callee_platform} if room else None,
            "other_reports": [{"id": str(o.id), "reason": o.reason, "status": o.status, "created_at": o.created_at}
                              for o in others]}


@router.post("/reports/{report_id}/review")
async def review_report(report_id: uuid.UUID, body: s.ReviewIn, admin: AdminUser = REPORTS,
                        db: AsyncSession = Depends(get_db)) -> dict:
    r = await db.get(Report, report_id)
    if not r:
        raise AppError(404, "not_found", "Report not found")
    r.status = "dismissed" if body.action == "dismiss" else "actioned"
    r.reviewed_by, r.reviewed_at, r.review_note = admin.id, utcnow(), body.note
    await db.commit()
    if body.action == "ban" and r.reported_id:
        u = await db.get(User, r.reported_id)
        if u:
            await moderation.ban(db, u, body.ban_hours, f"report {r.id}: {r.reason}", "admin", admin.id)
    return {"id": str(r.id), "status": r.status}


# ---- settings & plans ----------------------------------------------------------------------------

@router.get("/settings", dependencies=[STATS])
async def get_settings(db: AsyncSession = Depends(get_db)) -> dict:
    return await app_settings.get_all(db)


@router.put("/settings", dependencies=[SETTINGS])
async def put_settings(body: s.SettingsUpdate, db: AsyncSession = Depends(get_db)) -> dict:
    clean = {}
    for k, v in body.values.items():
        if k not in app_settings.DEFAULTS:
            raise AppError(422, "unknown_setting", f"Unknown setting {k}")
        want = type(app_settings.DEFAULTS[k])
        if want is int and isinstance(v, bool) or not isinstance(v, want):
            raise AppError(422, "invalid_setting", f"{k} must be {want.__name__}")
        if want is int and v < 0:
            raise AppError(422, "invalid_setting", f"{k} must be >= 0")
        clean[k] = v
    return await app_settings.update(db, clean)


@router.get("/plans", dependencies=[STATS])
async def list_plans(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Plan).order_by(Plan.sort_order))).scalars().all()
    return [{c.name: getattr(p, c.name) for c in Plan.__table__.columns} for p in rows]


@router.patch("/plans/{code}", dependencies=[SETTINGS])
async def update_plan(code: str, body: s.PlanUpdate, db: AsyncSession = Depends(get_db)) -> dict:
    p = (await db.execute(select(Plan).where(Plan.code == code))).scalar_one_or_none()
    if not p:
        raise AppError(404, "not_found", "Plan not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(p, k, v.upper() if k == "currency" else v)
    await db.commit()
    return {c.name: getattr(p, c.name) for c in Plan.__table__.columns}
