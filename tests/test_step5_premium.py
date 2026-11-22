"""Step 5: premium plans, purchase verification (fake mode), filters, priority, expiry, restore."""
import asyncio

import pytest

from tests.helpers import client, set_setting, signup
from tests.wsutil import connect, expect, expect_none, send, wait_queue_empty


@pytest.fixture(autouse=True)
async def _clean():
    await wait_queue_empty()
    set_setting("recent_match_minutes", 0)
    yield
    set_setting("recent_match_minutes", 10)


def buy(c, u, token="test_abc", product="friends_premium_month"):
    return c.post("/premium/verify/google", json={"product_id": product, "purchase_token": token},
                  headers=u["headers"])


def test_plans_from_db():
    with client() as c:
        body = c.get("/premium/plans").json()
    assert [p["code"] for p in body["plans"]] == ["week", "month", "year"]
    assert {f["id"] for f in body["features"]} == {"gender_filter", "priority_matching"}


def test_verify_restore_and_conflicts():
    with client() as c:
        u, other = signup(c, profile={}), signup(c, profile={})
        assert c.get("/premium/status", headers=u["headers"]).json()["is_premium"] is False
        tok = f"test_{u['user']['id']}"
        st = buy(c, u, tok).json()
        assert st["is_premium"] and st["platform"] == "google" and st["plan"] == "month"
        assert c.get("/me", headers=u["headers"]).json()["is_premium"]
        # Same token on another account: rejected on verify, moved on restore
        assert buy(c, other, tok).json()["error"]["code"] == "purchase_in_use"
        r = c.post("/premium/restore", json={"platform": "google",
                                             "purchases": [{"product_id": "friends_premium_month",
                                                            "purchase_token": tok}]}, headers=other["headers"])
        assert r.json()["is_premium"]
        assert c.get("/premium/status", headers=u["headers"]).json()["is_premium"] is False
        assert buy(c, u, "test_x", "unknown_sku").json()["error"]["code"] == "unknown_product"
        # Apple (fake mode)
        r = c.post("/premium/verify/apple", json={"transaction_id": f"test_ap_{u['user']['id']}",
                                                  "product_id": "friends.premium.year"}, headers=u["headers"])
        assert r.json()["plan"] == "year"


def test_expired_receipt_not_premium():
    with client() as c:
        u = signup(c, profile={})
        st = buy(c, u, f"test_expired_{u['user']['id']}").json()
        assert st["is_premium"] is False


async def test_premium_gender_filter():
    with client() as c:
        prem = signup(c, profile={"gender": "male"})
        buy(c, prem, f"test_{prem['user']['id']}")
        male = signup(c, profile={"gender": "male"})
        female = signup(c, profile={"gender": "female"})
    wp, wm, wf = (await connect(prem["access_token"]), await connect(male["access_token"]),
                  await connect(female["access_token"]))
    for w in (wp, wm, wf):
        await expect(w, "connected")
    await send(wp, type="join_queue", filters={"gender": "female", "country": "Anywhere"})
    s = await expect(wp, "searching")
    assert s["filters"]["gender"] == "female" and not s["filters_ignored"] and s["premium"]
    await send(wm, type="join_queue")
    await expect(wm, "searching")
    await expect_none(wp, "matched", 1)  # male doesn't satisfy the premium user's filter
    await send(wf, type="join_queue")
    m = await expect(wp, "matched")
    assert m["partner"]["id"] == female["user"]["id"]
    for w in (wp, wm, wf):
        await w.close()


async def test_premium_country_filter_and_priority():
    with client() as c:
        free_waiting = signup(c, profile={"country": "JP"})
        prem_waiting = signup(c, profile={"country": "JP"})
        buy(c, prem_waiting, f"test_{prem_waiting['user']['id']}")
        joiner = signup(c, profile={"country": "FR"})
    w_free, w_prem, w_join = (await connect(free_waiting["access_token"]), await connect(prem_waiting["access_token"]),
                              await connect(joiner["access_token"]))
    for w in (w_free, w_prem, w_join):
        await expect(w, "connected")
    # free user waits first (older), premium second, premium filters for FR
    await send(w_free, type="join_queue"); await expect(w_free, "searching")
    await asyncio.sleep(0.05)
    await send(w_prem, type="join_queue", filters={"country": "fr"}); await expect(w_prem, "searching")
    await send(w_join, type="join_queue")
    m = await expect(w_join, "matched")
    assert m["partner"]["id"] == prem_waiting["user"]["id"], "premium should be matched first"
    for w in (w_free, w_prem, w_join):
        await w.close()


async def test_filters_respect_feature_flag():
    with client() as c:
        prem = signup(c, profile={})
        buy(c, prem, f"test_{prem['user']['id']}")
    set_setting("gender_filter_enabled", False)
    try:
        w = await connect(prem["access_token"])
        await expect(w, "connected")
        await send(w, type="join_queue", filters={"gender": "female"})
        s = await expect(w, "searching")
        assert s["filters"]["gender"] == "Anywhere" and s["filters_ignored"]
        await w.close()
    finally:
        set_setting("gender_filter_enabled", True)


async def test_background_expiry():
    """A sub whose expiry passes gets flipped to expired by the scheduler (runs every 60s; run it directly)."""
    from datetime import timedelta

    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import settings
    from app.models.base import utcnow
    from app.models.billing import Subscription
    from app.models.user import User
    from app.services import billing

    with client() as c:
        u = signup(c, profile={})
        buy(c, u, f"test_{u['user']['id']}")
    eng = create_async_engine(settings.database_url, poolclass=NullPool)
    async with AsyncSession(eng) as db:
        past = utcnow() - timedelta(minutes=1)
        await db.execute(update(Subscription).where(Subscription.external_id == f"test_{u['user']['id']}")
                         .values(expires_at=past))
        await db.execute(update(User).where(User.id == u["user"]["id"]).values(premium_until=past))
        await db.commit()
        assert await billing.expire_due(db) >= 1
    await eng.dispose()
    with client() as c:
        assert c.get("/premium/status", headers=u["headers"]).json()["is_premium"] is False


async def test_free_user_country_filter_is_honoured():
    """Country filter is free: a non-premium user asking for BR skips a waiting JP user and matches the BR one."""
    with client() as c:
        jp = signup(c, profile={"country": "JP"})
        br = signup(c, profile={"country": "BR"})
        free = signup(c, profile={"country": "LK"})
    w_jp, w_br, w_free = (await connect(jp["access_token"]), await connect(br["access_token"]),
                          await connect(free["access_token"]))
    for w in (w_jp, w_br, w_free):
        await expect(w, "connected")
    # JP (free) only wants JP, so JP and BR stay unmatched in the queue
    await send(w_jp, type="join_queue", filters={"country": "JP"}); await expect(w_jp, "searching")
    await asyncio.sleep(0.05)
    await send(w_br, type="join_queue"); await expect(w_br, "searching")
    await expect_none(w_br, "matched", timeout=0.5)
    await send(w_free, type="join_queue", filters={"country": "br"})
    s = await expect(w_free, "searching")
    assert s["filters"]["country"] == "BR" and not s["premium"] and not s["filters_ignored"]
    m = await expect(w_free, "matched")
    assert m["partner"]["id"] == br["user"]["id"]
    await expect_none(w_jp, "matched", timeout=0.5)
    for w in (w_jp, w_br, w_free):
        await w.close()
