"""Time-limited coturn credentials (TURN REST API / use-auth-secret)."""
import base64
import hashlib
import hmac
import time

from app.core.config import settings


def credentials(user_id: str) -> dict:
    expires = int(time.time()) + settings.turn_ttl_seconds
    username = f"{expires}:{user_id}"
    digest = hmac.new(settings.turn_secret.encode(), username.encode(), hashlib.sha1).digest()
    password = base64.b64encode(digest).decode()
    h = settings.turn_host
    return {
        "username": username,
        "credential": password,
        "ttl": settings.turn_ttl_seconds,
        "expires_at": expires,
        # Shape matches RTCPeerConnection({iceServers}) directly.
        "ice_servers": [
            {"urls": [f"stun:{h}:3478"]},
            {"urls": [f"turn:{h}:3478?transport=udp", f"turn:{h}:3478?transport=tcp"],
             "username": username, "credential": password},
            {"urls": [f"turns:{h}:{port}?transport=tcp" for port in settings.turn_tls_ports],
             "username": username, "credential": password},
        ],
    }
