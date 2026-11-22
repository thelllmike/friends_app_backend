"""WebSocket endpoint: /ws?token=<access JWT>&platform=android|ios|web (or Authorization header)."""
import asyncio
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.deps import user_from_access_token
from app.core.errors import AppError
from app.models.base import utcnow
from app.services import presence
from app.ws.manager import CLOSE_BANNED, CLOSE_IDLE, CLOSE_UNAUTHORIZED, manager
from app.ws.session import Session

router = APIRouter()
log = logging.getLogger("friends.ws")
PLATFORMS = {"android", "ios", "web"}


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    token = ws.query_params.get("token")
    auth = ws.headers.get("authorization", "")
    if not token and auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    platform = ws.query_params.get("platform", "android").lower()
    platform = platform if platform in PLATFORMS else "android"

    await ws.accept()
    try:
        if not token:
            raise AppError(401, "unauthorized", "Missing token")
        async with SessionLocal() as db:
            user = await user_from_access_token(token, db, allow_banned=True)
            if user.is_banned():
                await ws.send_json({"type": "error", "code": "banned", "message": "Your account is suspended"})
                await ws.close(code=CLOSE_BANNED)
                return
            user.last_active_at, user.last_platform = utcnow(), platform
            await db.commit()
            uid, country, user_public = str(user.id), user.country, {
                "id": str(user.id), "display_name": user.display_name, "is_premium": user.is_premium()}
    except AppError as e:
        await ws.send_json({"type": "error", "code": e.code, "message": e.message})
        await ws.close(code=CLOSE_UNAUTHORIZED)
        return

    sid = await manager.register(uid, ws)
    session = Session(ws, uid, sid, platform, country)
    await presence.mark_online(uuid.UUID(uid), platform, country)
    await session.send({"type": "connected", "session_id": sid, "user": user_public,
                        "heartbeat_seconds": settings.ws_idle_timeout_seconds // 2})
    log.info("ws connected uid=%s sid=%s platform=%s", uid, sid, platform)
    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=settings.ws_idle_timeout_seconds)
            except asyncio.TimeoutError:
                await ws.close(code=CLOSE_IDLE, reason="idle_timeout")
                break
            await session.handle_raw(raw)
    except (WebSocketDisconnect, RuntimeError, KeyError):  # KeyError: binary frame
        pass
    except Exception:
        log.exception("ws error uid=%s", uid)
    finally:
        await session.cleanup()
        manager.unregister(uid, sid)
        if await manager.is_current(uid, sid):
            await presence.mark_offline(uuid.UUID(uid))
        log.info("ws closed uid=%s sid=%s", uid, sid)
