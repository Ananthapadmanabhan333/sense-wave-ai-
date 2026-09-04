"""Password hashing.

Uses ``bcrypt`` directly rather than passlib: passlib 1.7.4 reads
``bcrypt.__about__``, which bcrypt removed in 4.1, and its backend probe then
trips bcrypt's 72-byte check and raises. The direct API is stable and one
dependency lighter.

bcrypt silently ignores input past 72 bytes, which would make two different long
passwords interchangeable. We pre-hash with SHA-256 and base64-encode first, so
every password contributes its full entropy and the input to bcrypt is always
44 bytes.
"""

from __future__ import annotations

import base64
import hashlib

import bcrypt


def _prehash(raw: str) -> bytes:
    """SHA-256 then base64, so bcrypt never sees more than 72 bytes."""
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(raw: str) -> str:
    return bcrypt.hashpw(_prehash(raw), bcrypt.gensalt()).decode("ascii")


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(raw), hashed.encode("ascii"))
    except (ValueError, TypeError):
        # A malformed stored hash must fail closed, not raise into the caller.
        return False
