"""Admin authentication: HMAC-signed session tokens issued by ``/api/admin/login``.

Staging/production require ``ADMIN_INITIAL_EMAIL``, a 12+ character
``ADMIN_INITIAL_PASSWORD`` and a strong ``JWT_SECRET``; otherwise the admin API (which
includes placing outbound calls) stays disabled. The demo token and demo password work
only in development.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from fastapi import Header, HTTPException

from ..auth import hash_password, verify_password
from ..core.config import WEAK_SECRETS, settings

ADMIN_TOKEN_TTL = 12 * 3600
_demo_admin_hash = hash_password('demo-password')


def _sign(payload: str) -> str:
    return hmac.new(settings.jwt_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def session_token(role: str = 'admin', ttl: int = ADMIN_TOKEN_TTL) -> str:
    payload = f'{role}|{int(time.time()) + ttl}|{secrets.token_hex(8)}'
    return 'mcs.' + base64.urlsafe_b64encode(payload.encode()).decode() + '.' + _sign(payload)


def admin_status() -> tuple[bool, str]:
    """Whether the admin API is enabled, and why not."""
    if settings.is_development and not (settings.admin_initial_email and settings.admin_initial_password):
        return True, 'development'
    if not (settings.admin_initial_email and settings.admin_initial_password):
        return False, 'ADMIN_INITIAL_EMAIL/ADMIN_INITIAL_PASSWORD are not set'
    if settings.jwt_secret in WEAK_SECRETS or len(settings.jwt_secret) < 32:
        return False, 'JWT_SECRET is weak (use at least 32 random characters)'
    if len(settings.admin_initial_password) < 12:
        return False, 'ADMIN_INITIAL_PASSWORD is shorter than 12 characters'
    return True, 'configured'


def admin_allowed(authorization: str | None) -> bool:
    if settings.is_development and authorization and settings.admin_demo_token and \
            hmac.compare_digest(authorization, f'Bearer {settings.admin_demo_token}'):
        return True
    if not admin_status()[0] or not authorization or not authorization.startswith('Bearer mcs.'):
        return False
    try:
        _, encoded, sig = authorization.split('.', 2)
        payload = base64.urlsafe_b64decode(encoded).decode()
        role, expires, _nonce = payload.split('|', 2)
        return hmac.compare_digest(sig, _sign(payload)) and role == 'admin' and int(expires) > time.time()
    except (ValueError, UnicodeDecodeError):
        return False


def require_admin(authorization: str | None = Header(default=None)) -> str:
    if not admin_allowed(authorization):
        raise HTTPException(403, 'admin authorization required')
    return 'admin'


def check_login(email: str, password: str) -> bool:
    expected_email = settings.admin_initial_email or ('demo@example.test' if settings.is_development else '')
    if settings.is_development and not settings.admin_initial_password:
        password_ok = verify_password(password, _demo_admin_hash)
    else:
        password_ok = bool(settings.admin_initial_password) and hmac.compare_digest(
            password.encode(), settings.admin_initial_password.encode())
    return bool(expected_email) and hmac.compare_digest(email.encode(), expected_email.encode()) and password_ok
