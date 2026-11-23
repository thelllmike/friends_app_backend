"""Step 6: admin dashboard API. Test admins are created with `python -m app.cli create-admin`."""

from tests.helpers import client, set_setting, signup
from tests.wsutil import connect, expect, send, wait_queue_empty

ROOT = ("root@friends.test", "super-secret-pass-1")
MOD = ("mod@friends.test", "moderator-pass-123")
ANALYST = ("analyst@friends.test", "analyst-pass-1234")


def admin_headers(c, creds) -> dict:
    r = c.post("/admin/auth/login", json={"email": creds[0], "password": creds[1]})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_admin_login_and_roles():
    with client() as c:
        assert c.post("/admin/auth/login", json={"email": ROOT[0], "password": "nope"}).status_code == 401
        root, mod, an = admin_headers(c, ROOT), admin_headers(c, MOD), admin_headers(c, ANALYST)
        u = signup(c)
        # user tokens can't access admin
        assert c.get("/admin/overview", headers=u["headers"]).status_code == 401
        assert c.get("/admin/overview", headers=an).status_code == 200
        assert c.get("/admin/users", headers=an).status_code == 403
        assert c.get("/admin/users", headers=mod).status_code == 200
        assert c.put("/admin/settings", json={"values": {}}, headers=mod).status_code == 403
        assert c.get("/admin/admins", headers=mod).status_code == 403
        assert c.get("/admin/admins", headers=root).status_code == 200
        assert "admins:manage" in c.get("/admin/me", headers=root).json()["permissions"]


async def test_overview_and_stats_after_a_call():
    await wait_queue_empty()
    set_setting("recent_match_minutes", 0)
    with client() as c:
        a, b = signup(c, profile={"country": "IN"}), signup(c, profile={"country": "IN"})
        wa, wb = await connect(a["access_token"], "ios"), await connect(b["access_token"], "web")
        await expect(wa, "connected"); await expect(wb, "connected")
        await send(wa, type="join_queue"); await expect(wa, "searching")
        await send(wb, type="join_queue"); m = await expect(wa, "matched"); await expect(wb, "matched")
        caller = wa if m["role"] == "caller" else wb
        callee = wb if caller is wa else wa
        await send(callee, type="answer", sdp={"type": "answer", "sdp": "x"})
        await expect(caller, "answer")
        root = admin_headers(c, ROOT)
        ov = c.get("/admin/overview", headers=root).json()
        assert ov["online_now"]["value"] >= 2 and ov["web_users_online"]["value"] >= 1
        assert ov["online_by_platform"]["ios"]["value"] >= 1
        assert ov["calls_today"]["value"] >= 1
        assert set(ov["calls_today"]) == {"value", "last_week", "change_pct"}
        await send(wa, type="leave"); await expect(wa, "call_ended")
        ov = c.get("/admin/overview", headers=root).json()
        assert ov["avg_call_seconds"]["value"] is not None
        for days in (14, 30, 90):
            s = c.get(f"/admin/stats/calls?days={days}", headers=root).json()["series"]
            assert len(s) == days and s[-1]["calls"] >= 1
        assert c.get("/admin/stats/calls?days=7", headers=root).status_code == 422
        top = c.get("/admin/stats/top-countries", headers=root).json()
        assert any(x["country"] == "IN" for x in top)
        countries = {x["country"]: x for x in c.get("/admin/countries", headers=root).json()}
        assert countries["IN"]["online_now"] >= 2
        assert c.get("/admin/stats/online", headers=root).json()["total"] >= 2
    await wa.close(); await wb.close()
    set_setting("recent_match_minutes", 10)


async def test_users_search_ban_unban_premium():
    with client() as c:
        import uuid as _u
        name = "Zed" + _u.uuid4().hex[:8]
        u = signup(c, profile={"display_name": name})
        mod, root = admin_headers(c, MOD), admin_headers(c, ROOT)
        res = c.get(f"/admin/users?q={name.lower()}", headers=mod).json()
        assert res["total"] == 1 and res["items"][0]["id"] == u["user"]["id"]
        assert c.get(f"/admin/users?q={u['user']['id']}", headers=mod).json()["total"] == 1

        ws = await connect(u["access_token"])
        await expect(ws, "connected")
        r = c.post(f"/admin/users/{u['user']['id']}/ban", json={"reason": "abuse"}, headers=mod).json()
        assert r["permanently_banned"] and r["is_banned"]
        assert (await expect(ws, "error"))["code"] == "banned"
        assert c.get(f"/admin/users?banned=true&q={name}", headers=mod).json()["total"] == 1
        assert c.patch("/me", json={"age": 33}, headers=u["headers"]).status_code == 403
        c.post(f"/admin/users/{u['user']['id']}/unban", headers=mod)
        assert c.patch("/me", json={"age": 33}, headers=u["headers"]).status_code == 200
        detail = c.get(f"/admin/users/{u['user']['id']}", headers=mod).json()
        assert detail["bans"][0]["lifted_at"] is not None

        assert c.post(f"/admin/users/{u['user']['id']}/premium", json={"days": 7}, headers=mod).status_code == 403
        r = c.post(f"/admin/users/{u['user']['id']}/premium", json={"days": 7, "plan_code": "week"}, headers=root)
        assert r.json()["is_premium"]
        assert c.get("/premium/status", headers=u["headers"]).json()["platform"] == "manual"
        r = c.delete(f"/admin/users/{u['user']['id']}/premium", headers=root).json()
        assert r["is_premium"] is False and r["revoked_subscriptions"] == 1


