from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ALLOWLIST_ARTIFACT = (
    PROJECT_ROOT / "data" / "knowledge_base" / "governance" / "mnr_valid_document_allowlist.json"
)
DEFAULT_POLICY_CUTOFF = date(2026, 1, 1)


def normalize_document_number(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(char for char in text if unicodedata.category(char) not in {"Cf", "Cc"})
    translations = {ord(char): "〔" for char in "【［[（(﹝"}
    translations.update({ord(char): "〕" for char in "】］]）)﹞"})
    translations.update({ord(char): "-" for char in "—–－﹣‐"})
    return re.sub(r"\s+", "", text.translate(translations)).strip()


def parse_document_date(value: object) -> date | None:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    match = re.search(r"(\d{4})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})", text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def load_allowlist_artifact(path: Path = DEFAULT_ALLOWLIST_ARTIFACT) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"MNR valid-document allowlist artifact is missing: {path}. "
            "Run scripts/govern_mnr_policy_allowlist.py with the authoritative workbook first."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError(f"Invalid MNR allowlist artifact: {path}")
    return data


def allowlist_numbers(artifact: dict[str, Any]) -> set[str]:
    return {
        str(entry.get("normalized_document_number") or "")
        for entry in artifact.get("entries") or []
        if entry.get("normalized_document_number")
    }


def policy_is_allowed(
    document_number: object,
    publication_date: object,
    artifact: dict[str, Any],
    cutoff: date = DEFAULT_POLICY_CUTOFF,
) -> tuple[bool, str]:
    published = parse_document_date(publication_date)
    if published is None:
        return False, "unresolved_publication_date"
    if published >= cutoff:
        return True, "published_on_or_after_cutoff"
    normalized = normalize_document_number(document_number)
    if not normalized:
        return False, "pre_cutoff_missing_document_number"
    if normalized in allowlist_numbers(artifact):
        return True, "pre_cutoff_document_number_allowlisted"
    return False, "pre_cutoff_document_number_not_allowlisted"
