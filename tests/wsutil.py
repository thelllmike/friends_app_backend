"""Async WebSocket helpers for tests."""
import asyncio
import json

import websockets

from redis.asyncio import Redis

from app.core.config import settings
from tests.helpers import WS_BASE


async def connect(token: str, platform: str = "android"):
    ws = await websockets.connect(f"{WS_BASE}/ws?token={token}&platform={platform}")
    return ws


async def send(ws, **msg) -> None:
    await ws.send(json.dumps(msg))


async def expect(ws, wanted: str, timeout: float = 5) -> dict:
    async def _read():
        while True:
            msg = json.loads(await ws.recv())
            if msg["type"] == wanted:
                return msg
    return await asyncio.wait_for(_read(), timeout)


async def expect_none(ws, unwanted: str, timeout: float = 1.5) -> None:
    try:
        msg = await expect(ws, unwanted, timeout)
    except (asyncio.TimeoutError, websockets.ConnectionClosed):
        return
    raise AssertionError(f"unexpected {unwanted}: {msg}")


async def wait_queue_empty(timeout: float = 3) -> None:
    """Let disconnect cleanup from the previous test finish so queues don't leak between tests."""
    redis = redis_client()
    for _ in range(int(timeout / 0.05)):
        if await redis.zcard("mm:queue") == 0:
            return
        await asyncio.sleep(0.05)
    await redis.delete("mm:queue")


def redis_client() -> Redis:
    """A fresh client bound to the current test's event loop."""
    return Redis.from_url(settings.redis_url, decode_responses=True)
