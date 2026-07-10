from __future__ import annotations

import json
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT


DEFAULT_API_KEY_REGISTRY = PROJECT_ROOT / "data" / "api_keys.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    return "mkqa_" + secrets.token_urlsafe(32)


@dataclass(frozen=True)
class ApiKeyRecord:
    key_id: str
    name: str
    key_hash: str
    enabled: bool
    purpose: str | None = None
    created_at: str | None = None
    last_used_at: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ApiKeyRecord":
        return cls(
            key_id=str(payload.get("key_id") or ""),
            name=str(payload.get("name") or ""),
            key_hash=str(payload.get("key_hash") or ""),
            enabled=bool(payload.get("enabled", True)),
            purpose=payload.get("purpose"),
            created_at=payload.get("created_at"),
            last_used_at=payload.get("last_used_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key_id": self.key_id,
            "name": self.name,
            "key_hash": self.key_hash,
            "enabled": self.enabled,
            "purpose": self.purpose,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }


class ApiKeyRegistry:
    def __init__(self, path: Path | None = None):
        self.path = path or DEFAULT_API_KEY_REGISTRY

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> list[ApiKeyRecord]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        items = payload.get("keys", []) if isinstance(payload, dict) else []
        return [ApiKeyRecord.from_dict(item) for item in items if isinstance(item, dict)]

    def save(self, records: list[ApiKeyRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": utc_now(),
            "keys": [record.to_dict() for record in records],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.path.chmod(0o600)

    def create(self, name: str, purpose: str | None = None, api_key: str | None = None) -> tuple[ApiKeyRecord, str]:
        plain_key = api_key or generate_api_key()
        records = self.load()
        now = utc_now()
        key_id = "key_" + secrets.token_hex(6)
        record = ApiKeyRecord(
            key_id=key_id,
            name=name,
            key_hash=key_hash(plain_key),
            enabled=True,
            purpose=purpose,
            created_at=now,
            last_used_at=None,
        )
        records.append(record)
        self.save(records)
        return record, plain_key

    def find(self, api_key: str) -> ApiKeyRecord | None:
        hashed = key_hash(api_key)
        for record in self.load():
            if record.key_hash == hashed:
                return record
        return None

    def authenticate(self, api_key: str) -> ApiKeyRecord | None:
        record = self.find(api_key)
        if not record or not record.enabled:
            return None
        self.mark_used(record.key_id)
        return record

    def set_enabled(self, key_id: str, enabled: bool) -> ApiKeyRecord | None:
        records = self.load()
        updated: ApiKeyRecord | None = None
        new_records: list[ApiKeyRecord] = []
        for record in records:
            if record.key_id == key_id:
                updated = ApiKeyRecord(
                    key_id=record.key_id,
                    name=record.name,
                    key_hash=record.key_hash,
                    enabled=enabled,
                    purpose=record.purpose,
                    created_at=record.created_at,
                    last_used_at=record.last_used_at,
                )
                new_records.append(updated)
            else:
                new_records.append(record)
        if updated:
            self.save(new_records)
        return updated

    def mark_used(self, key_id: str) -> None:
        records = self.load()
        changed = False
        new_records: list[ApiKeyRecord] = []
        for record in records:
            if record.key_id == key_id:
                changed = True
                new_records.append(
                    ApiKeyRecord(
                        key_id=record.key_id,
                        name=record.name,
                        key_hash=record.key_hash,
                        enabled=record.enabled,
                        purpose=record.purpose,
                        created_at=record.created_at,
                        last_used_at=utc_now(),
                    )
                )
            else:
                new_records.append(record)
        if changed:
            self.save(new_records)
