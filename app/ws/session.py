"""One WebSocket session: protocol handling for matchmaking, signaling and in-call chat.

Client -> server                     Server -> client
  join_queue {filters}                 connected, searching, matched, no_users_found
  leave_queue                          offer / answer / ice_candidate (relayed)
  offer {sdp} / answer {sdp}           chat, chat_ack
  ice_candidate {candidate}            partner_left, call_ended, left_queue
  chat {text, client_msg_id}           error {code, message}, pong
  next / leave / ping
"""
import asyncio
import logging
import time
import uuid
from typing import Any

from fastapi import WebSocket

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.rate_limit import hit
from app.models.user import User
from app.schemas.common import COUNTRY_CODES
from app.schemas.user import photo_url
from app.services import app_settings, blocks
from app.services import matchmaking as mm
from app.ws.manager import CLOSE_RATE_LIMIT, manager

log = logging.getLogger("friends.ws")
ANYWHERE = {None, "", "anywhere", "any", "all"}
GENDERS = {"male", "female", "other"}
MAX_FRAME_BYTES = 64 * 1024  # SDPs are a few KB; anything larger is abuse


class WSError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code, self.message = code, message


class Session:
    def __init__(self, ws: WebSocket, uid: str, sid: str, platform: str, country: str | None = None) -> None:
        self.ws, self.uid, self.sid, self.platform, self.country = ws, uid, sid, platform, country
        self.filters: dict[str, Any] = {}
        self.search_task: asyncio.Task | None = None
        # token bucket for overall message flood protection
        self._tokens = float(settings.ws_max_messages_per_second)
        self._last = time.monotonic()
        self._violations = 0

    async def send(self, msg: dict[str, Any]) -> None:
        await manager.send(self.uid, msg, self.sid)

    async def error(self, code: str, message: str) -> None:
        await self.send({"type": "error", "code": code, "message": message})

    # ---- flood control ----
    def _allow(self) -> bool:
        now = time.monotonic()
        rate = settings.ws_max_messages_per_second
        self._tokens = min(rate, self._tokens + (now - self._last) * rate)
        self._last = now
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False

    async def handle_raw(self, raw: str) -> None:
        import json

        if len(raw) > MAX_FRAME_BYTES:
            return await self.error("message_too_large", "Message too large")
        if not self._allow():
            self._violations += 1
            if self._violations > 50:
                await self.ws.close(code=CLOSE_RATE_LIMIT, reason="rate_limited")
                return
            return await self.error("rate_limited", "Too many messages")
        try:
            msg = json.loads(raw)
            assert isinstance(msg, dict)
        except Exception:
            return await self.error("bad_message", "Invalid JSON")
        handler = HANDLERS.get(msg.get("type"))
        if not handler:
            return await self.error("unknown_type", f"Unknown message type: {msg.get('type')}")
        try:
            await handler(self, msg)
        except WSError as e:
            await self.error(e.code, e.message)

    # ---- matchmaking ----
    async def join_queue(self, msg: dict[str, Any]) -> None:
        raw_filters = msg.get("filters") if isinstance(msg.get("filters"), dict) else self.filters
        # Being in a call and asking to search == "next".
        await self.end_current_room("next")
        await mm.leave_queue(self.uid, self.sid)
        self._cancel_search()

        flags = await app_settings.get_all()
        if flags["maintenance_mode"]:
            raise WSError("maintenance", "We're doing maintenance, try again shortly")
        async with SessionLocal() as db:
            user = await db.get(User, uuid.UUID(self.uid))
            if not user:
                raise WSError("unauthorized", "Account not found")
            if user.is_banned():
                await manager.force_disconnect(self.uid, "banned")
                return
            if not user.profile_complete:
                raise WSError("profile_incomplete", "Complete your profile before searching")
            excluded = await blocks.excluded_ids(db, user.id)
            premium = user.is_premium()
            ticket_base = dict(uid=self.uid, sid=self.sid, platform=self.platform,
                               display_name=user.display_name, age=user.age, country=user.country,
                               gender=user.gender, photo_url=photo_url(user.photo_path),
                               interests=list(user.interests or []), premium=premium, excluded=sorted(excluded))

        want_country, want_gender, ignored = self._resolve_filters(raw_filters, premium, flags)
        self.filters = raw_filters or {}
        ticket = mm.Ticket(**ticket_base, want_country=want_country, want_gender=want_gender)
        await self.send({"type": "searching", "filters": {"country": want_country or "Anywhere",
                                                          "gender": want_gender or "Anywhere"},
                         "filters_ignored": ignored, "premium": premium,
                         "timeout_seconds": settings.search_timeout_seconds})
        match = await mm.join_and_match(ticket, priority=bool(flags["priority_matching_enabled"]))
        if match:
            await notify_match(match)
        else:
            self.search_task = asyncio.create_task(self._search_timeout())

    @staticmethod
    def _resolve_filters(f: dict[str, Any] | None, premium: bool, flags: dict) -> tuple[str | None, str | None, bool]:
        """Returns (country, gender, filters_ignored). Free users always match 'Anywhere'."""
        f = f or {}
        country = f.get("country")
        gender = f.get("gender")
        country = None if (country is None or str(country).lower() in ANYWHERE) else str(country).upper()
        gender = None if (gender is None or str(gender).lower() in ANYWHERE) else str(gender).lower()
        if country and country not in COUNTRY_CODES:
            raise WSError("invalid_filter", "Unknown country code")
        if gender and gender not in GENDERS:
            raise WSError("invalid_filter", "Gender must be male, female, other or Anywhere")
        requested = bool(country or gender)
        if not premium:
            return None, None, requested
        ignored = False
        if country and not flags["country_filter_enabled"]:
            country, ignored = None, True
        if gender and not flags["gender_filter_enabled"]:
            gender, ignored = None, True
        return country, gender, ignored

    async def _search_timeout(self) -> None:
        try:
            await asyncio.sleep(settings.search_timeout_seconds)
            if await mm.leave_queue(self.uid, self.sid):
                await self.send({"type": "no_users_found",
                                 "message": "No one is available right now. Try again or widen your filters."})
        except asyncio.CancelledError:
            pass

    def _cancel_search(self) -> None:
        if self.search_task and not self.search_task.done():
            self.search_task.cancel()
        self.search_task = None

    async def leave_queue(self, _: dict[str, Any]) -> None:
        self._cancel_search()
        await mm.leave_queue(self.uid, self.sid)
        await self.send({"type": "left_queue"})

    async def end_current_room(self, reason: str, notify_self: bool = True) -> bool:
        room = await mm.current_room(self.uid)
        if not room:
            return False
        await end_room_and_notify(room.room_id, reason, self.uid, notify_ender=notify_self)
        return True

    async def next(self, _: dict[str, Any]) -> None:
        await self.join_queue({"type": "join_queue", "filters": self.filters})

    async def leave(self, _: dict[str, Any]) -> None:
        self._cancel_search()
        await mm.leave_queue(self.uid, self.sid)
        if not await self.end_current_room("leave"):
            await self.send({"type": "left_queue"})

    # ---- signaling relay ----
    async def _room_or_fail(self, msg: dict[str, Any]) -> mm.RoomInfo:
        room = await mm.current_room(self.uid, self.sid)
        if not room:
            raise WSError("not_in_room", "You are not in a call")
        if msg.get("room_id") and msg["room_id"] != room.room_id:
            raise WSError("room_mismatch", "That call has ended")
        return room

    async def relay(self, msg: dict[str, Any]) -> None:
        room = await self._room_or_fail(msg)
        kind = msg["type"]
        payload_key = "candidate" if kind == "ice_candidate" else "sdp"
        if payload_key not in msg:
            raise WSError("bad_message", f"'{payload_key}' is required")
        partner = room.partner_of(self.uid)
        if kind == "answer":
            await mm.mark_connected(room.room_id)
        await manager.send(partner, {"type": kind, "room_id": room.room_id, payload_key: msg[payload_key]},
                           room.sids[partner])

    # ---- chat ----
    async def chat(self, msg: dict[str, Any]) -> None:
        if not await app_settings.get("chat_enabled"):
            raise WSError("chat_disabled", "Chat is disabled")
        room = await self._room_or_fail(msg)
        text = msg.get("text")
        if not isinstance(text, str) or not text.strip():
            raise WSError("bad_message", "Message is empty")
        text = text.strip()
        if len(text) > settings.chat_max_length:
            raise WSError("message_too_long", f"Max {settings.chat_max_length} characters")
        if not await hit(f"chat:{self.uid}", settings.chat_max_per_10s, 10):
            raise WSError("rate_limited", "You're sending messages too fast")
        mid, ts = uuid.uuid4().hex, int(time.time() * 1000)
        partner = room.partner_of(self.uid)
        await manager.send(partner, {"type": "chat", "room_id": room.room_id, "id": mid, "text": text, "ts": ts,
                                     "from": "partner"}, room.sids[partner])
        await self.send({"type": "chat_ack", "room_id": room.room_id, "id": mid, "ts": ts,
                         "client_msg_id": msg.get("client_msg_id")})

    async def ping(self, _: dict[str, Any]) -> None:
        from app.services import presence

        await presence.mark_online(uuid.UUID(self.uid), self.platform, self.country)
        await self.send({"type": "pong", "ts": int(time.time() * 1000)})

    # ---- teardown ----
    async def cleanup(self) -> None:
        self._cancel_search()
        await mm.leave_queue(self.uid, self.sid)
        room = await mm.current_room(self.uid, self.sid)
        if room:
            replaced = not await manager.is_current(self.uid, self.sid)
            await end_room_and_notify(room.room_id, "replaced" if replaced else "disconnect", self.uid,
                                      notify_ender=False)


