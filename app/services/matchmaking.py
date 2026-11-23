"""Redis matchmaking queue and call rooms.

mm:queue              ZSET uid -> score (join time; premium gets a large bonus => scanned first)
mm:ticket:{uid}       HASH  waiting user's snapshot (sid, profile, wanted filters, excluded ids)
room:{room_id}        HASH  caller, callee, sid:{uid}, matched_at, connected_at, platform:{uid}
user_room:{uid}       STRING room_id
recent:{a}:{b}        STRING (TTL) pair matched recently -> don't rematch

Matching and leaving are single Lua scripts, so Redis runs each atomically: a waiting user can never be
handed to two rooms, and there is no global lock to queue behind (load test: a lock held across the scan
and a DB write stalled matching at ~1.9k users on one CPU). The call's database record is written after.
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
    matched_at: datetime | None = None


async def join_and_match(t: Ticket, priority: bool) -> Match | None:
    """Enqueue `t` and try to pair it immediately with the best waiting candidate."""
    match = await _join_atomic(t, priority)
    if match:
        await _record_room(match)
    return match


# KEYS: none (keys are derived; single-node Redis). ARGV: uid, score, scan_limit, country, gender,
# want_country, want_gender, excluded(json list), then the ticket's hash as field/value pairs.
# Ticket hash values are JSON-encoded. Returns the claimed partner's ticket (flat field/value list), or
# false after enqueuing the joiner.
_JOIN_LUA = """
local uid, score, limit = ARGV[1], ARGV[2], tonumber(ARGV[3])
local country, gender, want_country, want_gender = ARGV[4], ARGV[5], ARGV[6], ARGV[7]
local mine_excl = {}
for _, x in ipairs(cjson.decode(ARGV[8])) do mine_excl[x] = true end
local function val(v) if v == nil or v == false then return nil end local d = cjson.decode(v)
  if d == cjson.null then return nil end return d end
local candidates = redis.call('ZRANGE', 'mm:queue', 0, limit - 1)
for _, c in ipairs(candidates) do
  if c ~= uid and not mine_excl[c] then
    local tk = 'mm:ticket:' .. c
    local f = redis.call('HMGET', tk, 'country', 'gender', 'want_country', 'want_gender', 'excluded')
    if not f[1] then
      redis.call('ZREM', 'mm:queue', c)  -- orphan entry
    else
      local pair = (uid < c) and ('recent:' .. uid .. ':' .. c) or ('recent:' .. c .. ':' .. uid)
      local c_country, c_gender, c_wc, c_wg = val(f[1]), val(f[2]), val(f[3]), val(f[4])
      local ok = (want_country == '' or want_country == c_country)
        and (want_gender == '' or want_gender == c_gender)
        and (c_wc == nil or c_wc == country) and (c_wg == nil or c_wg == gender)
        and redis.call('EXISTS', pair) == 0
      if ok then
        for _, x in ipairs(cjson.decode(f[5])) do if x == uid then ok = false break end end
      end
      if ok then
        local other = redis.call('HGETALL', tk)
        redis.call('ZREM', 'mm:queue', c, uid)
        redis.call('DEL', tk, 'mm:ticket:' .. uid)
        return other
      end
    end
  end
end
local mine = 'mm:ticket:' .. uid
redis.call('DEL', mine)
redis.call('HSET', mine, unpack(ARGV, 9))
redis.call('EXPIRE', mine, 600)
redis.call('ZADD', 'mm:queue', score, uid)
return false
"""

# ARGV: uid, expected sid (JSON-encoded) or '' for any. Returns 1 if the user was removed from the queue.
_LEAVE_LUA = """
local tk = 'mm:ticket:' .. ARGV[1]
local sid = redis.call('HGET', tk, 'sid')
if not sid then return 0 end
if ARGV[2] ~= '' and sid ~= ARGV[2] then return 0 end
redis.call('ZREM', 'mm:queue', ARGV[1])
redis.call('DEL', tk)
return 1
"""
_join_script = redis.register_script(_JOIN_LUA)
_leave_script = redis.register_script(_LEAVE_LUA)


async def _join_atomic(t: Ticket, priority: bool) -> Match | None:
    score = time.time() - (PREMIUM_BONUS if (t.premium and priority) else 0)
    flat: list[str] = []
    for k, v in t.to_redis().items():
        flat += [k, v]
    res = await _join_script(args=[t.uid, score, settings.queue_scan_limit, t.country or "", t.gender or "",
                                   t.want_country or "", t.want_gender or "", json.dumps(t.excluded), *flat])
    if not res:
        return None
    other = Ticket.from_redis(dict(zip(res[::2], res[1::2])))
    return await _create_room(caller=t, callee=other)


async def leave_queue(uid: str, sid: str | None = None) -> bool:
    """Remove a waiting user (only if the ticket belongs to `sid`, when given). True if removed."""
    return bool(await _leave_script(args=[uid, json.dumps(sid) if sid is not None else ""]))


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
    m = Match(room_id, caller, callee, shared_interests(caller, callee))
    m.matched_at = now
    return m


async def _record_room(m: Match) -> None:
    """Persist the call for history/stats (outside the queue lock)."""
    async with SessionLocal() as db:
        db.add(CallRoom(id=uuid.UUID(m.room_id), caller_id=uuid.UUID(m.caller.uid), callee_id=uuid.UUID(m.callee.uid),
                        matched_at=m.matched_at, caller_platform=m.caller.platform, callee_platform=m.callee.platform))
        await db.commit()
    log.info("matched room=%s caller=%s callee=%s", m.room_id, m.caller.uid, m.callee.uid)


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
