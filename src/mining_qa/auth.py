from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request, status

from .account_store import AccountStore
from .api_keys import ApiKeyRegistry
from .config import PROJECT_ROOT, Settings, get_settings


AuthType = Literal["session", "api_key", "legacy_api_key", "local_dev"]


@dataclass(frozen=True)
class Principal:
    user_id: str | None
    account: str
    display_name: str
    role: str
    auth_type: AuthType
    credential_id: str | None
    rate_limit_key: str
    quota_managed: bool


def key_fingerprint(api_key: str | None) -> str | None:
    if not api_key:
        return None
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _resolved_app_db_path(settings: Settings) -> Path:
    path = Path(settings.app_db_path)
    return path if path.is_absolute() else PROJECT_ROOT / path


@lru_cache
def _store_for_path(path: str) -> AccountStore:
    return AccountStore(Path(path))


def get_account_store(settings: Settings | None = None) -> AccountStore:
    current = settings or get_settings()
    return _store_for_path(str(_resolved_app_db_path(current)))


def clear_account_store_cache() -> None:
    _store_for_path.cache_clear()


def resolve_principal(
    request: Request,
    x_api_key: str | None = None,
    authorization: str | None = None,
    settings: Settings | None = None,
) -> Principal:
    current = settings or get_settings()
    store = get_account_store(current)
    api_key = x_api_key or extract_bearer_token(authorization)
    if api_key:
        account_key = store.authenticate_api_key(api_key)
        if account_key:
            return Principal(
                user_id=account_key["user_id"],
                account=account_key["account"],
                display_name=account_key["display_name"],
                role=account_key["role"],
                auth_type="api_key",
                credential_id=account_key["api_key_id"],
                rate_limit_key=f"api:{account_key['api_key_id']}",
                quota_managed=True,
            )

        registry = ApiKeyRegistry(Path(current.api_key_registry_path) if current.api_key_registry_path else None)
        registry_record = registry.authenticate(api_key) if registry.exists() else None
        if registry_record or api_key in current.allowed_api_keys:
            fingerprint = key_fingerprint(api_key) or "unknown"
            return Principal(
                user_id=None,
                account="legacy-api-client",
                display_name="Legacy API Client",
                role="service",
                auth_type="legacy_api_key",
                credential_id=registry_record.key_id if registry_record else fingerprint,
                rate_limit_key=f"legacy:{fingerprint}",
                quota_managed=False,
            )

    session_token = request.cookies.get(current.session_cookie_name)
    if session_token:
        user = store.authenticate_session(session_token)
        if user:
            return Principal(
                user_id=user["user_id"],
                account=user["account"],
                display_name=user["display_name"],
                role=user["role"],
                auth_type="session",
                credential_id=user.get("session_id"),
                rate_limit_key=f"user:{user['user_id']}",
                quota_managed=True,
            )

    if not current.auth_required:
        return Principal(
            user_id=None,
            account="local-dev",
            display_name="Local Developer",
            role="service",
            auth_type="local_dev",
            credential_id=None,
            rate_limit_key="local-dev",
            quota_managed=False,
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "UNAUTHORIZED", "message": "请登录或提供有效的 API Key。"},
    )


def require_session_principal(
    request: Request,
    settings: Settings | None = None,
) -> Principal:
    principal = resolve_principal(request, settings=settings)
    if principal.auth_type != "session" or not principal.user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "SESSION_REQUIRED", "message": "该操作需要网页登录。"},
        )
    return principal


def require_admin_principal(
    request: Request,
    settings: Settings | None = None,
) -> Principal:
    principal = require_session_principal(request, settings=settings)
    if principal.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "ADMIN_REQUIRED", "message": "该操作仅限管理员。"},
        )
    return principal


def require_api_key(
    settings: Settings,
    x_api_key: str | None = None,
    authorization: str | None = None,
) -> str:
    """Compatibility helper for older callers that do not have a Request object."""
    api_key = x_api_key or extract_bearer_token(authorization)
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "Missing or invalid API key."},
        )
    store = get_account_store(settings)
    if store.authenticate_api_key(api_key):
        return api_key
    registry = ApiKeyRegistry(Path(settings.api_key_registry_path) if settings.api_key_registry_path else None)
    if (registry.exists() and registry.authenticate(api_key)) or api_key in settings.allowed_api_keys:
        return api_key
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "UNAUTHORIZED", "message": "Missing or invalid API key."},
    )
