"""Reports, blocks and bans."""
import logging
import uuid
from datetime import timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.base import utcnow
from app.models.call import CallRoom
from app.models.safety import Ban, Block, Report
from app.models.user import User
from app.services import app_settings, blocks

log = logging.getLogger("friends.moderation")


async def partner_in_room(db: AsyncSession, room_id: uuid.UUID, me: uuid.UUID) -> uuid.UUID:
    room = await db.get(CallRoom, room_id)
    if not room or me not in (room.caller_id, room.callee_id):
        raise AppError(404, "room_not_found", "Call not found")
    other = room.callee_id if room.caller_id == me else room.caller_id
    if other is None:
        raise AppError(404, "user_not_found", "That user no longer exists")
    return other


async def block(db: AsyncSession, me: User, target: uuid.UUID, room_id: uuid.UUID | None = None) -> None:
    if target == me.id:
        raise AppError(400, "invalid_target", "You can't block yourself")
    if not await db.get(User, target):
        raise AppError(404, "user_not_found", "User not found")
    exists = (await db.execute(select(Block).where(Block.blocker_id == me.id, Block.blocked_id == target))
              ).scalar_one_or_none()
    if not exists:
        db.add(Block(blocker_id=me.id, blocked_id=target, room_id=room_id))
        await db.commit()
    await blocks.invalidate(me.id, target)


async def unblock(db: AsyncSession, me: User, target: uuid.UUID) -> None:
    row = (await db.execute(select(Block).where(Block.blocker_id == me.id, Block.blocked_id == target))
           ).scalar_one_or_none()
    if not row:
        raise AppError(404, "not_found", "User is not blocked")
    await db.delete(row)
    await db.commit()
    await blocks.invalidate(me.id, target)


async def create_report(db: AsyncSession, me: User, room_id: uuid.UUID, reason: str, details: str | None) -> Report:
    reported = await partner_in_room(db, room_id, me.id)
    dup = (await db.execute(select(Report).where(Report.reporter_id == me.id, Report.room_id == room_id))
           ).scalar_one_or_none()
    if dup:
        return dup  # idempotent: one report per call per reporter
    report = Report(reporter_id=me.id, reported_id=reported, room_id=room_id, reason=reason, details=details)
    db.add(report)
    await db.commit()
    await maybe_auto_ban(db, reported)
    return report


async def maybe_auto_ban(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Temp-ban once `report_threshold` distinct users reported them within `report_window_hours`."""
    cfg = await app_settings.get_all(db)
    since = utcnow() - timedelta(hours=int(cfg["report_window_hours"]))
    distinct = (await db.execute(
        select(func.count(func.distinct(Report.reporter_id))).where(
            Report.reported_id == user_id, Report.created_at >= since, Report.status != "dismissed")
    )).scalar_one()
    user = await db.get(User, user_id)
    if not user or user.is_banned() or distinct < int(cfg["report_threshold"]):
        return False
    await ban(db, user, hours=int(cfg["auto_ban_hours"]), reason=f"auto: {distinct} reports", source="auto")
    return True


async def ban(db: AsyncSession, user: User, hours: int | None, reason: str, source: str,
              admin_id: uuid.UUID | None = None) -> Ban:
    """hours=None -> permanent."""
    expires = None if hours is None else utcnow() + timedelta(hours=hours)
    if expires is None:
        user.permanently_banned = True
    else:
        user.banned_until = max(filter(None, [user.banned_until, expires]))
    row = Ban(user_id=user.id, reason=reason[:255], source=source, admin_id=admin_id, expires_at=expires)
    db.add(row)
    await db.commit()
    log.warning("user banned id=%s source=%s hours=%s reason=%s", user.id, source, hours, reason)
    from app.ws.manager import manager

    await manager.force_disconnect(user.id, "banned")  # their session cleanup ends any active call
    return row


async def unban(db: AsyncSession, user: User, admin_id: uuid.UUID | None) -> None:
    user.permanently_banned = False
    user.banned_until = None
    await db.execute(update(Ban).where(Ban.user_id == user.id, Ban.lifted_at.is_(None),
                                       or_(Ban.expires_at.is_(None), Ban.expires_at > utcnow()))
                     .values(lifted_at=utcnow(), lifted_by=admin_id))
    await db.commit()
    log.info("user unbanned id=%s by=%s", user.id, admin_id)
