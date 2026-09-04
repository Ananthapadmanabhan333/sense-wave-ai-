"""Password hashing."""

from __future__ import annotations

from sensewave.security import hash_password, verify_password


def test_roundtrip():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong horse battery staple", hashed)


def test_hashes_are_salted():
    assert hash_password("same") != hash_password("same")


def test_long_passwords_stay_distinct():
    """Raw bcrypt ignores input past 72 bytes; the SHA-256 pre-hash prevents
    two different long passwords from becoming interchangeable."""
    a = "x" * 200 + "A"
    b = "x" * 200 + "B"
    assert not verify_password(b, hash_password(a))
    assert verify_password(a, hash_password(a))


def test_malformed_stored_hash_fails_closed():
    assert verify_password("anything", "not-a-bcrypt-hash") is False
    assert verify_password("anything", "") is False
