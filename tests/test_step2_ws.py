"""Step 2: WebSocket matchmaking + signaling."""

import pytest
import websockets

from tests.helpers import client, signup
from tests.wsutil import redis_client, connect, expect, expect_none, send, wait_queue_empty


@pytest.fixture(autouse=True)
async def _clean_queue():
    await wait_queue_empty()
    yield


def users(n: int = 2, **profile) -> list[dict]:
    with client() as c:
        return [signup(c, profile=profile) for _ in range(n)]


async def test_bad_token_rejected():
    ws = await connect("garbage")
    assert (await expect(ws, "error"))["code"] == "invalid_token"
    with pytest.raises(websockets.ConnectionClosed) as e:
        await ws.recv()
    assert e.value.rcvd.code == 4401


async def test_profile_incomplete_cannot_search():
    with client() as c:
        u = signup(c)  # no profile
    ws = await connect(u["access_token"])
    await expect(ws, "connected")
    await send(ws, type="join_queue")
    assert (await expect(ws, "error"))["code"] == "profile_incomplete"
    await ws.close()


async def test_free_user_filters_ignored_and_timeout():
    (u,) = users(1)
    ws = await connect(u["access_token"])
    await expect(ws, "connected")
    await send(ws, type="join_queue", filters={"country": "FR", "gender": "female"})
    s = await expect(ws, "searching")
    assert s["filters_ignored"] is True and s["filters"] == {"country": "Anywhere", "gender": "Anywhere"}
    # SEARCH_TIMEOUT_SECONDS=5 in the dev .env
    nf = await expect(ws, "no_users_found", timeout=8)
    assert nf["type"] == "no_users_found"
    await ws.close()


async def test_relay_requires_room_and_unknown_type():
    (u,) = users(1)
    ws = await connect(u["access_token"])
    await expect(ws, "connected")
    await send(ws, type="offer", sdp={"sdp": "x"})
    assert (await expect(ws, "error"))["code"] == "not_in_room"
    await send(ws, type="hack_the_planet")
    assert (await expect(ws, "error"))["code"] == "unknown_type"
    await ws.send("not json")
    assert (await expect(ws, "error"))["code"] == "bad_message"
    await send(ws, type="ping")
    await expect(ws, "pong")
    await ws.close()


async def test_disconnect_notifies_partner():
    a, b = users(2)
    wa, wb = await connect(a["access_token"]), await connect(b["access_token"], "ios")
    await expect(wa, "connected"); await expect(wb, "connected")
    await send(wa, type="join_queue"); await expect(wa, "searching")
    await send(wb, type="join_queue")
    m = await expect(wa, "matched"); await expect(wb, "matched")
    await wb.close()
    left = await expect(wa, "partner_left")
    assert left["room_id"] == m["room_id"] and left["reason"] == "disconnected"
    await wa.close()


async def test_leave_ends_call():
    a, b = users(2)
    wa, wb = await connect(a["access_token"]), await connect(b["access_token"])
    await expect(wa, "connected"); await expect(wb, "connected")
    await send(wa, type="join_queue"); await expect(wa, "searching")
    await send(wb, type="join_queue"); await expect(wb, "matched"); await expect(wa, "matched")
    await send(wb, type="leave")
    assert (await expect(wb, "call_ended"))["reason"] == "leave"
    assert (await expect(wa, "partner_left"))["reason"] == "left"
    # B not requeued after leave
    await send(wa, type="join_queue")
    await expect_none(wb, "matched")
    await wa.close(); await wb.close()


async def test_duplicate_session_replaces_old():
    a, b = users(2)
    old = await connect(a["access_token"])
    await expect(old, "connected")
    wb = await connect(b["access_token"])
    await expect(wb, "connected")
    await send(old, type="join_queue"); await expect(old, "searching")
    await send(wb, type="join_queue"); await expect(wb, "matched"); await expect(old, "matched")
    new = await connect(a["access_token"], "web")
    await expect(new, "connected")
    with pytest.raises(websockets.ConnectionClosed) as e:
        await expect(old, "never")
    assert e.value.rcvd.code == 4409
    # the call the old session was in is ended for the partner
    assert (await expect(wb, "partner_left"))["reason"] == "disconnected"
    # the new session works normally
    await send(new, type="ping"); await expect(new, "pong")
    await new.close(); await wb.close()


async def test_shared_interests_and_partner_info():
    with client() as c:
        a = signup(c, profile={"display_name": "Ana", "interests": ["Music", "Books", "Food"], "country": "BR"})
        b = signup(c, profile={"display_name": "Ben", "interests": ["Food", "Music"], "country": "DE"})
    wa, wb = await connect(a["access_token"]), await connect(b["access_token"])
    await expect(wa, "connected"); await expect(wb, "connected")
    await send(wa, type="join_queue"); await expect(wa, "searching")
    await send(wb, type="join_queue")
    ma, mb = await expect(wa, "matched"), await expect(wb, "matched")
    assert ma["shared_interests"] == ["Music", "Food"]
    assert ma["partner"]["display_name"] == "Ben" and ma["partner"]["country"] == "DE"
    assert mb["partner"]["age"] == 25 and "gender" not in mb["partner"]
    # B (the joiner) is the caller
    assert mb["role"] == "caller" and ma["role"] == "callee"
    await wa.close(); await wb.close()


async def test_chat_rate_limit():
    a, b = users(2)
    wa, wb = await connect(a["access_token"]), await connect(b["access_token"])
    await expect(wa, "connected"); await expect(wb, "connected")
    await send(wa, type="join_queue"); await expect(wa, "searching")
    await send(wb, type="join_queue"); await expect(wa, "matched"); await expect(wb, "matched")
    for i in range(12):
        await send(wa, type="chat", text=f"m{i}")
    assert (await expect(wa, "error"))["code"] == "rate_limited"
    await wa.close(); await wb.close()


async def test_online_count_reflects_connections():
    (u,) = users(1)
    ws = await connect(u["access_token"], "web")
    await expect(ws, "connected")
    with client() as c:
        assert c.get("/online-count").json()["online"] >= 1  # cached, may lag
    r = redis_client()
    assert await r.zscore("online:p:web", u["user"]["id"]) is not None
    await ws.close()
