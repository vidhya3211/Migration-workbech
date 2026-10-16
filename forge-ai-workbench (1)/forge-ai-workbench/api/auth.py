"""Administrator authentication.

The workbench has one kind of user: the administrator. Every review decision the administrator records is
attributed to their signed-in identity and to the accountable role of the checkpoint it was taken at (the
audit trail shows both), so the governance trail is preserved without per-persona logins.

* ``admin`` (default): a single administrator account. The password is verified against a PBKDF2-SHA256 hash
  (``AIWB_ADMIN_PASSWORD_HASH``, from Key Vault) and a short-lived HS256 bearer token is issued, signed with
  ``AIWB_AUTH_SIGNING_KEY`` (Key Vault). Failed sign-ins are counted in PostgreSQL and lock the account for
  ``auth.lockout_seconds`` after ``auth.max_failed_logins`` attempts. For local development only,
  ``AIWB_ADMIN_PASSWORD`` may be given in clear (refused when ``environment=production``).
* ``entra``: Microsoft Entra ID bearer tokens validated against the tenant's JWKS; the token must carry the
  ``auth.admin_app_role`` app role.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import jwt
from fastapi import HTTPException, Request, status

from config.settings import Settings, secret

ADMIN = "admin"
ISSUER = "ai-workbench"
PBKDF2_ITERATIONS = 390_000


@dataclass(frozen=True)
class Principal:
    user: str
    roles: frozenset[str]

    @property
    def is_admin(self) -> bool:
        return ADMIN in self.roles

    def has(self, role: str) -> bool:
        return self.is_admin or role in self.roles


# ------------------------------------------------------------------ password hashing
def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(iterations, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


class AuthConfigError(RuntimeError):
    pass


class AdminAuthenticator:
    def __init__(self, settings: Settings, kc) -> None:
        self.settings = settings
        self.kc = kc
        self._hash = secret("AIWB_ADMIN_PASSWORD_HASH")
        dev_password = secret("AIWB_ADMIN_PASSWORD")
        if not self._hash and dev_password:
            if settings.environment == "production":
                raise AuthConfigError("AIWB_ADMIN_PASSWORD is for local development only; provide "
                                      "AIWB_ADMIN_PASSWORD_HASH from Key Vault in production")
            self._hash = hash_password(dev_password, iterations=10_000)
        key = secret("AIWB_AUTH_SIGNING_KEY")
        if not key:
            if settings.environment == "production":
                raise AuthConfigError("AIWB_AUTH_SIGNING_KEY must be provided from Key Vault in production")
            warnings.warn("AIWB_AUTH_SIGNING_KEY not set: using an ephemeral signing key (sessions end on restart)",
                          stacklevel=2)
            key = secrets.token_urlsafe(48)
        if len(key) < 32:
            raise AuthConfigError("AIWB_AUTH_SIGNING_KEY must be at least 32 characters")
        self._key = key

    @property
    def configured(self) -> bool:
        return bool(self._hash)

    def login(self, username: str, password: str) -> dict:
        a = self.settings.auth
        if not self.configured:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "administrator sign-in is not configured (set AIWB_ADMIN_PASSWORD_HASH)")
        user = (username or "").strip()
        state = self.kc.login_state(user) if user else None
        now = datetime.now(timezone.utc)
        if state is not None and state.locked_until is not None and state.locked_until > now:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many failed sign-ins; try again later")
        ok = hmac.compare_digest(user.lower(), a.admin_username.lower()) and verify_password(password or "", self._hash)
        self.kc.record_login(user or "-", ok, a.max_failed_logins, a.lockout_seconds)
        if not ok:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password")
        exp = now + timedelta(minutes=a.token_ttl_minutes)
        token = jwt.encode({"sub": a.admin_username, "roles": [ADMIN], "iss": ISSUER, "iat": int(now.timestamp()),
                            "exp": int(exp.timestamp())}, self._key, algorithm="HS256")
        return {"access_token": token, "token_type": "bearer", "expires_at": exp.isoformat(),
                "user": {"username": a.admin_username, "roles": [ADMIN]}}

    def principal(self, token: str) -> Principal:
        try:
            claims = jwt.decode(token, self._key, algorithms=["HS256"], issuer=ISSUER,
                                options={"require": ["exp", "sub", "iss"]})
        except jwt.PyJWTError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}") from exc
        roles = frozenset(claims.get("roles") or [])
        if ADMIN not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "administrator role required")
        return Principal(user=claims["sub"], roles=roles)


@lru_cache(maxsize=4)
def _jwks_client(tenant: str):
    return jwt.PyJWKClient(f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys")


def _entra(token: str, settings: Settings) -> Principal:  # pragma: no cover - requires Entra ID
    a = settings.auth
    try:
        key = _jwks_client(a.entra_tenant_id).get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=a.entra_audience,
                            issuer=f"https://login.microsoftonline.com/{a.entra_tenant_id}/v2.0")
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}") from exc
    user = claims.get("preferred_username") or claims.get("upn") or claims.get("oid")
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token carries no user identity")
    if a.admin_app_role not in (claims.get(a.role_claim) or []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"app role {a.admin_app_role!r} required")
    return Principal(user=user, roles=frozenset({ADMIN}))


def bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip()
    token = request.query_params.get("access_token")   # EventSource cannot set headers (SSE only)
    if token and request.url.path.endswith("/events"):
        return token
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in required", headers={"WWW-Authenticate": "Bearer"})


def principal_dependency(settings: Settings, authenticator: AdminAuthenticator | None):
    def get_principal(request: Request) -> Principal:
        token = bearer(request)
        if settings.auth.mode == "entra":
            return _entra(token, settings)
        if authenticator is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "authentication is not configured")
        return authenticator.principal(token)
    return get_principal
