"""Credential encryption.

Tokens are encrypted at rest. ``KeyProvider`` is the seam that lets self-host
derive a key from a local secret while a hosted deployment swaps in a
KMS/envelope-encryption implementation without touching call sites.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Protocol

from cryptography.fernet import Fernet

from app.settings import get_settings


class KeyProvider(Protocol):
    def fernet(self) -> Fernet: ...


class EnvKeyProvider:
    """Derives a Fernet key from ``OPE_SECRET_KEY``. Self-host default."""

    def __init__(self, secret: str) -> None:
        digest = hashlib.sha256(secret.encode()).digest()
        self._key = base64.urlsafe_b64encode(digest)

    def fernet(self) -> Fernet:
        return Fernet(self._key)


_provider: KeyProvider = EnvKeyProvider(get_settings().secret_key)


def set_key_provider(provider: KeyProvider) -> None:
    """Hosted deployments inject a KMS-backed provider here at startup."""
    global _provider
    _provider = provider


def encrypt(plaintext: str) -> bytes:
    return _provider.fernet().encrypt(plaintext.encode())


def decrypt(token: bytes) -> str:
    return _provider.fernet().decrypt(token).decode()
