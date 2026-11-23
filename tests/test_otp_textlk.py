"""Text.lk SMS provider, against a fake Text.lk (httpx.MockTransport) — no real SMS is sent."""
import json

import httpx
import pytest

from app.core.config import settings
from app.core.errors import AppError
from app.services import otp


@pytest.fixture
def textlk(monkeypatch):
    """Route the provider's HTTP calls to a handler the test controls; returns the captured requests."""
    seen: list[httpx.Request] = []
    state = {"handler": lambda req: httpx.Response(200, json={"status": "success", "data": {}})}

    def transport_handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return state["handler"](req)

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(transport_handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(otp.httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(settings, "textlk_api_token", "test-token")
    monkeypatch.setattr(settings, "textlk_sender_id", "Friends")
    monkeypatch.setattr(settings, "textlk_api_base", "https://textlk.test/api/v3/")
    return seen, state


async def test_sends_code_in_textlk_format(textlk):
    seen, _ = textlk
    await otp.TextLKProvider().send("+94771234567", "123456")
    (req,) = seen
    assert str(req.url) == "https://textlk.test/api/v3/sms/send"
    assert req.headers["authorization"] == "Bearer test-token"
    body = json.loads(req.content)
    assert body["recipient"] == "94771234567" and body["sender_id"] == "Friends" and body["type"] == "plain"
    assert "123456" in body["message"]


async def test_error_inside_http_200_is_a_failure(textlk):
    _, state = textlk
    state["handler"] = lambda req: httpx.Response(200, json={"status": "error", "message": "Insufficient balance"})
    with pytest.raises(AppError) as e:
        await otp.TextLKProvider().send("+94771234567", "123456")
    assert e.value.status == 502 and e.value.code == "sms_failed"


async def test_network_failure_is_reported_cleanly(textlk):
    _, state = textlk

    def boom(req):
        raise httpx.ConnectError("down")

    state["handler"] = boom
    with pytest.raises(AppError) as e:
        await otp.TextLKProvider().send("+94771234567", "123456")
    assert e.value.code == "sms_failed"
