"""Reports and blocks (Report & block sheet, Settings > Blocked users)."""
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.rate_limit import enforce
from app.models.safety import Block, Report
from app.models.user import User
from app.schemas.safety import BlockIn, ReportIn, ReportOut
from app.schemas.user import BlockedUserOut, photo_url
from app.services import moderation
from app.services import matchmaking as mm
from app.ws.session import end_room_and_notify

router = APIRouter(tags=["safety"])


async def _end_if_current(user_id: uuid.UUID, room_id: uuid.UUID, reason: str) -> bool:
    room = await mm.current_room(str(user_id))
    if room and room.room_id == str(room_id):
        await end_room_and_notify(room.room_id, reason, str(user_id))
        return True
    return False


@router.post("/reports", response_model=ReportOut, status_code=201)
async def report(body: ReportIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Private to the reporter: the reported user is only told the partner left."""
    await enforce(f"report:{user.id}", settings.reports_per_hour, 3600)
    await moderation.partner_in_room(db, body.room_id, user.id)  # validates the reporter was in this call
    ended = await _end_if_current(user.id, body.room_id, "report")  # end first, then (maybe) auto-ban
    r = await moderation.create_report(db, user, body.room_id, body.reason, body.details)
    if body.block and r.reported_id:
        await moderation.block(db, user, r.reported_id, body.room_id)
    return ReportOut(id=r.id, room_id=r.room_id, reason=r.reason, created_at=r.created_at,
                     call_ended=ended, blocked=body.block)


@router.get("/reports/mine", response_model=list[ReportOut])
async def my_reports(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Report).where(Report.reporter_id == user.id)
                             .order_by(Report.created_at.desc()).limit(100))).scalars().all()
    return [ReportOut(id=r.id, room_id=r.room_id, reason=r.reason, created_at=r.created_at) for r in rows]


@router.post("/blocks", status_code=201)
async def block(body: BlockIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """"You will not be matched again." Ends the current call if it's with that user."""
    target = body.user_id or await moderation.partner_in_room(db, body.room_id, user.id)
    await moderation.block(db, user, target, body.room_id)
    ended = False
    room = await mm.current_room(str(user.id))
    if room and room.partner_of(str(user.id)) == str(target):
        await end_room_and_notify(room.room_id, "block", str(user.id))
        ended = True
    return {"blocked_user_id": str(target), "call_ended": ended}


@router.get("/blocks", response_model=list[BlockedUserOut])
async def list_blocks(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Block, User).join(User, User.id == Block.blocked_id)
                             .where(Block.blocker_id == user.id).order_by(Block.created_at.desc()))).all()
    return [BlockedUserOut(user_id=u.id, display_name=u.display_name, photo_url=photo_url(u.photo_path),
                           blocked_at=b.created_at) for b, u in rows]


@router.delete("/blocks/{user_id}", status_code=204)
async def unblock(user_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await moderation.unblock(db, user, user_id)
