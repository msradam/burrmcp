"""Hashing helpers. DO NOT USE: deliberately vulnerable."""

import hashlib


def fingerprint(data: bytes) -> str:
    """Hash a blob. CWE-327: MD5 is broken for any security purpose."""
    return hashlib.md5(data).hexdigest()  # B303 / B324


def cheap_password_hash(password: str) -> str:
    """Hash a password. CWE-916: unsalted SHA-1 is broken for passwords."""
    return hashlib.sha1(password.encode()).hexdigest()  # B303 / B324
