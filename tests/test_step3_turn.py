"""Step 3: TURN credentials match coturn's use-auth-secret algorithm."""
import base64
import hashlib
import hmac
import time

from app.core.config import settings
from tests.helpers import client, signup


def test_turn_credentials():
    with client() as c:
        assert c.get("/turn-credentials").status_code == 401
        u = signup(c)
        r = c.get("/turn-credentials", headers=u["headers"]).json()
    expires, uid = r["username"].split(":")
    assert uid == u["user"]["id"] and int(expires) > time.time()
    expected = base64.b64encode(hmac.new(settings.turn_secret.encode(), r["username"].encode(),
                                         hashlib.sha1).digest()).decode()
    assert r["credential"] == expected
    urls = [x for s in r["ice_servers"] for x in s["urls"]]
    assert any(x.startswith("stun:") for x in urls)
    assert any("3478?transport=udp" in x for x in urls) and any("3478?transport=tcp" in x for x in urls)
    tls = [x for x in urls if x.startswith("turns:")]
    assert tls and all(any(f":{p}?" in x for x in tls) for p in settings.turn_tls_ports)
