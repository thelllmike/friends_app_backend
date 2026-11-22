"""Redis matchmaking queue and call rooms.

mm:queue              ZSET uid -> score (join time; premium gets a large bonus => scanned first)
mm:ticket:{uid}       HASH  waiting user's snapshot (sid, profile, wanted filters, excluded ids)
room:{room_id}        HASH  caller, callee, sid:{uid}, matched_at, connected_at, platform:{uid}
user_room:{uid}       STRING room_id
recent:{a}:{b}        STRING (TTL) pair matched recently -> don't rematch

Every queue mutation (join / match / leave) runs under the `mm:lock` Redis lock so concurrent workers
can never hand the same user to two rooms.
"""
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.redis import redis
from app.models.call import CallRoom
from app.schemas.common import INTERESTS

log = logging.getLogger("friends.mm")
PREMIUM_BONUS = 1_000_000_000  # seconds; premium tickets always sort before free ones
ROOM_TTL = 12 * 3600
LOCK = "mm:lock"
# Delete KEYS[1] only if it still equals ARGV[1] (don't clobber a newer room pointer).
_DEL_IF_EQ = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


@dataclass
class Ticket:
    uid: str
    sid: str
    platform: str
    display_name: str
    age: int
    country: str
    gender: str
    photo_url: str | None
    interests: list[str]
    want_country: str | None
    want_gender: str | None
    premium: bool
    excluded: list[str]

    def to_redis(self) -> dict[str, str]:
        d = self.__dict__.copy()
        return {k: json.dumps(v) for k, v in d.items()}

    @classmethod
    def from_redis(cls, h: dict[str, str]) -> "Ticket":
        return cls(**{k: json.loads(v) for k, v in h.items() if k in cls.__dataclass_fields__})

    def public(self) -> dict:
        return {"id": self.uid, "display_name": self.display_name, "age": self.age, "country": self.country,
                "photo_url": self.photo_url}


def _lock():
    return redis.lock(LOCK, timeout=5, blocking_timeout=5)


def _pair_key(a: str, b: str) -> str:
    x, y = sorted((a, b))
    return f"recent:{x}:{y}"


def _wants(a: Ticket, b: Ticket) -> bool:
    return (a.want_country is None or a.want_country == b.country) and (
        a.want_gender is None or a.want_gender == b.gender)


def compatible(a: Ticket, b: Ticket) -> bool:
    return (a.uid != b.uid and _wants(a, b) and _wants(b, a)
            and b.uid not in a.excluded and a.uid not in b.excluded)


def shared_interests(a: Ticket, b: Ticket) -> list[str]:
    common = set(a.interests) & set(b.interests)
    return [i for i in INTERESTS if i in common]


@dataclass
class Match:
    room_id: str
    caller: Ticket
    callee: Ticket
    shared: list[str]


async def join_and_match(t: Ticket, priority: bool) -> Match | None:
    """Enqueue `t` and try to pair it immediately with the best waiting candidate."""
    async with _lock():
        score = time.time() - (PREMIUM_BONUS if (t.premium and priority) else 0)
        candidates = [c for c in await redis.zrange("mm:queue", 0, settings.queue_scan_limit - 1) if c != t.uid]
        if candidates:
            async with redis.pipeline(transaction=False) as p:
                for c in candidates:
                    p.hgetall(f"mm:ticket:{c}")
                    p.exists(_pair_key(t.uid, c))
                res = await p.execute()
            for i, c in enumerate(candidates):
                h, recent = res[2 * i], res[2 * i + 1]
                if not h:
                    await redis.zrem("mm:queue", c)  # orphan entry
                    continue
                other = Ticket.from_redis(h)
                if recent or not compatible(t, other):
                    continue
                # Pair found: remove the waiting user, never enqueue the joiner.
                await redis.zrem("mm:queue", c)
                await redis.delete(f"mm:ticket:{c}", f"mm:ticket:{t.uid}")
                await redis.zrem("mm:queue", t.uid)
                return await _create_room(caller=t, callee=other)
        # No partner yet: wait in the queue.
        async with redis.pipeline(transaction=True) as p:
            p.delete(f"mm:ticket:{t.uid}")
            p.hset(f"mm:ticket:{t.uid}", mapping=t.to_redis())
            p.expire(f"mm:ticket:{t.uid}", 600)
            p.zadd("mm:queue", {t.uid: score})
            await p.execute()
        return None


