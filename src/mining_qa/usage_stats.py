import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .auth import key_fingerprint
from .config import PROJECT_ROOT


class UsageStats:
    def __init__(self, path: Path | None = None):
        self.path = path or PROJECT_ROOT / "data" / "api_calls.jsonl"

    def summarize(self, api_key: str | None = None) -> dict[str, Any]:
        key_hash = key_fingerprint(api_key) if api_key else None
        today = datetime.now(timezone.utc).date().isoformat()
        total = 0
        today_total = 0
        duration_sum = 0.0
        duration_count = 0
        last_timestamp = None
        endpoint_counts: Counter[str] = Counter()
        status_counts: Counter[str] = Counter()

        if not self.path.exists():
            return self._empty()

        with self.path.open("r", encoding="utf-8") as file:
            for line in file:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if key_hash and record.get("api_key_fingerprint") != key_hash:
                    continue

                total += 1
                timestamp = record.get("timestamp")
                if timestamp and timestamp[:10] == today:
                    today_total += 1
                if timestamp:
                    last_timestamp = timestamp

                endpoint_counts[record.get("endpoint", "unknown")] += 1
                status_counts[record.get("status", "unknown")] += 1
                duration = record.get("duration_ms")
                if isinstance(duration, int | float):
                    duration_sum += float(duration)
                    duration_count += 1

        return {
            "total_calls": total,
            "today_calls": today_total,
            "endpoint_counts": dict(endpoint_counts),
            "status_counts": dict(status_counts),
            "average_duration_ms": round(duration_sum / duration_count, 2) if duration_count else 0,
            "last_call_at": last_timestamp,
        }

    def _empty(self) -> dict[str, Any]:
        return {
            "total_calls": 0,
            "today_calls": 0,
            "endpoint_counts": {},
            "status_counts": {},
            "average_duration_ms": 0,
            "last_call_at": None,
        }
