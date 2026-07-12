from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASE_LEXICON_PATH = Path(__file__).with_name("domain_lexicon.json")
DEFAULT_RUNTIME_LEXICON_PATH = PROJECT_ROOT / "data" / "app" / "domain_lexicon_runtime.json"
GENERIC_ACTION_EXPRESSIONS = {
    "去哪个机构",
    "去哪里申请",
    "在哪里备案",
    "找谁办",
    "去哪办",
    "哪办",
    "谁负责",
    "怎么办",
    "要交什么",
}

_CACHE_LOCK = threading.Lock()
_CACHE_SIGNATURE: tuple[object, ...] | None = None
_CACHE_ENTRIES: tuple[dict[str, Any], ...] = ()


def runtime_lexicon_path() -> Path:
    configured = os.getenv("DOMAIN_LEXICON_RUNTIME_PATH", "").strip()
    return Path(configured) if configured else DEFAULT_RUNTIME_LEXICON_PATH


def _path_signature(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (str(path), -1, -1)
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _clean_string_list(value: object, *, limit: int = 40, item_limit: int = 160) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for raw in value:
        item = re.sub(r"\s+", " ", str(raw or "")).strip()[:item_limit]
        if item and item not in items:
            items.append(item)
        if len(items) >= limit:
            break
    return items


def normalize_lexicon_entry(raw: dict[str, Any]) -> dict[str, Any]:
    entry = dict(raw)
    entry["lexicon_id"] = str(entry.get("lexicon_id") or "").strip()
    entry["user_expression"] = re.sub(
        r"\s+", " ", str(entry.get("user_expression") or "")
    ).strip()[:120]
    entry["canonical_term"] = re.sub(
        r"\s+", " ", str(entry.get("canonical_term") or "")
    ).strip()[:200]
    entry["intent_label"] = str(entry.get("intent_label") or "general").strip()[:80]
    entry["domain"] = str(entry.get("domain") or "solid_mineral").strip()[:80]
    entry["aliases"] = _clean_string_list(entry.get("aliases"), limit=30, item_limit=120)
    entry["positive_expansions"] = _clean_string_list(
        entry.get("positive_expansions"), limit=40, item_limit=160
    )
    entry["negative_terms"] = _clean_string_list(
        entry.get("negative_terms"), limit=40, item_limit=160
    )
    entry["evidence_required_patterns"] = _clean_string_list(
        entry.get("evidence_required_patterns"), limit=30, item_limit=160
    )
    entry["required_context_terms"] = _clean_string_list(
        entry.get("required_context_terms"), limit=30, item_limit=100
    )
    entry["forbidden_context_terms"] = _clean_string_list(
        entry.get("forbidden_context_terms"), limit=30, item_limit=100
    )
    entry["match_type"] = (
        str(entry.get("match_type") or "phrase")
        if str(entry.get("match_type") or "phrase") in {"phrase", "exact"}
        else "phrase"
    )
    entry["domain_gate_enabled"] = bool(entry.get("domain_gate_enabled", True))
    entry["intent_trigger_enabled"] = bool(entry.get("intent_trigger_enabled", True))
    entry["priority"] = max(0, min(100, int(entry.get("priority") or 0)))
    entry["risk_level"] = (
        str(entry.get("risk_level") or "medium")
        if str(entry.get("risk_level") or "medium") in {"low", "medium", "high"}
        else "medium"
    )
    entry["status"] = str(entry.get("status") or "active")
    entry["origin"] = str(entry.get("origin") or "builtin")
    entry["version"] = max(1, int(entry.get("version") or 1))
    return entry


def _read_entries(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    entries: list[dict[str, Any]] = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        entry = normalize_lexicon_entry(raw)
        if entry["lexicon_id"] and entry["user_expression"] and entry["canonical_term"]:
            entries.append(entry)
    return entries


def base_domain_lexicon() -> list[dict[str, Any]]:
    return _read_entries(BASE_LEXICON_PATH)


def clear_domain_lexicon_cache() -> None:
    global _CACHE_SIGNATURE, _CACHE_ENTRIES
    with _CACHE_LOCK:
        _CACHE_SIGNATURE = None
        _CACHE_ENTRIES = ()


def domain_lexicon(*, include_inactive: bool = False) -> list[dict[str, Any]]:
    global _CACHE_SIGNATURE, _CACHE_ENTRIES
    runtime_path = runtime_lexicon_path()
    signature = (_path_signature(BASE_LEXICON_PATH), _path_signature(runtime_path))
    with _CACHE_LOCK:
        if signature != _CACHE_SIGNATURE:
            merged = {entry["lexicon_id"]: entry for entry in base_domain_lexicon()}
            for entry in _read_entries(runtime_path):
                merged[entry["lexicon_id"]] = entry
            _CACHE_ENTRIES = tuple(
                sorted(
                    merged.values(),
                    key=lambda entry: (-int(entry.get("priority") or 0), entry["lexicon_id"]),
                )
            )
            _CACHE_SIGNATURE = signature
        entries = _CACHE_ENTRIES
    if include_inactive:
        return [dict(entry) for entry in entries]
    return [dict(entry) for entry in entries if entry.get("status") == "active"]


def _entry_matches(query: str, entry: dict[str, Any], purpose: str) -> bool:
    if entry.get("status") != "active":
        return False
    if purpose == "domain_gate" and not entry.get("domain_gate_enabled", True):
        return False
    if purpose == "intent" and not entry.get("intent_trigger_enabled", True):
        return False
    required_context = entry.get("required_context_terms") or []
    if required_context and not any(term in query for term in required_context):
        return False
    forbidden_context = entry.get("forbidden_context_terms") or []
    if any(term in query for term in forbidden_context):
        return False
    probes = [entry.get("user_expression"), *(entry.get("aliases") or [])]
    if purpose != "domain_gate":
        probes.append(entry.get("canonical_term"))
    probes = [str(probe).strip() for probe in probes if str(probe or "").strip()]
    if entry.get("match_type") == "exact":
        return any(query.strip() == probe for probe in probes)
    return any(probe in query for probe in probes)


def matched_lexicon_entries(
    query: str,
    intent_label: str | None = None,
    *,
    purpose: str = "intent",
    entries: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    source = list(entries) if entries is not None else domain_lexicon()
    for raw in source:
        entry = normalize_lexicon_entry(raw)
        if intent_label and entry.get("intent_label") != intent_label:
            continue
        if _entry_matches(query, entry, purpose):
            matches.append(entry)
    return sorted(matches, key=lambda entry: int(entry.get("priority") or 0), reverse=True)


def lexicon_query_expansions(
    query: str,
    *,
    entries: Iterable[dict[str, Any]] | None = None,
) -> list[str]:
    expansions: list[str] = []
    for entry in matched_lexicon_entries(query, purpose="retrieval", entries=entries):
        expansions.append(str(entry.get("canonical_term") or ""))
        expansions.extend(str(term) for term in (entry.get("positive_expansions") or []) if term)
    return list(dict.fromkeys(term for term in expansions if term))


def lexicon_negative_terms(
    query: str,
    intent_label: str | None = None,
    *,
    entries: Iterable[dict[str, Any]] | None = None,
) -> list[str]:
    terms: list[str] = []
    for entry in matched_lexicon_entries(
        query,
        intent_label=intent_label,
        purpose="retrieval",
        entries=entries,
    ):
        terms.extend(str(term) for term in (entry.get("negative_terms") or []) if term)
    return list(dict.fromkeys(terms))


def lexicon_evidence_patterns(
    query: str,
    *,
    entries: Iterable[dict[str, Any]] | None = None,
) -> list[str]:
    patterns: list[str] = []
    for entry in matched_lexicon_entries(query, purpose="retrieval", entries=entries):
        patterns.extend(
            str(pattern)
            for pattern in (entry.get("evidence_required_patterns") or [])
            if pattern
        )
    return list(dict.fromkeys(patterns))


def query_has_intent(query: str, intent_label: str) -> bool:
    return bool(matched_lexicon_entries(query, intent_label=intent_label, purpose="intent"))


def governed_domain_terms() -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            entry["user_expression"]
            for entry in domain_lexicon()
            if entry.get("domain_gate_enabled", True)
        )
    )


def lexicon_match_summary(
    query: str,
    entries: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    source = [normalize_lexicon_entry(entry) for entry in entries]
    intent_matches = matched_lexicon_entries(query, purpose="intent", entries=source)
    gate_matches = matched_lexicon_entries(query, purpose="domain_gate", entries=source)
    retrieval_matches = matched_lexicon_entries(query, purpose="retrieval", entries=source)
    return {
        "domain_gate_passed": bool(gate_matches),
        "domain_matches": [
            {
                "lexicon_id": entry["lexicon_id"],
                "user_expression": entry["user_expression"],
                "canonical_term": entry["canonical_term"],
            }
            for entry in gate_matches
        ],
        "intent_matches": [
            {
                "lexicon_id": entry["lexicon_id"],
                "user_expression": entry["user_expression"],
                "canonical_term": entry["canonical_term"],
                "intent_label": entry["intent_label"],
            }
            for entry in intent_matches
        ],
        "retrieval_matches": [
            {
                "lexicon_id": entry["lexicon_id"],
                "user_expression": entry["user_expression"],
                "canonical_term": entry["canonical_term"],
            }
            for entry in retrieval_matches
        ],
        "expansions": lexicon_query_expansions(query, entries=source),
        "evidence_patterns": lexicon_evidence_patterns(query, entries=source),
    }


def lexicon_candidate_warnings(candidate: dict[str, Any], active_entries: Iterable[dict[str, Any]]) -> list[str]:
    entry = normalize_lexicon_entry(
        {
            **candidate,
            "lexicon_id": candidate.get("lexicon_id") or candidate.get("target_lexicon_id") or "",
        }
    )
    warnings: list[str] = []
    expression = entry["user_expression"]
    if entry["domain_gate_enabled"] and (
        expression in GENERIC_ACTION_EXPRESSIONS or len(expression) <= 2
    ) and not entry["required_context_terms"]:
        warnings.append("该表达较通用；启用领域门控前应配置必要上下文词。")
    if not candidate.get("positive_examples"):
        warnings.append("尚未提供正向示例。")
    if not candidate.get("negative_examples"):
        warnings.append("尚未提供容易误判的反例。")
    if not entry["domain_gate_enabled"] and not entry["intent_trigger_enabled"] and not any(
        entry.get(field)
        for field in ("positive_expansions", "negative_terms", "evidence_required_patterns")
    ):
        warnings.append("该候选无运行作用：未启用门控或意图，也未配置检索与证据约束。")
    for existing in active_entries:
        normalized = normalize_lexicon_entry(existing)
        if normalized["lexicon_id"] == entry["lexicon_id"]:
            continue
        probes = {normalized["user_expression"], *normalized["aliases"]}
        candidate_probes = {entry["user_expression"], *entry["aliases"]}
        if probes & candidate_probes and (
            normalized["canonical_term"] != entry["canonical_term"]
            or normalized["intent_label"] != entry["intent_label"]
        ):
            warnings.append(
                f"与现有词条 {normalized['lexicon_id']} 的表达重叠，但规范术语或意图不同。"
            )
    return list(dict.fromkeys(warnings))


def publish_runtime_lexicon(entries: Iterable[dict[str, Any]], path: Path | None = None) -> Path:
    target = path or runtime_lexicon_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    normalized = [normalize_lexicon_entry(entry) for entry in entries]
    normalized.sort(key=lambda entry: (-entry["priority"], entry["lexicon_id"]))
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    clear_domain_lexicon_cache()
    return target
