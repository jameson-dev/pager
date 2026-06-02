"""Simple single-password gate.

One shared password (no username) blocks the whole site until entered. The
password is stored only as a salted PBKDF2 hash in config; a persisted random
secret signs the session cookie so logins survive restarts.

If no password is configured, auth is disabled (nothing is blocked) so existing
deployments keep working until an admin sets one.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from . import config as cfg

COOKIE_NAME = "pager_session"
_ITERATIONS = 200_000


# --------------------------------------------------------------- password hashing
def hash_password(password: str) -> str:
    """Return a 'pbkdf2$<iter>$<salt_hex>$<hash_hex>' string."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:  # noqa: BLE001  (malformed stored hash)
        return False


# --------------------------------------------------------------- config helpers
def _auth_conf() -> dict:
    return cfg.load_config().get("auth") or {}


def is_enabled() -> bool:
    """Auth is active only once a password hash is configured."""
    return bool(_auth_conf().get("password_hash"))


def set_password(password: str) -> None:
    conf = cfg.load_config()
    auth = dict(conf.get("auth") or {})
    auth["password_hash"] = hash_password(password)
    auth.setdefault("secret", secrets.token_hex(32))  # keep existing secret
    conf["auth"] = auth
    cfg.save_config(conf)


def clear_password() -> None:
    conf = cfg.load_config()
    auth = dict(conf.get("auth") or {})
    auth.pop("password_hash", None)
    conf["auth"] = auth
    cfg.save_config(conf)


def check_password(password: str) -> bool:
    stored = _auth_conf().get("password_hash")
    return bool(stored) and verify_password(password, stored)


# --------------------------------------------------------------- session cookie
def _secret() -> str:
    """Persisted signing secret; created on first use."""
    auth = _auth_conf()
    sec = auth.get("secret")
    if not sec:
        conf = cfg.load_config()
        a = dict(conf.get("auth") or {})
        sec = a.get("secret") or secrets.token_hex(32)
        a["secret"] = sec
        conf["auth"] = a
        cfg.save_config(conf)
    return sec


def make_session_token() -> str:
    """A signed token bound to the current password hash + secret. Changing the
    password (new hash) invalidates all existing sessions."""
    auth = _auth_conf()
    payload = auth.get("password_hash", "")
    return hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()


def valid_session(token: str | None) -> bool:
    if not token:
        return False
    return hmac.compare_digest(token, make_session_token())
