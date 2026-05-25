"""Tests for cryptographic helper functions."""
from __future__ import annotations

import re

from app.config import get_settings
from app.crypto import generate_api_key, hash_api_key, hash_password, redact_api_key, verify_password


def test_generate_api_key_hash_and_prefix() -> None:
    """Generated keys match the public format and redact safely."""
    plaintext, digest, prefix = generate_api_key("live")
    assert re.match(r"^rsmb_live_[A-Za-z0-9_-]{43}$", plaintext)
    assert digest == hash_api_key(plaintext, get_settings().key_pepper)
    assert prefix == redact_api_key(plaintext)
    assert plaintext not in prefix


def test_password_hash_verify_round_trip() -> None:
    """Argon2 password hashes verify the original but not a different value."""
    digest = hash_password("correct horse")
    assert verify_password("correct horse", digest)
    assert not verify_password("wrong horse", digest)

