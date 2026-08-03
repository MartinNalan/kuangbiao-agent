from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from threading import Lock
from typing import Any, Iterator

from .config import Settings


LLM_USAGE_SCHEMA_VERSION = "geowiki-llm-stage-usage.v1"
_stage: ContextVar[str] = ContextVar("geowiki_llm_stage", default="unclassified")
_attempt: ContextVar[int] = ContextVar("geowiki_llm_attempt", default=1)


@contextmanager
def llm_call_context(stage: str, *, attempt: int = 1) -> Iterator[None]:
    """Attach non-sensitive stage metadata to one model call.

    Context variables keep concurrent API and research tasks isolated without
    changing the public LLM client signature used by test doubles.
    """

    stage_token = _stage.set(str(stage or "unclassified")[:80])
    attempt_token = _attempt.set(max(1, int(attempt)))
    try:
        yield
    finally:
        _attempt.reset(attempt_token)
        _stage.reset(stage_token)


def current_llm_call_context() -> tuple[str, int]:
    return _stage.get(), _attempt.get()


class LLMUsageLedger:
    """Permission-restricted, bounded model-usage log without prompt bodies."""

    def __init__(self, settings: Settings):
        self.enabled = settings.llm_usage_ledger_enabled
        self.path = Path(settings.llm_usage_ledger_path)
        self.max_bytes = settings.llm_usage_ledger_max_bytes
        self.backup_count = settings.llm_usage_ledger_backup_count
        self._lock = Lock()

    def write_call(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: str,
        latency_ms: float,
        success: bool,
        usage: dict[str, Any] | None = None,
        output_chars: int = 0,
        finish_reason: str | None = None,
        error_type: str | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        stage, attempt = current_llm_call_context()
        system_content = next(
            (str(item.get("content") or "") for item in messages if item.get("role") == "system"),
            "",
        )
        usage = usage or {}
        prompt_tokens = self._integer(usage.get("prompt_tokens"))
        completion_tokens = self._integer(usage.get("completion_tokens"))
        total_tokens = self._integer(usage.get("total_tokens"))
        cache_hit_tokens, cache_miss_tokens = self._cache_tokens(usage)
        payload = {
            "schema_version": LLM_USAGE_SCHEMA_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "attempt": attempt,
            "model": model,
            "response_format": response_format,
            "system_prompt_sha256": hashlib.sha256(system_content.encode("utf-8")).hexdigest(),
            "message_count": len(messages),
            "input_chars": sum(len(str(item.get("content") or "")) for item in messages),
            "output_chars": max(0, int(output_chars)),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "prompt_cache_hit_tokens": cache_hit_tokens,
            "prompt_cache_miss_tokens": cache_miss_tokens,
            "cache_hit_ratio": (
                round(cache_hit_tokens / prompt_tokens, 6)
                if prompt_tokens and cache_hit_tokens is not None
                else None
            ),
            "latency_ms": round(max(0.0, float(latency_ms)), 3),
            "finish_reason": str(finish_reason)[:40] if finish_reason else None,
            "success": bool(success),
            "error_type": str(error_type)[:120] if error_type else None,
        }
        return self._write(payload)

    @classmethod
    def normalized_usage(cls, usage: dict[str, Any] | None) -> dict[str, int | None]:
        value = usage or {}
        cache_hit_tokens, cache_miss_tokens = cls._cache_tokens(value)
        return {
            "prompt_tokens": cls._integer(value.get("prompt_tokens")),
            "completion_tokens": cls._integer(value.get("completion_tokens")),
            "total_tokens": cls._integer(value.get("total_tokens")),
            "prompt_cache_hit_tokens": cache_hit_tokens,
            "prompt_cache_miss_tokens": cache_miss_tokens,
        }

    @staticmethod
    def _integer(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _cache_tokens(cls, usage: dict[str, Any]) -> tuple[int | None, int | None]:
        hit = cls._integer(usage.get("prompt_cache_hit_tokens"))
        miss = cls._integer(usage.get("prompt_cache_miss_tokens"))
        details = usage.get("prompt_tokens_details")
        if hit is None and isinstance(details, dict):
            hit = cls._integer(details.get("cached_tokens"))
        prompt = cls._integer(usage.get("prompt_tokens"))
        if miss is None and prompt is not None and hit is not None:
            miss = max(0, prompt - hit)
        return hit, miss

    def _write(self, payload: dict[str, Any]) -> bool:
        line = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            + "\n"
        ).encode("utf-8")
        if len(line) > self.max_bytes:
            return False
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                size = self.path.stat().st_size if self.path.exists() else 0
                if size and size + len(line) > self.max_bytes:
                    self._rotate()
                descriptor = os.open(
                    self.path,
                    os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                    0o600,
                )
                try:
                    os.write(descriptor, line)
                finally:
                    os.close(descriptor)
                os.chmod(self.path, 0o600)
            return True
        except OSError:
            return False

    def _rotate(self) -> None:
        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        if self.path.exists():
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
