"""WebSocket connection registry with cross-worker delivery over Redis pub/sub.

Each user has at most one live session (newest wins). Messages are addressed to (uid, sid); if the
target session lives on this worker it's sent directly, otherwise it's published on `ws:deliver` and
the worker that owns it delivers.
"""
import asyncio
import json
import logging
import uuid
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from app.core.redis import redis

log = logging.getLogger("friends.ws")
CHANNEL = "ws:deliver"

# Close codes the client can switch on
CLOSE_UNAUTHORIZED = 4401
CLOSE_BANNED = 4403
CLOSE_REPLACED = 4409
CLOSE_RATE_LIMIT = 4429
CLOSE_IDLE = 4408
CLOSE_DELETED = 4410


class ConnectionManager:
    def __init__(self) -> None:
        self.local: dict[str, tuple[str, WebSocket]] = {}  # uid -> (sid, ws)
        self._listener: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()

    # ---- lifecycle ----
    async def start(self) -> None:
        self._listener = asyncio.create_task(self._listen(), name="ws-pubsub")

    async def stop(self) -> None:
        if self._listener:
            self._listener.cancel()

    async def _listen(self) -> None:
        while True:
            try:
                pubsub = redis.pubsub()
                await pubsub.subscribe(CHANNEL)
                async for item in pubsub.listen():
                    if item.get("type") == "message":
                        await self._handle_bus(json.loads(item["data"]))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pubsub listener crashed, restarting")
                await asyncio.sleep(1)

    async def _handle_bus(self, env: dict[str, Any]) -> None:
        uid, sid = env["uid"], env.get("sid")
        entry = self.local.get(uid)
        if not entry:
            return
        local_sid, ws = entry
        if env.get("control") == "replace":
            if local_sid != env["keep_sid"]:
                await self._close(uid, local_sid, CLOSE_REPLACED, "session_replaced")
            return
        if env.get("control") == "close":
            await self._close(uid, local_sid, env.get("code", CLOSE_BANNED), env.get("reason", "closed"))
            return
        if sid is None or sid == local_sid:
            await self._send_ws(ws, env["msg"])

    # ---- registry ----
    async def register(self, uid: str, ws: WebSocket) -> str:
        sid = uuid.uuid4().hex
        old = self.local.get(uid)
        self.local[uid] = (sid, ws)
        await redis.set(f"ws:sid:{uid}", sid, ex=24 * 3600)
        if old:  # duplicate session on this worker
            self._spawn(self._close_ws(old[1], CLOSE_REPLACED, "session_replaced"))
        await redis.publish(CHANNEL, json.dumps({"uid": uid, "control": "replace", "keep_sid": sid}))
        return sid

    def unregister(self, uid: str, sid: str) -> None:
        if self.local.get(uid, (None,))[0] == sid:
            del self.local[uid]

    async def is_current(self, uid: str, sid: str) -> bool:
        return await redis.get(f"ws:sid:{uid}") == sid

    # ---- sending ----
    async def send(self, uid: str, msg: dict[str, Any], sid: str | None = None) -> None:
        entry = self.local.get(uid)
        if entry and (sid is None or entry[0] == sid):
            await self._send_ws(entry[1], msg)
        else:
            await redis.publish(CHANNEL, json.dumps({"uid": uid, "sid": sid, "msg": msg}, default=str))

    async def force_disconnect(self, uid: Any, reason: str, code: int = CLOSE_BANNED) -> None:
        uid = str(uid)
        entry = self.local.get(uid)
        if entry:
            await self._close(uid, entry[0], code, reason)
        await redis.publish(CHANNEL, json.dumps({"uid": uid, "control": "close", "reason": reason, "code": code}))

    async def _close(self, uid: str, sid: str, code: int, reason: str) -> None:
        entry = self.local.get(uid)
        if entry and entry[0] == sid:
            ws = entry[1]

            async def _do() -> None:
                await self._send_ws(ws, {"type": "error", "code": reason, "message": reason.replace("_", " ")})
                await self._close_ws(ws, code, reason)

            # Closing waits for the client's close handshake; never block the caller (or the pub/sub loop) on it.
            self._spawn(_do())

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _send_ws(ws: WebSocket, msg: dict[str, Any]) -> None:
        try:
            if ws.application_state == WebSocketState.CONNECTED:
                await ws.send_text(json.dumps(msg, default=str))
        except Exception:
            pass  # socket is going away; its own loop handles cleanup

    @staticmethod
    async def _close_ws(ws: WebSocket, code: int, reason: str) -> None:
        try:
            if ws.application_state == WebSocketState.CONNECTED:
                await ws.close(code=code, reason=reason)
        except Exception:
            pass


manager = ConnectionManager()
