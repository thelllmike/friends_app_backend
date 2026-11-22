"""Server-side verification of Google and Apple ID tokens (JWKS + signature + aud/iss/exp)."""
import asyncio
from dataclasses import dataclass

import jwt
from jwt import PyJWKClient

from app.core.config import settings
from app.core.errors import AppError

GOOGLE_JWKS = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
APPLE_JWKS = "https://appleid.apple.com/auth/keys"
APPLE_ISSUER = "https://appleid.apple.com"

# PyJWKClient caches keys in memory between calls.
_google_jwks = PyJWKClient(GOOGLE_JWKS, cache_keys=True, lifespan=3600)
_apple_jwks = PyJWKClient(APPLE_JWKS, cache_keys=True, lifespan=3600)


@dataclass
class VerifiedIdentity:
    provider: str
    subject: str
    email: str | None


def _verify(token: str, jwks: PyJWKClient, audiences: list[str], issuers: tuple[str, ...] | str) -> dict:
    if not audiences:
        raise AppError(500, "provider_not_configured", "Login provider not configured on server")
    try:
        key = jwks.get_signing_key_from_jwt(token)
        return jwt.decode(token, key.key, algorithms=["RS256"], audience=audiences, issuer=issuers,
                          options={"require": ["exp", "iat", "sub", "aud", "iss"]})
    except jwt.PyJWTError as e:
        raise AppError(401, "invalid_identity_token", f"Identity token rejected: {e}")


async def verify_google(id_token: str) -> VerifiedIdentity:
    claims = await asyncio.to_thread(_verify, id_token, _google_jwks, settings.google_client_ids, GOOGLE_ISSUERS)
    if claims.get("email") and not claims.get("email_verified", False):
        claims["email"] = None
    return VerifiedIdentity("google", claims["sub"], claims.get("email"))


async def verify_apple(identity_token: str) -> VerifiedIdentity:
    claims = await asyncio.to_thread(_verify, identity_token, _apple_jwks, settings.apple_audiences, APPLE_ISSUER)
    return VerifiedIdentity("apple", claims["sub"], claims.get("email"))
