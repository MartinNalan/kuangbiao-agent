from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Iterable

from .mnr_policy_allowlist import normalize_document_number


AUTHORITY_ANCHOR_NONE = "none"
AUTHORITY_ANCHOR_STRICT = "strict_authority"
AUTHORITY_ANCHOR_STATUS = "status_lookup"
AUTHORITY_ANCHOR_RELATION = "comparison_or_relation"
AUTHORITY_ANCHOR_INCIDENTAL = "incidental_mention"

_STATUS_MARKERS = (
    "是否现行",
    "是否存在",
    "有没有这个标准",
    "是否废止",
    "是否有效",
    "还有效",
    "现行状态",
    "有效状态",
    "被哪个标准替代",
    "由哪个标准替代",
)
_RELATION_MARKERS = (
    "比较",
    "区别",
    "差异",
    "异同",
    "关系",
    "关联",
    "衔接",
    "同时适用",
    "哪个优先",
    "优先适用",
    "相对于",
)
_STRICT_MARKERS = (
    "严格依据",
    "仅依据",
    "只依据",
    "请依据",
    "必须依据",
    "以该标准为准",
    "按照该标准",
    "根据该标准",
)
_DIRECT_PROVISION_MARKERS = (
    "如何规定",
    "怎么规定",
    "怎样规定",
    "规定了什么",
    "具体规定",
    "具体要求",
    "条款内容",
)
_CLAUSE_MARKER_RE = re.compile(
    r"第\s*(?:\d+(?:\.\d+)*|[零〇一二三四五六七八九十百两]+)\s*(?:条|款|项|章|节)",
    flags=re.IGNORECASE,
)
_ARABIC_CLAUSE_RE = re.compile(
    r"第\s*(?P<value>\d+(?:\.\d+)*)\s*(?:条|款|项|章|节)",
    flags=re.IGNORECASE,
)
_CHINESE_CLAUSE_RE = re.compile(
    r"第\s*(?P<value>[零〇一二三四五六七八九十百两]+)\s*(?:条|款|项|章|节)"
)
_LETTER_CLAUSE_RE = re.compile(
    r"(?<![A-Z0-9/])(?P<value>[A-Z]\.\d+(?:\.\d+)*)\s*(?:条|款|项|章|节)?",
    flags=re.IGNORECASE,
)
_SOURCE_ARABIC_RE = re.compile(r"(?<![A-Z0-9/-])(?:[A-Z]\.)?\d+(?:\.\d+)*(?![A-Z0-9/-])", re.IGNORECASE)


@dataclass(frozen=True)
class AuthorityAnchor:
    mode: str = AUTHORITY_ANCHOR_NONE
    standard_numbers: tuple[str, ...] = ()
    clause_refs: tuple[str, ...] = ()
    scope_origin: str = "none"

    @property
    def is_strict(self) -> bool:
        return self.mode == AUTHORITY_ANCHOR_STRICT

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AuthorityCatalogDecision:
    anchor: AuthorityAnchor
    proceed: bool
    filter_standard_numbers: tuple[str, ...] = ()
    matched_standard_numbers: tuple[str, ...] = ()
    missing_standard_numbers: tuple[str, ...] = ()
    blocked_standard_numbers: tuple[str, ...] = ()
    reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["anchor"] = self.anchor.to_payload()
        return payload


def classify_authority_anchor(
    question: str,
    standard_numbers: Iterable[str],
    *,
    scope_origin: str,
) -> AuthorityAnchor:
    numbers = tuple(dict.fromkeys(str(value).strip() for value in standard_numbers if str(value).strip()))
    if not numbers or scope_origin != "user":
        return AuthorityAnchor(scope_origin=scope_origin)

    compact = re.sub(r"\s+", "", question or "")
    clauses = extract_requested_clause_refs(question)
    if any(marker in compact for marker in _STATUS_MARKERS):
        mode = AUTHORITY_ANCHOR_STATUS
    elif any(marker in compact for marker in _RELATION_MARKERS):
        mode = AUTHORITY_ANCHOR_RELATION
    elif (
        any(marker in compact for marker in _STRICT_MARKERS)
        or bool(clauses)
        or (
            any(marker in compact for marker in _DIRECT_PROVISION_MARKERS)
            and bool(_CLAUSE_MARKER_RE.search(compact) or numbers)
        )
    ):
        mode = AUTHORITY_ANCHOR_STRICT
    else:
        mode = AUTHORITY_ANCHOR_INCIDENTAL
    return AuthorityAnchor(
        mode=mode,
        standard_numbers=numbers,
        clause_refs=clauses,
        scope_origin=scope_origin,
    )


