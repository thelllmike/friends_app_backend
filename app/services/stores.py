"""Server-side receipt verification against Google Play and the App Store (no third-party SDKs).

Google Play: Android Publisher API purchases.subscriptionsv2.get with a service-account OAuth token.
App Store:   App Store Server API /inApps/v1/subscriptions/{originalTransactionId} with an ES256 JWT.
"""
import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import jwt

from app.core.config import settings
from app.core.errors import AppError

log = logging.getLogger("friends.stores")


@dataclass
class StorePurchase:
    platform: str  # google | apple
    external_id: str  # Google: purchase token; Apple: originalTransactionId
    product_id: str
    status: str  # active | expired | revoked | cancelled
    expires_at: datetime
    auto_renew: bool
    raw: dict


def _ts_ms(v: str | int) -> datetime:
    return datetime.fromtimestamp(int(v) / 1000, timezone.utc)


def fake_mode_purchase(platform: str, token: str, product_id: str, duration_days: int) -> StorePurchase | None:
    """Dev-only: tokens like 'test_<anything>' are accepted as valid without calling the stores."""
    if not settings.billing_fake_mode or settings.is_production or not token.startswith("test_"):
        return None
    expired = token.startswith("test_expired")
    exp = datetime.now(timezone.utc) + (timedelta(seconds=-1) if expired else timedelta(days=duration_days))
    return StorePurchase(platform, token, product_id, "expired" if expired else "active", exp, not expired,
                         {"fake": True})


# ---- Google Play ---------------------------------------------------------------------------------

_google_token: dict[str, float | str] = {"value": "", "exp": 0.0}


async def _google_access_token() -> str:
    if _google_token["value"] and float(_google_token["exp"]) > time.time() + 60:
        return str(_google_token["value"])
    if not settings.google_service_account_file:
        raise AppError(503, "billing_not_configured", "Google Play verification is not configured")
    sa = json.loads(await asyncio.to_thread(lambda: open(settings.google_service_account_file).read()))
    now = int(time.time())
    assertion = jwt.encode({"iss": sa["client_email"], "scope": "https://www.googleapis.com/auth/androidpublisher",
                            "aud": sa["token_uri"], "iat": now, "exp": now + 3600}, sa["private_key"],
                           algorithm="RS256")
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(sa["token_uri"], data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                                "assertion": assertion})
    r.raise_for_status()
    body = r.json()
    _google_token.update(value=body["access_token"], exp=time.time() + int(body.get("expires_in", 3600)))
    return body["access_token"]


GOOGLE_ACTIVE = {"SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD"}


async def verify_google(purchase_token: str, product_id: str) -> StorePurchase:
    pkg = settings.google_play_package_name
    base = f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{pkg}/purchases"
    headers = {"Authorization": f"Bearer {await _google_access_token()}"}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{base}/subscriptionsv2/tokens/{purchase_token}", headers=headers)
        if r.status_code in (400, 404, 410):
            raise AppError(400, "invalid_receipt", "Google Play rejected this purchase token")
        r.raise_for_status()
        data = r.json()
        items = data.get("lineItems") or []
        item = next((i for i in items if i.get("productId") == product_id), items[0] if items else None)
        if not item:
            raise AppError(400, "invalid_receipt", "Purchase has no line items")
        # Unacknowledged purchases are refunded by Google after 3 days.
        if data.get("acknowledgementState") == "ACKNOWLEDGEMENT_STATE_PENDING":
            ack = await c.post(f"{base}/subscriptions/{item['productId']}/tokens/{purchase_token}:acknowledge",
                               headers=headers, json={})
            if ack.status_code >= 400:
                log.warning("google acknowledge failed status=%s", ack.status_code)
    state = data.get("subscriptionState", "")
    expires = datetime.fromisoformat(item["expiryTime"].replace("Z", "+00:00"))
    status = ("active" if state in GOOGLE_ACTIVE or (state == "SUBSCRIPTION_STATE_CANCELED"
                                                     and expires > datetime.now(timezone.utc))
              else "revoked" if state == "SUBSCRIPTION_STATE_REVOKED" else "expired")
    auto = bool((item.get("autoRenewingPlan") or {}).get("autoRenewEnabled"))
    return StorePurchase("google", purchase_token, item["productId"], status, expires, auto, data)


# ---- App Store -----------------------------------------------------------------------------------

APPLE_PROD = "https://api.storekit.itunes.apple.com"
APPLE_SANDBOX = "https://api.storekit-sandbox.itunes.apple.com"


def _apple_jwt() -> str:
    if not (settings.apple_issuer_id and settings.apple_key_id and settings.apple_private_key_file):
        raise AppError(503, "billing_not_configured", "App Store verification is not configured")
    key = open(settings.apple_private_key_file).read()
    now = int(time.time())
    return jwt.encode({"iss": settings.apple_issuer_id, "iat": now, "exp": now + 1200, "aud": "appstoreconnect-v1",
                       "bid": settings.apple_bundle_id}, key, algorithm="ES256",
                      headers={"kid": settings.apple_key_id, "typ": "JWT"})


def _jws_payload(jws: str) -> dict:
    """Decode a JWS payload fetched directly from Apple over TLS (the transport authenticates the source)."""
    part = jws.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


# Apple subscription status codes: 1 active, 2 expired, 3 billing retry, 4 grace period, 5 revoked
APPLE_STATUS = {1: "active", 2: "expired", 3: "expired", 4: "active", 5: "revoked"}


async def verify_apple(transaction_id: str) -> StorePurchase:
    headers = {"Authorization": f"Bearer {_apple_jwt()}"}
    data = None
    async with httpx.AsyncClient(timeout=15) as c:
        for base in (APPLE_PROD, APPLE_SANDBOX):  # Apple recommends prod first, then sandbox on 404
            r = await c.get(f"{base}/inApps/v1/subscriptions/{transaction_id}", headers=headers)
            if r.status_code == 200:
                data = r.json()
                break
            if r.status_code not in (400, 404):
                r.raise_for_status()
    if not data:
        raise AppError(400, "invalid_receipt", "App Store does not recognize this transaction")
    if data.get("bundleId") and settings.apple_bundle_id and data["bundleId"] != settings.apple_bundle_id:
        raise AppError(400, "invalid_receipt", "Receipt is for a different app")
    best: StorePurchase | None = None
    for group in data.get("data", []):
        for last in group.get("lastTransactions", []):
            tx = _jws_payload(last["signedTransactionInfo"])
            renewal = _jws_payload(last["signedRenewalInfo"]) if last.get("signedRenewalInfo") else {}
            status = APPLE_STATUS.get(int(last.get("status", 2)), "expired")
            if tx.get("revocationDate"):
                status = "revoked"
            p = StorePurchase("apple", str(tx["originalTransactionId"]), tx["productId"], status,
                              _ts_ms(tx["expiresDate"]), bool(renewal.get("autoRenewStatus")),
                              {"transaction": tx, "renewal": renewal, "status": last.get("status")})
            if best is None or p.expires_at > best.expires_at:
                best = p
    if not best:
        raise AppError(400, "invalid_receipt", "No subscription found for this transaction")
    return best
