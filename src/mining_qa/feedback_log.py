import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .auth import key_fingerprint
from .config import PROJECT_ROOT


class FeedbackLogger:
    def __init__(self, path: Path | None = None):
        self.path = path or PROJECT_ROOT / "data" / "answer_feedback.jsonl"

    def write(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **record,
        }
        if "api_key" in payload:
            payload["api_key_fingerprint"] = key_fingerprint(payload.pop("api_key"))
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False) + "\n")
