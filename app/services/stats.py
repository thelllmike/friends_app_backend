"""Dashboard statistics."""
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin import OnlineSnapshot
from app.models.base import utcnow
from app.models.call import CallRoom
from app.models.safety import Report
from app.models.user import User
from app.services import presence


def pct_change(cur: float | None, prev: float | None) -> float | None:
    if cur is None or not prev:
        return None
    return round((cur - prev) / prev * 100, 1)


def metric(cur: float | None, prev: float | None) -> dict:
    return {"value": cur, "last_week": prev, "change_pct": pct_change(cur, prev)}


async def _calls_between(db: AsyncSession, start: datetime, end: datetime) -> tuple[int, float | None]:
    count = (await db.execute(select(func.count()).select_from(CallRoom).where(
        CallRoom.matched_at >= start, CallRoom.matched_at < end))).scalar_one()
    avg = (await db.execute(select(func.avg(CallRoom.duration_seconds)).where(
        CallRoom.ended_at >= start, CallRoom.ended_at < end, CallRoom.connected_at.is_not(None)))).scalar_one()
    return int(count), (round(float(avg), 1) if avg is not None else None)


async def _open_reports_at(db: AsyncSession, at: datetime) -> int:
    return int((await db.execute(select(func.count()).select_from(Report).where(
        Report.created_at <= at, or_(Report.status == "open", Report.reviewed_at > at)))).scalar_one())


async def _snapshot_near(db: AsyncSession, at: datetime) -> OnlineSnapshot | None:
    return (await db.execute(select(OnlineSnapshot).where(
        OnlineSnapshot.taken_at.between(at - timedelta(minutes=30), at + timedelta(minutes=30)))
        .order_by(func.abs(func.extract("epoch", OnlineSnapshot.taken_at - at))).limit(1))).scalar_one_or_none()


async def overview(db: AsyncSession) -> dict:
    now = utcnow()
    week_ago = now - timedelta(days=7)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    live = await presence.counts()
    snap = await _snapshot_near(db, week_ago)
    prev_online = {k: getattr(snap, k) for k in ("total", "android", "ios", "web")} if snap else {}
    calls_now, avg_now = await _calls_between(db, today, now)
    calls_prev, avg_prev = await _calls_between(db, today - timedelta(days=7), week_ago)
    return {
        "generated_at": now,
        "online_now": metric(live["total"], prev_online.get("total")),
        "online_by_platform": {p: metric(live[p], prev_online.get(p)) for p in ("android", "ios", "web")},
        "web_users_online": metric(live["web"], prev_online.get("web")),
        "calls_today": metric(calls_now, calls_prev),
        "avg_call_seconds": metric(avg_now, avg_prev),
        "open_reports": metric(await _open_reports_at(db, now), await _open_reports_at(db, week_ago)),
    }


async def calls_per_day(db: AsyncSession, days: int) -> list[dict]:
    rows = (await db.execute(text("""
        SELECT d::date AS day, COUNT(r.id) AS calls, COALESCE(AVG(r.duration_seconds)
               FILTER (WHERE r.connected_at IS NOT NULL), 0) AS avg_seconds
        FROM generate_series((now() AT TIME ZONE 'UTC')::date - (:days - 1), (now() AT TIME ZONE 'UTC')::date,
                             interval '1 day') d
        LEFT JOIN call_rooms r ON (r.matched_at AT TIME ZONE 'UTC')::date = d::date
        GROUP BY d ORDER BY d"""), {"days": days})).all()
    return [{"date": r.day.isoformat(), "calls": int(r.calls), "avg_seconds": round(float(r.avg_seconds), 1)}
            for r in rows]


async def top_countries(db: AsyncSession, days: int, limit: int) -> list[dict]:
    since = utcnow() - timedelta(days=days)
    rows = (await db.execute(select(User.country, func.count()).where(
        User.last_active_at >= since, User.country.is_not(None)).group_by(User.country)
        .order_by(func.count().desc()).limit(limit))).all()
    total = sum(n for _, n in rows) or 1
    return [{"country": cc, "active_users": n, "share_pct": round(n / total * 100, 1)} for cc, n in rows]


async def countries(db: AsyncSession) -> list[dict]:
    week = utcnow() - timedelta(days=7)
    rows = (await db.execute(select(
        User.country, func.count().label("users"),
        func.count().filter(User.last_active_at >= week).label("active_7d"),
        func.count().filter(and_(User.premium_until.is_not(None), User.premium_until > utcnow())).label("premium"),
    ).where(User.country.is_not(None)).group_by(User.country).order_by(text("users DESC")))).all()
    online = await presence.by_country()
    return [{"country": r.country, "users": r.users, "active_7d": r.active_7d, "premium": r.premium,
             "online_now": online.get(r.country, 0)} for r in rows]



async def online_history(db: AsyncSession, hours: int, points: int) -> list[dict]:
    since = utcnow() - timedelta(hours=hours)
    bucket = max(1, hours * 3600 // points)
    rows = (await db.execute(text("""
        SELECT to_timestamp(floor(extract(epoch FROM taken_at) / :b) * :b) AS ts,
               ROUND(AVG(total)) AS total, ROUND(AVG(android)) AS android, ROUND(AVG(ios)) AS ios,
               ROUND(AVG(web)) AS web
        FROM online_snapshots WHERE taken_at >= :since GROUP BY 1 ORDER BY 1"""),
        {"b": bucket, "since": since})).all()
    return [{"t": r.ts, "total": int(r.total), "android": int(r.android), "ios": int(r.ios), "web": int(r.web)}
            for r in rows]