async def notify_match(m: mm.Match) -> None:
    for me, other, role in ((m.caller, m.callee, "caller"), (m.callee, m.caller, "callee")):
        await manager.send(me.uid, {"type": "matched", "room_id": m.room_id, "role": role,
                                    "partner": other.public(), "shared_interests": m.shared}, me.sid)


# Reasons the partner is allowed to see; report/block are private.
_PUBLIC_REASON = {"disconnect": "disconnected", "replaced": "disconnected"}


async def end_room_and_notify(room_id: str, reason: str, ended_by: str | None, notify_ender: bool = True) -> None:
    res = await mm.end_room(room_id, reason, ended_by)
    if not res:
        return
    room, duration = res
    for uid in (room.caller, room.callee):
        if uid == ended_by:
            if notify_ender:
                await manager.send(uid, {"type": "call_ended", "room_id": room_id, "reason": reason,
                                         "duration_seconds": duration}, room.sids[uid])
        else:
            await manager.send(uid, {"type": "partner_left", "room_id": room_id,
                                     "reason": _PUBLIC_REASON.get(reason, "left"),
                                     "duration_seconds": duration}, room.sids[uid])


async def end_room_for_user(uid: str, reason: str) -> str | None:
    """Used by REST (report/block/ban): end whatever call `uid` is in. Returns room_id."""
    room = await mm.current_room(uid)
    if not room:
        return None
    await end_room_and_notify(room.room_id, reason, uid)
    return room.room_id


HANDLERS = {
    "join_queue": Session.join_queue,
    "leave_queue": Session.leave_queue,
    "next": Session.next,
    "leave": Session.leave,
    "offer": Session.relay,
    "answer": Session.relay,
    "ice_candidate": Session.relay,
    "chat": Session.chat,
    "ping": Session.ping,
}
