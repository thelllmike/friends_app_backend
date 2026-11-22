#!/usr/bin/env python3
"""End-to-end smoke test: two fake users sign up, connect, match, exchange offer/answer/ICE, chat, then "next".

Requires a running server with OTP_DEV_ECHO=true (dev). Usage:
    python scripts/e2e_two_users.py [--api http://127.0.0.1:8000]
"""
import argparse
import asyncio
import json
import random
import sys

import httpx
import websockets


def step(msg: str) -> None:
    print(f"  ✓ {msg}")


async def signup(c: httpx.AsyncClient, name: str, country: str, gender: str, interests: list[str]) -> str:
    phone = "+1555" + "".join(random.choices("0123456789", k=7))
    r = await c.post("/auth/otp/request", json={"phone": phone})
    if r.status_code == 429:
        sys.exit(f"Login rate limit hit (LOGIN_RATE_PER_MINUTE); wait {r.json()['error']['details']['retry_after']}s")
    r.raise_for_status()
    code = r.json().get("dev_code")
    if not code:
        sys.exit("Server must run with OTP_DEV_ECHO=true for this script")
    r = await c.post("/auth/otp/verify", json={"phone": phone, "code": code, "confirm_18_plus": True,
                                               "accept_terms": True})
    r.raise_for_status()
    token = r.json()["access_token"]
    r = await c.patch("/me", headers={"Authorization": f"Bearer {token}"},
                      json={"display_name": name, "age": random.randint(18, 40), "country": country,
                            "gender": gender, "interests": interests})
    r.raise_for_status()
    return token


async def expect(ws, wanted: str, timeout: float = 10) -> dict:
    """Read messages until one of type `wanted` arrives (skips pongs etc.)."""
    async def _read():
        while True:
            msg = json.loads(await ws.recv())
            if msg["type"] == wanted:
                return msg
            if msg["type"] == "error":
                raise AssertionError(f"server error while waiting for {wanted}: {msg}")
    return await asyncio.wait_for(_read(), timeout)


async def main(api: str) -> None:
    ws_url = api.replace("http", "ws", 1) + "/ws"
    async with httpx.AsyncClient(base_url=api, timeout=10) as c:
        print("1. Sign up two users")
        ta = await signup(c, "Alice", "US", "female", ["Music", "Travel", "Anime"])
        tb = await signup(c, "Bob", "GB", "male", ["Music", "Gaming", "Anime"])
        step("Alice and Bob created with profiles")
        turn = (await c.get("/turn-credentials", headers={"Authorization": f"Bearer {ta}"}))
        if turn.status_code == 200:
            step(f"TURN credentials: {len(turn.json()['ice_servers'])} ice server entries")

    async with websockets.connect(f"{ws_url}?token={ta}&platform=ios") as a, \
            websockets.connect(f"{ws_url}?token={tb}&platform=android") as b:
        print("2. Connect WebSockets")
        await expect(a, "connected")
        await expect(b, "connected")
        step("both connected")

        print("3. Join queue and match")
        await a.send(json.dumps({"type": "join_queue", "filters": {"country": "Anywhere", "gender": "Anywhere"}}))
        await expect(a, "searching")
        await b.send(json.dumps({"type": "join_queue"}))
        ma, mb = await expect(a, "matched"), await expect(b, "matched")
        assert ma["room_id"] == mb["room_id"]
        assert {ma["role"], mb["role"]} == {"caller", "callee"}
        assert ma["shared_interests"] == ["Music", "Anime"], ma["shared_interests"]
        step(f"matched room={ma['room_id'][:8]} A={ma['role']} B={mb['role']} "
             f"partner={ma['partner']['display_name']} shared={ma['shared_interests']}")

        caller, callee = (a, b) if ma["role"] == "caller" else (b, a)
        print("4. Exchange offer / answer / ICE")
        await caller.send(json.dumps({"type": "offer", "sdp": {"type": "offer", "sdp": "v=0 fake-offer"}}))
        assert (await expect(callee, "offer"))["sdp"]["sdp"] == "v=0 fake-offer"
        await callee.send(json.dumps({"type": "answer", "sdp": {"type": "answer", "sdp": "v=0 fake-answer"}}))
        assert (await expect(caller, "answer"))["sdp"]["sdp"] == "v=0 fake-answer"
        cand = {"candidate": "candidate:1 1 udp 2122260223 10.0.0.1 5000 typ host", "sdpMid": "0",
                "sdpMLineIndex": 0}
        await caller.send(json.dumps({"type": "ice_candidate", "candidate": cand}))
        await callee.send(json.dumps({"type": "ice_candidate", "candidate": cand}))
        assert (await expect(callee, "ice_candidate"))["candidate"] == cand
        assert (await expect(caller, "ice_candidate"))["candidate"] == cand
        step("offer, answer and ICE candidates relayed both ways")

        print("5. In-call chat")
        await a.send(json.dumps({"type": "chat", "text": "Hi Bob! \U0001F44B", "client_msg_id": "c1"}))
        ack = await expect(a, "chat_ack")
        got = await expect(b, "chat")
        assert got["text"] == "Hi Bob! \U0001F44B" and ack["client_msg_id"] == "c1" and ack["id"] == got["id"]
        await b.send(json.dumps({"type": "chat", "text": "x" * 10_000}))
        err = await expect(b, "error")
        assert err["code"] == "message_too_long"
        step("chat relayed, ack received, over-long message rejected")

        await asyncio.sleep(1.1)  # let the call timer tick
        print("6. Next")
        await a.send(json.dumps({"type": "next"}))
        ended = await expect(a, "call_ended")
        left = await expect(b, "partner_left")
        await expect(a, "searching")
        assert left["room_id"] == ma["room_id"] and ended["duration_seconds"] >= 1
        step(f"A pressed next: A requeued, B got partner_left (call lasted {ended['duration_seconds']}s)")

        # B searches again: they were just matched, so they must NOT be paired again.
        await b.send(json.dumps({"type": "join_queue"}))
        await expect(b, "searching")
        try:
            m = await expect(b, "matched", timeout=2)
            raise AssertionError(f"recently matched pair was rematched: {m}")
        except asyncio.TimeoutError:
            step("recent-match rule respected: A and B were not paired again")
        await a.send(json.dumps({"type": "leave"}))
        await b.send(json.dumps({"type": "leave"}))
    print("\nAll good \U0001F389")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    asyncio.run(main(ap.parse_args().api))