def evaluate_authority_catalog(
    anchor: AuthorityAnchor,
    catalog_items: Iterable[Any],
) -> AuthorityCatalogDecision:
    if not anchor.is_strict:
        return AuthorityCatalogDecision(anchor=anchor, proceed=True)

    items = list(catalog_items)
    matched: list[str] = []
    missing: list[str] = []
    blocked: list[str] = []
    eligible: list[str] = []
    for requested in anchor.standard_numbers:
        normalized_requested = normalize_document_number(requested)
        matches = [
            item
            for item in items
            if normalize_document_number(_item_value(item, "standard_no"))
            == normalized_requested
        ]
        if not matches:
            missing.append(requested)
            continue
        matched.append(requested)
        answerable = [item for item in matches if _catalog_item_can_answer(item)]
        if not answerable:
            blocked.append(requested)
            continue
        eligible.append(requested)

    if missing:
        reason = "当前知识库无法核验用户指定的标准号：" + "、".join(missing)
    elif blocked:
        reason = "用户指定的标准已收录，但当前不能作为回答依据：" + "、".join(blocked)
    else:
        reason = None
    proceed = not missing and not blocked and len(eligible) == len(anchor.standard_numbers)
    return AuthorityCatalogDecision(
        anchor=anchor,
        proceed=proceed,
        filter_standard_numbers=tuple(eligible) if proceed else (),
        matched_standard_numbers=tuple(matched),
        missing_standard_numbers=tuple(missing),
        blocked_standard_numbers=tuple(blocked),
        reason=reason,
    )


def strict_sources_satisfy_anchor(
    anchor: AuthorityAnchor,
    sources: Iterable[Any],
) -> bool:
    if not anchor.is_strict:
        return True
    source_items = list(sources)
    if not source_items:
        return False
    allowed = {
        normalize_document_number(value)
        for value in anchor.standard_numbers
        if normalize_document_number(value)
    }
    source_numbers = [
        normalize_document_number(_item_value(source, "standard_no"))
        for source in source_items
    ]
    if not source_numbers or any(not value or value not in allowed for value in source_numbers):
        return False
    if not allowed.issubset(set(source_numbers)):
        return False
    if not anchor.clause_refs:
        return True
    source_refs = tuple(
        ref
        for source in source_items
        for ref in extract_source_clause_refs(
            str(
                _item_value(source, "chapter")
                or _item_value(source, "clause_no")
                or _item_value(source, "section_path")
                or ""
            )
        )
    )
    return all(any(_clause_ref_matches(target, actual) for actual in source_refs) for target in anchor.clause_refs)


def filter_sources_to_anchor(anchor: AuthorityAnchor, sources: Iterable[Any]) -> list[Any]:
    source_items = list(sources)
    if not anchor.is_strict:
        return source_items
    allowed = {
        normalize_document_number(value)
        for value in anchor.standard_numbers
        if normalize_document_number(value)
    }
    return [
        source
        for source in source_items
        if normalize_document_number(_item_value(source, "standard_no")) in allowed
    ]


def extract_requested_clause_refs(text: str) -> tuple[str, ...]:
    refs: list[str] = []
    for match in _ARABIC_CLAUSE_RE.finditer(text or ""):
        refs.append(_normalize_clause_ref(match.group("value")))
    for match in _CHINESE_CLAUSE_RE.finditer(text or ""):
        number = _chinese_integer(match.group("value"))
        if number is not None:
            refs.append(str(number))
    for match in _LETTER_CLAUSE_RE.finditer(text or ""):
        refs.append(_normalize_clause_ref(match.group("value")))
    return tuple(dict.fromkeys(ref for ref in refs if ref))


def extract_source_clause_refs(text: str) -> tuple[str, ...]:
    requested = list(extract_requested_clause_refs(text))
    requested.extend(
        _normalize_clause_ref(match.group(0))
        for match in _SOURCE_ARABIC_RE.finditer(text or "")
    )
    return tuple(dict.fromkeys(ref for ref in requested if ref))


def _catalog_item_can_answer(item: Any) -> bool:
    status = str(_item_value(item, "status") or "").strip().lower()
    if any(marker in status for marker in ("废止", "失效", "repealed", "withdrawn")):
        return False
    return bool(_item_value(item, "can_answer"))


def _item_value(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _normalize_clause_ref(value: str) -> str:
    normalized = re.sub(r"\s+", "", value or "").upper().strip(".。")
    return normalized


def _clause_ref_matches(target: str, actual: str) -> bool:
    target = _normalize_clause_ref(target)
    actual = _normalize_clause_ref(actual)
    return actual == target or actual.startswith(target + ".")


def _chinese_integer(value: str) -> int | None:
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100}
    if not value:
        return None
    if all(char in digits for char in value):
        return int("".join(str(digits[char]) for char in value))
    total = 0
    current = 0
    for char in value:
        if char in digits:
            current = digits[char]
        elif char in units:
            unit = units[char]
            total += (current or 1) * unit
            current = 0
        else:
            return None
    return total + current
