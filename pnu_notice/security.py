from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import urllib.parse
import urllib.request


def random_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str, secret: str) -> str:
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def verify_turnstile(token: str, secret: str, expected_action: str, hostnames: tuple[str, ...],
                     remote_ip: str | None = None) -> bool:
    """Server-side Siteverify. Fails closed on any error or mismatch; tokens are single-use."""
    if not token or len(token) > 2048 or not hostnames:
        return False
    fields = {"secret": secret, "response": token}
    if remote_ip:
        fields["remoteip"] = remote_ip
    request = urllib.request.Request(TURNSTILE_VERIFY_URL, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read())
    except Exception:
        return False
    return (result.get("success") is True and result.get("action") == expected_action
            and result.get("hostname") in hostnames)


def management_token(subscriber_id: int, created_at: str, secret: str) -> str:
    message = f"{subscriber_id}:{created_at}"
    signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{message}:{signature}".encode()).decode().rstrip("=")
