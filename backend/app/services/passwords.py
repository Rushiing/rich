"""Password hashing helpers.

Wraps bcrypt with our own constants so route code stays terse. We keep
hash size at default cost factor 12 — fine on Railway's CPU. Hashes are
self-contained ($2b$12$...$<hash>) so verifying doesn't need a salt
column; whatever cost we used at hash time travels with the string.
"""
from __future__ import annotations

import bcrypt

# Reasonable lower bound. The frontend already prevents shorter; this
# enforces at the API boundary too in case someone hits /register directly.
MIN_PASSWORD_LEN = 6
MAX_PASSWORD_LEN = 64


class PasswordError(ValueError):
    """Raised when a password fails validation. Caller turns this into a 400."""


def validate(password: str) -> None:
    """Pre-hash sanity. Raises PasswordError when bad."""
    if not isinstance(password, str):
        raise PasswordError("密码格式错误")
    if len(password) < MIN_PASSWORD_LEN:
        raise PasswordError(f"密码至少 {MIN_PASSWORD_LEN} 位")
    if len(password) > MAX_PASSWORD_LEN:
        raise PasswordError(f"密码最长 {MAX_PASSWORD_LEN} 位")
    # bcrypt has a 72-byte input ceiling — anything longer gets silently
    # truncated, which is a footgun for users picking long passphrases.
    # We refuse outright instead of letting it slip through.
    if len(password.encode("utf-8")) > 72:
        raise PasswordError("密码过长（含中文时请控制在 24 字内）")


def hash_password(password: str) -> str:
    """Bcrypt-hash a plaintext password. Caller must call validate() first."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, hashed: str | None) -> bool:
    """Constant-time check. Returns False (never raises) when hashed is
    None / empty / malformed — that way a user without a password set just
    fails login like an unknown account, no info leak."""
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False
