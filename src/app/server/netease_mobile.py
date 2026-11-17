"""Device approval for NetEase favorites on the fnAudio HTTPS entry."""

import hashlib
import re
import secrets
import time
from http.cookies import CookieError, SimpleCookie

from state_store import state_db_connect


COOKIE_NAME = "__Host-fnaudio-netease"
APPROVAL_SECONDS = 10 * 60
SESSION_SECONDS = 30 * 24 * 60 * 60
PROXY_PORT = 53200


def device_digest(cookie_header):
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except CookieError:
        return ""
    value = cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        return ""
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def device_cookie(token, max_age):
    return f"{COOKIE_NAME}={token}; Path=/; Max-Age={max_age}; Secure; HttpOnly; SameSite=Strict"


def device_status(config, digest):
    if not digest:
        return False, ""
    with state_db_connect(config) as conn:
        row = conn.execute(
            "SELECT code FROM netease_mobile_devices WHERE digest = ? AND expires > ?",
            (digest, int(time.time())),
        ).fetchone()
    return (row["code"] is None, row["code"] or "") if row else (False, "")


def request_approval(config):
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    code = secrets.token_hex(6).upper()
    now = int(time.time())
    with state_db_connect(config) as conn:
        conn.execute("DELETE FROM netease_mobile_devices WHERE expires <= ?", (now,))
        result = conn.execute(
            """INSERT INTO netease_mobile_devices (digest, code, expires)
               SELECT ?, ?, ? WHERE (SELECT COUNT(*) FROM netease_mobile_devices WHERE code IS NOT NULL) < 64""",
            (digest, code, now + APPROVAL_SECONDS),
        )
        if result.rowcount == 0:
            return "", ""
    return token, code


def approve_device(config, code):
    if not isinstance(code, str) or not re.fullmatch(r"[0-9A-F]{12}", code.strip().upper()):
        return False
    now = int(time.time())
    with state_db_connect(config) as conn:
        result = conn.execute(
            "UPDATE netease_mobile_devices SET code = NULL, expires = ? WHERE code = ? AND expires > ?",
            (now + SESSION_SECONDS, code.strip().upper(), now),
        )
        return result.rowcount == 1


def revoke_devices(config):
    with state_db_connect(config) as conn:
        conn.execute("DELETE FROM netease_mobile_devices")