async def test_reports_queue_review():
    await wait_queue_empty()
    set_setting("recent_match_minutes", 0)
    set_setting("report_threshold", 100)
    with client() as c:
        a, b = signup(c, profile={"display_name": "Reporter"}), signup(c, profile={"display_name": "Reported"})
        wa, wb = await connect(a["access_token"]), await connect(b["access_token"])
        await expect(wa, "connected"); await expect(wb, "connected")
        await send(wa, type="join_queue"); await expect(wa, "searching")
        await send(wb, type="join_queue"); m = await expect(wa, "matched"); await expect(wb, "matched")
        rep = c.post("/reports", json={"room_id": m["room_id"], "reason": "underage"}, headers=a["headers"]).json()
        mod = admin_headers(c, MOD)
        q = c.get("/admin/reports?limit=200", headers=mod).json()
        item = next(x for x in q["items"] if x["id"] == rep["id"])
        assert item["reported"]["display_name"] == "Reported" and item["room_id"] == m["room_id"]
        d = c.get(f"/admin/reports/{rep['id']}", headers=mod).json()
        assert d["room"]["id"] == m["room_id"] and d["room"]["end_reason"] == "report"
        r = c.post(f"/admin/reports/{rep['id']}/review", json={"action": "ban", "ban_hours": 2, "note": "minor"},
                   headers=mod).json()
        assert r["status"] == "actioned"
        detail = c.get(f"/admin/users/{b['user']['id']}", headers=mod).json()
        assert detail["is_banned"] and not detail["permanently_banned"]
        assert all(x["id"] != rep["id"] for x in c.get("/admin/reports", headers=mod).json()["items"])
    await wa.close(); await wb.close()
    set_setting("recent_match_minutes", 10)
    set_setting("report_threshold", 3)


def test_settings_and_plan_prices():
    with client() as c:
        root = admin_headers(c, ROOT)
        assert c.put("/admin/settings", json={"values": {"nope": 1}}, headers=root).status_code == 422
        assert c.put("/admin/settings", json={"values": {"report_threshold": "5"}}, headers=root).status_code == 422
        assert c.put("/admin/settings", json={"values": {"chat_enabled": 1}}, headers=root).status_code == 422
        s = c.put("/admin/settings", json={"values": {"report_threshold": 4}}, headers=root).json()
        assert s["report_threshold"] == 4
        c.put("/admin/settings", json={"values": {"report_threshold": 3}}, headers=root)
        p = c.patch("/admin/plans/month", json={"price_cents": 1499, "currency": "eur"}, headers=root).json()
        assert p["price_cents"] == 1499 and p["currency"] == "EUR"
        month = next(x for x in c.get("/premium/plans").json()["plans"] if x["code"] == "month")
        assert month["price_cents"] == 1499
        c.patch("/admin/plans/month", json={"price_cents": 1299, "currency": "USD"}, headers=root)


async def test_recent_calls_and_online_history():
    await wait_queue_empty()
    set_setting("recent_match_minutes", 0)
    with client() as c:
        a, b = signup(c, profile={}), signup(c, profile={})
        wa, wb = await connect(a["access_token"], "android"), await connect(b["access_token"], "web")
        await expect(wa, "connected"); await expect(wb, "connected")
        await send(wa, type="join_queue"); await expect(wa, "searching")
        await send(wb, type="join_queue"); await expect(wa, "matched"); await expect(wb, "matched")
        await send(wa, type="leave"); await expect(wa, "call_ended")
        root, an = admin_headers(c, ROOT), admin_headers(c, ANALYST)
        uid = a["user"]["id"]
        r = c.get("/admin/calls", params={"user_id": uid}, headers=root).json()
        assert r["total"] == 1
        call = r["items"][0]
        assert {call["caller"]["id"], call["callee"]["id"]} == {uid, b["user"]["id"]}
        assert call["end_reason"] == "leave" and call["ended_at"]
        assert c.get("/admin/calls", headers=root).json()["total"] >= 1
        assert c.get("/admin/calls", headers=an).status_code == 403  # needs users:read
        h = c.get("/admin/stats/online-history", params={"hours": 24, "points": 24}, headers=an)
        assert h.status_code == 200 and isinstance(h.json()["series"], list)
        assert c.get("/admin/stats/online-history", params={"hours": 0}, headers=an).status_code == 422


def test_dashboard_is_served_with_csp():
    with client() as c:
        r = c.get("/dashboard/")
        assert r.status_code == 200 and "Friends Admin" in r.text
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert c.get("/dashboard/js/app.js").status_code == 200
        assert c.get("/dashboard/.git/config").status_code == 404
        assert c.get("/dashboard/js/../.env").status_code == 404


def test_health_answers_get_and_head():
    """The apps' offline check probes /health; HEAD must not be a 405 (that showed 'No connection')."""
    with client() as c:
        assert c.get("/health").status_code == 200
        assert c.head("/health").status_code == 200
