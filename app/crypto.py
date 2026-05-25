"""API-key and password cryptography helpers."""
from __future__ import annotations

import hashlib
import secrets
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import get_settings
from app.constants import API_KEY_PREFIX_HEAD, API_KEY_PREFIX_TAIL, API_KEY_RANDOM_BYTES

_password_hasher = PasswordHasher()


def hash_api_key(plaintext: str, pepper: str) -> str:
    """Hash a plaintext API key with the active pepper using SHA-256 hex."""
    return hashlib.sha256(f"{plaintext}{pepper}".encode("utf-8")).hexdigest()


def redact_api_key(plaintext: str) -> str:
    """Return a display-only prefix using the first 8 and last 4 body chars."""
    parts = plaintext.split("_", 2)
    if len(parts) != 3:
        raise ValueError("API key must use rsmb_<env>_<token> format")
    head = parts[2][:API_KEY_PREFIX_HEAD]
    tail = parts[2][-API_KEY_PREFIX_TAIL:]
    return f"{parts[0]}_{parts[1]}_{head}...{tail}"


def generate_api_key(env: Literal["live", "test"]) -> tuple[str, str, str]:
    """Generate a plaintext API key, its hash, and its redacted prefix."""
    plaintext = f"rsmb_{env}_{secrets.token_urlsafe(API_KEY_RANDOM_BYTES)}"
    digest = hash_api_key(plaintext, get_settings().key_pepper)
    return plaintext, digest, redact_api_key(plaintext)


def hash_password(plaintext: str) -> str:
    """Hash a password with Argon2id defaults from argon2-cffi."""
    return _password_hasher.hash(plaintext)


def verify_password(plaintext: str, password_hash: str) -> bool:
    """Verify a password hash and return False instead of raising on mismatch."""
    try:
        return _password_hasher.verify(password_hash, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
