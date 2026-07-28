"""Small purpose-bound signed session tokens for self-host browser sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime


class SessionTokenError(ValueError):
    pass


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(f"{value}{padding}")
    except ValueError as exc:
        raise SessionTokenError("session token is malformed") from exc


def _key(secret: str) -> bytes:
    return hashlib.sha256(f"ope-session\0{secret}".encode()).digest()


def sign_session(
    subject: str,
    *,
    purpose: str,
    secret: str,
    now: datetime | None = None,
) -> str:
    issued_at = int((now or datetime.now(UTC)).timestamp())
    payload = json.dumps(
        {"sub": subject, "purpose": purpose, "iat": issued_at},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded = _encode(payload)
    signature = _encode(hmac.digest(_key(secret), encoded.encode(), "sha256"))
    return f"{encoded}.{signature}"


def verify_session(
    token: str,
    *,
    purpose: str,
    secret: str,
    max_age_s: int,
    now: datetime | None = None,
) -> str:
    try:
        encoded, signature = token.split(".", 1)
    except ValueError as exc:
        raise SessionTokenError("session token is malformed") from exc

    expected = _encode(hmac.digest(_key(secret), encoded.encode(), "sha256"))
    if not hmac.compare_digest(signature, expected):
        raise SessionTokenError("session token signature is invalid")

    try:
        payload = json.loads(_decode(encoded))
        subject = payload["sub"]
        token_purpose = payload["purpose"]
        issued_at = int(payload["iat"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SessionTokenError("session token payload is invalid") from exc

    if not isinstance(subject, str) or not subject:
        raise SessionTokenError("session token subject is invalid")
    if token_purpose != purpose:
        raise SessionTokenError("session token purpose is invalid")

    timestamp = int((now or datetime.now(UTC)).timestamp())
    age = timestamp - issued_at
    if age < -60:
        raise SessionTokenError("session token issue time is invalid")
    if age > max_age_s:
        raise SessionTokenError("session token expired")
    return subject

