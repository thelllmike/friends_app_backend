"""Step 1: auth + profile."""
import io

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from PIL import Image

from app.core.errors import AppError
from app.services import identity
from tests.helpers import client, random_phone, signup


def test_health():
    with client() as c:
        assert c.get("/health").json()["status"] == "ok"


def test_signup_requires_consent():
    with client() as c:
        phone = random_phone()
        code = c.post("/auth/otp/request", json={"phone": phone}, headers={"x-forwarded-for": phone}).json()["dev_code"]
        r = c.post("/auth/otp/verify", json={"phone": phone, "code": code}, headers={"x-forwarded-for": phone})
        assert r.status_code == 400 and r.json()["error"]["code"] == "consent_required"


def test_wrong_otp_and_bad_phone():
    with client() as c:
        phone = random_phone()
        c.post("/auth/otp/request", json={"phone": phone}, headers={"x-forwarded-for": phone})
        r = c.post("/auth/otp/verify", json={"phone": phone, "code": "000000", "confirm_18_plus": True,
                                             "accept_terms": True}, headers={"x-forwarded-for": phone})
        assert r.json()["error"]["code"] in ("otp_invalid",)
        r = c.post("/auth/otp/request", json={"phone": "12345"}, headers={"x-forwarded-for": "x" + phone})
        assert r.json()["error"]["code"] == "invalid_phone"


def test_signup_then_login_existing():
    with client() as c:
        a = signup(c)
        assert a["is_new_user"] and a["user"]["onboarding_step"] == 2 and a["user"]["onboarding_total_steps"] == 3
        b = signup(c, phone=a["phone"])  # second login, same phone
        assert not b["is_new_user"] and b["user"]["id"] == a["user"]["id"]


def test_profile_validation_and_step():
    with client() as c:
        u = signup(c)
        h = u["headers"]
        assert c.patch("/me", json={"age": 17}, headers=h).status_code == 422
        assert c.patch("/me", json={"country": "XX"}, headers=h).status_code == 422
        assert c.patch("/me", json={"interests": ["Music", "Cooking"]}, headers=h).status_code == 422
        six = ["Music", "Travel", "Gaming", "Movies", "Fitness", "Art"]
        assert c.patch("/me", json={"interests": six}, headers=h).status_code == 422
        r = c.patch("/me", json={"display_name": "  Ana   Maria ", "age": 22, "country": "br", "gender": "female",
                                 "interests": ["music", "Anime", "Music"], "language": "pt"}, headers=h)
        me = r.json()
        assert me["display_name"] == "Ana Maria" and me["country"] == "BR" and me["interests"] == ["Music", "Anime"]
        assert me["profile_complete"] and me["onboarding_step"] == 3 and me["language"] == "pt"
        assert c.patch("/me", json={"language": "xx"}, headers=h).json()["error"]["code"] == "unsupported_language"
        r = c.put("/me/onboarding", json={"step": 3, "completed": True}, headers=h)
        assert r.json()["onboarding_completed"]


def test_photo_upload_served_and_replaced():
    with client() as c:
        u = signup(c, profile={})
        buf = io.BytesIO()
        Image.new("RGB", (2000, 1000), "red").save(buf, "PNG")
        r = c.post("/me/photo", files={"file": ("a.png", buf.getvalue(), "image/png")}, headers=u["headers"])
        assert r.status_code == 200, r.text
        url = r.json()["photo_url"]
        img = Image.open(io.BytesIO(c.get(url).content))
        assert img.format == "JPEG" and max(img.size) == 768
        r = c.post("/me/photo", files={"file": ("x.png", b"not an image", "image/png")}, headers=u["headers"])
        assert r.status_code == 415


def test_refresh_rotation_and_reuse_detection():
    with client() as c:
        u = signup(c)
        r1 = c.post("/auth/refresh", json={"refresh_token": u["refresh_token"]})
        assert r1.status_code == 200
        # replaying the old token kills the whole family
        r2 = c.post("/auth/refresh", json={"refresh_token": u["refresh_token"]})
        assert r2.json()["error"]["code"] == "token_reused"
        r3 = c.post("/auth/refresh", json={"refresh_token": r1.json()["refresh_token"]})
        assert r3.status_code == 401


def test_logout_and_logout_all():
    with client() as c:
        u = signup(c)
        assert c.post("/auth/logout", json={"refresh_token": u["refresh_token"]}).status_code == 204
        assert c.post("/auth/refresh", json={"refresh_token": u["refresh_token"]}).status_code == 401
        assert c.post("/auth/logout-all", headers=u["headers"]).status_code == 204
        assert c.get("/me", headers=u["headers"]).status_code == 401  # access token invalidated


def test_linked_accounts_and_delete():
    with client() as c:
        u = signup(c)
        h = u["headers"]
        accts = c.get("/auth/linked-accounts", headers=h).json()
        assert [a["provider"] for a in accts] == ["phone"]
        assert c.delete("/auth/link/phone", headers=h).json()["error"]["code"] == "last_login_method"
        # link a second phone? not allowed (one per provider)
        p2 = random_phone()
        code = c.post("/auth/otp/request", json={"phone": p2}, headers={"x-forwarded-for": p2}).json()["dev_code"]
        r = c.post("/auth/link/phone", json={"phone": p2, "code": code}, headers=h)
        assert r.json()["error"]["code"] == "provider_already_linked"
        assert c.delete("/me", headers=h).status_code == 204
        assert c.get("/me", headers=h).status_code == 401
        # same phone signs up fresh
        again = signup(c, phone=u["phone"])
        assert again["is_new_user"] and again["user"]["id"] != u["user"]["id"]


# ---- Google / Apple token verification (unit, with a local RSA key standing in for the provider JWKS) ----

class _FakeJWKS:
    def __init__(self, key):
        self._key = key

    def get_signing_key_from_jwt(self, _token):
        class K:
            key = self._key.public_key()
        return K()


def _token(key, **claims):
    import time
    base = {"iat": int(time.time()), "exp": int(time.time()) + 600, "sub": "g-123"}
    return jwt.encode({**base, **claims}, key, algorithm="RS256")


async def test_google_and_apple_verification(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(identity, "_google_jwks", _FakeJWKS(key))
    monkeypatch.setattr(identity, "_apple_jwks", _FakeJWKS(key))
    monkeypatch.setattr(identity.settings, "google_client_ids", ["my-client"])
    monkeypatch.setattr(identity.settings, "apple_audiences", ["com.example.friends"])

    ok = _token(key, aud="my-client", iss="https://accounts.google.com", email="a@b.c", email_verified=True)
    v = await identity.verify_google(ok)
    assert v.subject == "g-123" and v.email == "a@b.c"
    with pytest.raises(AppError):
        await identity.verify_google(_token(key, aud="other-client", iss="accounts.google.com"))
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AppError):
        await identity.verify_google(_token(other_key, aud="my-client", iss="accounts.google.com"))

    a = await identity.verify_apple(_token(key, aud="com.example.friends", iss="https://appleid.apple.com"))
    assert a.provider == "apple"
    with pytest.raises(AppError):
        await identity.verify_apple(_token(key, aud="com.example.friends", iss="https://evil.example"))