async def leave_queue(uid: str, sid: str | None = None) -> bool:
    """Remove a waiting user (only if the ticket belongs to `sid`, when given). True if removed."""
    async with _lock():
        h = await redis.hgetall(f"mm:ticket:{uid}")
        if not h:
            return False
        if sid is not None and json.loads(h.get("sid", "null")) != sid:
            return False
        await redis.zrem("mm:queue", uid)
        await redis.delete(f"mm:ticket:{uid}")
        return True


async def is_waiting(uid: str, sid: str) -> bool:
    h = await redis.hget(f"mm:ticket:{uid}", "sid")
    return h is not None and json.loads(h) == sid


async def _create_room(caller: Ticket, callee: Ticket) -> Match:
    room_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    async with redis.pipeline(transaction=True) as p:
        p.hset(f"room:{room_id}", mapping={
            "caller": caller.uid, "callee": callee.uid,
            f"sid:{caller.uid}": caller.sid, f"sid:{callee.uid}": callee.sid,
            "matched_at": now.timestamp(), "connected_at": "",
        })
        p.expire(f"room:{room_id}", ROOM_TTL)
        p.set(f"user_room:{caller.uid}", room_id, ex=ROOM_TTL)
        p.set(f"user_room:{callee.uid}", room_id, ex=ROOM_TTL)
        await p.execute()
    async with SessionLocal() as db:
        db.add(CallRoom(id=uuid.UUID(room_id), caller_id=uuid.UUID(caller.uid), callee_id=uuid.UUID(callee.uid),
                        matched_at=now, caller_platform=caller.platform, callee_platform=callee.platform))
        await db.commit()
    log.info("matched room=%s caller=%s callee=%s", room_id, caller.uid, callee.uid)
    return Match(room_id, caller, callee, shared_interests(caller, callee))


@dataclass
class RoomInfo:
    room_id: str
    caller: str
    callee: str
    sids: dict[str, str]
    matched_at: float
    connected_at: float | None

    def partner_of(self, uid: str) -> str:
        return self.callee if uid == self.caller else self.caller


async def get_room(room_id: str) -> RoomInfo | None:
    h = await redis.hgetall(f"room:{room_id}")
    if not h:
        return None
    return RoomInfo(room_id, h["caller"], h["callee"],
                    {h["caller"]: h[f"sid:{h['caller']}"], h["callee"]: h[f"sid:{h['callee']}"]},
                    float(h["matched_at"]), float(h["connected_at"]) if h.get("connected_at") else None)


async def current_room(uid: str, sid: str | None = None) -> RoomInfo | None:
    """The room `uid` is in (and, if `sid` is given, only if this session owns that seat)."""
    room_id = await redis.get(f"user_room:{uid}")
    if not room_id:
        return None
    room = await get_room(room_id)
    if not room or uid not in room.sids or (sid is not None and room.sids[uid] != sid):
        return None
    return room


async def mark_connected(room_id: str) -> None:
    """Called when the first SDP answer is relayed: the call timer starts here."""
    if await redis.hget(f"room:{room_id}", "connected_at") == "":
        await redis.hset(f"room:{room_id}", "connected_at", time.time())


async def end_room(room_id: str, reason: str, ended_by: str | None) -> tuple[RoomInfo, int] | None:
    """End a room exactly once. Returns (room, duration_seconds) or None if it was already ended."""
    room = await get_room(room_id)
    if not room:
        return None
    if not await redis.delete(f"room:{room_id}"):
        return None  # someone else ended it concurrently
    recent_min = 10
    try:
        from app.services import app_settings
        recent_min = int(await app_settings.get("recent_match_minutes"))
    except Exception:  # settings lookup must never block ending a call
        pass
    async with redis.pipeline(transaction=False) as p:
        for u in (room.caller, room.callee):
            p.eval(_DEL_IF_EQ, 1, f"user_room:{u}", room_id)
        if recent_min > 0:
            p.set(_pair_key(room.caller, room.callee), 1, ex=recent_min * 60)
        await p.execute()
    now = time.time()
    duration = int(now - room.connected_at) if room.connected_at else 0
    async with SessionLocal() as db:
        row = await db.get(CallRoom, uuid.UUID(room_id))
        if row:
            row.ended_at = datetime.fromtimestamp(now, timezone.utc)
            if room.connected_at:
                row.connected_at = datetime.fromtimestamp(room.connected_at, timezone.utc)
            row.duration_seconds = duration
            row.end_reason = reason
            row.ended_by = uuid.UUID(ended_by) if ended_by else None
            await db.commit()
    log.info("room ended room=%s reason=%s duration=%ss", room_id, reason, duration)
    return room, duration
