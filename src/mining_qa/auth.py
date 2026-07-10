import hashlib
from pathlib import Path

from fastapi import Header, HTTPException, status

from .api_keys import ApiKeyRegistry
from .config import Settings


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


def require_api_key(
    settings: Settings,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> str:
    allowed_keys = settings.allowed_api_keys
    registry = ApiKeyRegistry(Path(settings.api_key_registry_path) if settings.api_key_registry_path else None)
    registry_enabled = registry.exists()
    if not allowed_keys and not registry_enabled:
        return "local-dev"

    api_key = x_api_key or extract_bearer_token(authorization)
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "UNAUTHORIZED",
                "message": "Missing or invalid API key.",
            },
        )

    if registry_enabled:
        record = registry.authenticate(api_key)
        if record:
            return api_key

    if api_key in allowed_keys:
        return api_key

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": "UNAUTHORIZED",
            "message": "Missing or invalid API key.",
        },
    )
