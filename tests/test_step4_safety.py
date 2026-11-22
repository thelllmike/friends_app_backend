"""Step 4: reports, blocks, bans."""
import uuid

import pytest
import websockets

from tests.helpers import client, set_setting, signup
from tests.wsutil import connect, expect, expect_none, send, wait_queue_empty


@pytest.fixture(autouse=True)
async def _setup():
    await wait_queue_empty()
    set_setting("recent_match_minutes", 0)  # so only blocks prevent rematching here
    set_setting("report_threshold", 3)
    yield
    set_setting("recent_match_minutes", 10)


async def pair(ta: str, tb: str):
    wa, wb = await connect(ta), await connect(tb)
    await expect(wa, "connected"); await expect(wb, "connected")
    await send(wa, type="join_queue"); await expect(wa, "searching")
    await send(wb, type="join_queue")
    ma = await expect(wa, "matched"); await expect(wb, "matched")
    return wa, wb, ma["room_id"]


async def test_report_ends_call_and_is_private():
    with client() as c:
        a, b, x = signup(c, profile={}), signup(c, profile={}), signup(c, profile={})
        wa, wb, room = await pair(a["access_token"], b["access_token"])
        r = c.post("/reports", json={"room_id": room, "reason": "spam_or_scam", "details": "links"},
                   headers=a["headers"])
        assert r.status_code == 201 and r.json()["call_ended"] is True
        assert (await expect(wa, "call_ended"))["reason"] == "report"
        assert (await expect(wb, "partner_left"))["reason"] == "left"  # B isn't told it was a report
        # Same call reported twice -> idempotent
        assert c.post("/reports", json={"room_id": room, "reason": "other"}, headers=a["headers"]).json()["id"] \
            == r.json()["id"]
        # Someone who wasn't in the call can't report it
        assert c.post("/reports", json={"room_id": room, "reason": "other"}, headers=x["headers"]).status_code == 404
        assert c.post("/reports", json={"room_id": room, "reason": "bogus"}, headers=a["headers"]).status_code == 422
        assert len(c.get("/reports/mine", headers=a["headers"]).json()) == 1
    await wa.close(); await wb.close()


async def test_block_prevents_match_and_unblock_restores():
    with client() as c:
        a, b = signup(c, profile={"display_name": "Blocker"}), signup(c, profile={"display_name": "Blocked"})
        wa, wb, room = await pair(a["access_token"], b["access_token"])
        r = c.post("/blocks", json={"room_id": room}, headers=a["headers"]).json()
        assert r["call_ended"] and r["blocked_user_id"] == b["user"]["id"]
        await expect(wb, "partner_left")
        lst = c.get("/blocks", headers=a["headers"]).json()
        assert [x["display_name"] for x in lst] == ["Blocked"]

        # Neither direction can match now
        await send(wb, type="join_queue"); await expect(wb, "searching")
        await send(wa, type="join_queue"); await expect(wa, "searching")
        await expect_none(wa, "matched")
        await send(wa, type="leave_queue"); await send(wb, type="leave_queue")
        await expect(wa, "left_queue"); await expect(wb, "left_queue")

        assert c.delete(f"/blocks/{b['user']['id']}", headers=a["headers"]).status_code == 204
        assert c.get("/blocks", headers=a["headers"]).json() == []
        await send(wb, type="join_queue"); await expect(wb, "searching")
        await send(wa, type="join_queue")
        await expect(wa, "matched")
    await wa.close(); await wb.close()


async def test_auto_ban_after_threshold_distinct_reports():
    with client() as c:
        bad = signup(c, profile={"display_name": "Troll"})
        reporters = [signup(c, profile={}) for _ in range(3)]
        wbad = None
        for i, rep in enumerate(reporters):
            wr, wbad, room = await pair(rep["access_token"], bad["access_token"])
            c.post("/reports", json={"room_id": room, "reason": "harassment_or_bullying"}, headers=rep["headers"])
            await wr.close()
            if i < 2:
                await expect(wbad, "partner_left")
                await wbad.close()
        # third distinct report -> temp ban, live socket kicked
        with pytest.raises(websockets.ConnectionClosed) as e:
            await expect(wbad, "never")
        assert e.value.rcvd.code == 4403
        assert c.patch("/me", json={"age": 30}, headers=bad["headers"]).json()["error"]["code"] == "banned"
        me = c.get("/me", headers=bad["headers"])  # banned users can still view their profile
        assert me.status_code == 200
        ws = await connect(bad["access_token"])
        assert (await expect(ws, "error"))["code"] == "banned"
        # login is refused too
        r = c.post("/auth/refresh", json={"refresh_token": bad["refresh_token"]})
        assert r.json()["error"]["code"] == "banned"


async def test_block_validation():
    with client() as c:
        a = signup(c, profile={})
        assert c.post("/blocks", json={}, headers=a["headers"]).status_code == 422
        assert c.post("/blocks", json={"user_id": a["user"]["id"]}, headers=a["headers"]).status_code == 400
        assert c.post("/blocks", json={"user_id": str(uuid.uuid4())}, headers=a["headers"]).status_code == 404
        assert c.post("/blocks", json={"room_id": str(uuid.uuid4())}, headers=a["headers"]).status_code == 404
