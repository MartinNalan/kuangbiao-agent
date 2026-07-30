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
DEPARTMENTAL_ALLOWLIST_AUTHORITY_LEVELS = frozenset({"部门规范性文件"})
UPPER_LAW_AUTHORITY_LEVELS = frozenset({"法律", "行政法规"})
CURRENT_STATUS_VALUES = frozenset({"current", "active", "现行", "现行有效", "有效"})
REPEALED_STATUS_VALUES = frozenset({"repealed", "deprecated", "废止", "废止/失效", "失效"})


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


def authority_governance_class(
    authority_level: object,
    document_type: object = None,
) -> str:
    """Return the validity regime that governs a policy-library record.

    The workbook behind ``DEFAULT_ALLOWLIST_ARTIFACT`` is a departmental
    normative-document list.  It is not negative validity evidence for laws,
    administrative regulations, State Council documents, departmental rules,
    or judicial interpretations.
    """
    level = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(authority_level or "")))
    doc_type = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(document_type or ""))).lower()
    if level in UPPER_LAW_AUTHORITY_LEVELS:
        return "upper_law_or_administrative_regulation"
    if level in DEPARTMENTAL_ALLOWLIST_AUTHORITY_LEVELS:
        return "departmental_normative_document"
    if level:
        return "outside_departmental_allowlist_scope"
    if doc_type in {"law", "regulation", "administrative_regulation"}:
        return "upper_law_or_administrative_regulation"
    return "authority_classification_required"


def explicit_status_class(value: object) -> str:
    normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).lower()
    if normalized in CURRENT_STATUS_VALUES:
        return "current"
    if normalized in REPEALED_STATUS_VALUES:
        return "repealed"
    return "unverified"


def policy_is_allowed(
    document_number: object,
    publication_date: object,
    artifact: dict[str, Any],
    cutoff: date = DEFAULT_POLICY_CUTOFF,
    *,
    authority_level: object = None,
    document_type: object = None,
    effective_status: object = None,
    official_source_verified: bool = False,
) -> tuple[bool, str]:
    governance_class = authority_governance_class(authority_level, document_type)
    status_class = explicit_status_class(effective_status)

    if governance_class == "upper_law_or_administrative_regulation":
        if not official_source_verified:
            return False, "upper_law_requires_competent_official_source"
        if status_class == "current":
            return True, "verified_current_upper_law_outside_departmental_allowlist_scope"
        if status_class == "repealed":
            return False, "explicitly_repealed_upper_law"
        return False, "upper_law_requires_explicit_current_or_repeal_evidence"

    if governance_class == "outside_departmental_allowlist_scope":
        if not official_source_verified:
            return False, "out_of_scope_document_requires_competent_official_source"
        if status_class == "current":
            return True, "verified_current_document_outside_departmental_allowlist_scope"
        if status_class == "repealed":
            return False, "explicitly_repealed_document_outside_departmental_allowlist_scope"
        return False, "out_of_scope_document_requires_explicit_current_or_repeal_evidence"

    if governance_class != "departmental_normative_document":
        return False, "authority_classification_required_before_allowlist_evaluation"

    published = parse_document_date(publication_date)
    if published is None:
        return False, "departmental_document_unresolved_publication_date"
    if published >= cutoff:
        return True, "departmental_document_published_on_or_after_allowlist_cutoff"
    normalized = normalize_document_number(document_number)
    if not normalized:
        return False, "pre_cutoff_departmental_document_missing_document_number"
    if normalized in allowlist_numbers(artifact):
        return True, "pre_cutoff_departmental_document_number_allowlisted"
    return False, "pre_cutoff_departmental_document_number_not_allowlisted"
