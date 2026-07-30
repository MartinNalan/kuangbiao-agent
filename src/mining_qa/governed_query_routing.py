"""Deterministic, auditable query routing rules accepted through v4 experiments.

This module owns only high-confidence transformations whose boundaries have
been reviewed and tested.  It never invents an answer, a standard number or a
hard document filter.  Lexical and semantic query text remain separate so an
intent-specific terminology mapping cannot silently steer vector retrieval.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re


ROUTER_VERSION = "governed-query-routing.v1"
SAND_GOLD_MAPPING_ID = "mineral.sand_gold.standard_family.v1"

_STANDARD_SELECTOR_RE = re.compile(
    r"(?:哪|何)(?:一)?(?:个|项|部)?(?:现行)?(?:行业|国家|地质勘查|勘查)?"
    r"(?:标准|规范|规程)"
)
_STANDARD_APPLICABILITY_ACTIONS = (
    "适用",
    "采用",
    "使用",
    "执行",
    "依据",
    "按照",
    "按哪个",
    "按哪项",
    "规定",
)
_SAND_GOLD_TECHNICAL_DETAIL_TERMS = (
    "工业指标",
    "边界品位",
    "最低工业品位",
    "品位",
    "最小可采厚度",
    "夹石剔除厚度",
    "厚度",
    "化学分析",
    "基本分析",
    "组合分析",
    "光谱分析",
    "分析",
    "分析项目",
    "取样",
    "采样",
    "样品",
    "缩分",
    "K值",
    "内检",
    "外检",
    "检查比例",
    "工程间距",
    "资源量估算",
    "储量估算",
    "资源储量估算",
    "估算方法",
)
_SAND_GOLD_AMBIGUOUS_PATTERNS = (
    re.compile(r"砂金(?:矿)?(?:怎么|如何)(?:勘查|评价)(?:[？?。]?$)"),
    re.compile(r"砂金(?:矿)?有什么要求(?:[？?。]?$)"),
    re.compile(r"砂金(?:矿)?按什么要求(?:执行)?(?:[？?。]?$)"),
)


@dataclass(frozen=True)
class GovernedQueryRoute:
    router_version: str
    original_question: str
    canonical_question: str
    lexical_query: str
    semantic_query: str
    governed_intent: str
    mapping_id: str | None
    mapping_applied: bool
    alias_triggered: bool
    retrieval_allowed: bool
    confirmation_required: bool
    confirmation_prompt: str | None
    normalizations: tuple[tuple[str, str], ...]
    audit_reasons: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return asdict(self)


def _clean_question(value: str) -> str:
    # Upstream query normalization deliberately preserves domain-significant
    # forms such as Roman numerals (Ⅰ/Ⅱ/Ⅲ).  This router only owns the reviewed
    # sand-gold aliases, so it must not perform an unrelated Unicode rewrite.
    return re.sub(r"\s+", " ", value or "").strip()


def _canonicalize_sand_gold(question: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    if "沙金" not in question:
        return question, ()
    return question.replace("沙金", "砂金"), (("沙金", "砂金"),)


def _is_sand_gold_technical_detail(question: str) -> bool:
    return any(term in question for term in _SAND_GOLD_TECHNICAL_DETAIL_TERMS)


def _is_standard_applicability(question: str) -> bool:
    return bool(
        _STANDARD_SELECTOR_RE.search(question)
        and any(action in question for action in _STANDARD_APPLICABILITY_ACTIONS)
    )


def _is_ambiguous_sand_gold_scope(question: str) -> bool:
    compact = re.sub(r"\s+", "", question)
    return any(pattern.search(compact) for pattern in _SAND_GOLD_AMBIGUOUS_PATTERNS)


def _standard_family_query(question: str) -> str:
    return question.replace("砂金矿", "金属砂矿类").replace("砂金", "金属砂矿类")


def route_governed_query(question: str) -> GovernedQueryRoute:
    """Return the deterministic route without executing retrieval."""

    original = _clean_question(question)
    canonical, normalizations = _canonicalize_sand_gold(original)
    alias_triggered = "砂金" in canonical
    governed_intent = "other"
    mapping_id: str | None = None
    mapping_applied = False
    retrieval_allowed = True
    confirmation_required = False
    confirmation_prompt: str | None = None
    lexical_query = canonical
    reasons: list[str] = []

    if alias_triggered:
        if _is_sand_gold_technical_detail(canonical):
            governed_intent = "technical_detail"
            reasons.append("technical_detail_guard_prevents_standard_family_mapping")
        elif _is_ambiguous_sand_gold_scope(canonical):
            governed_intent = "ambiguous"
            retrieval_allowed = False
            confirmation_required = True
            confirmation_prompt = (
                "请确认您需要查找适用标准，还是一般工业指标、取样分析质量或资源储量估算等具体技术要求。"
            )
            reasons.append("ambiguous_scope_requires_user_confirmation")
        elif _is_standard_applicability(canonical):
            governed_intent = "standard_applicability"
            mapping_id = SAND_GOLD_MAPPING_ID
            mapping_applied = True
            lexical_query = _standard_family_query(canonical)
            reasons.append("explicit_standard_applicability_allows_standard_family_mapping")
        else:
            reasons.append("alias_normalized_without_category_expansion")

    return GovernedQueryRoute(
        router_version=ROUTER_VERSION,
        original_question=original,
        canonical_question=canonical,
        lexical_query=lexical_query,
        semantic_query=canonical,
        governed_intent=governed_intent,
        mapping_id=mapping_id,
        mapping_applied=mapping_applied,
        alias_triggered=alias_triggered,
        retrieval_allowed=retrieval_allowed,
        confirmation_required=confirmation_required,
        confirmation_prompt=confirmation_prompt,
        normalizations=normalizations,
        audit_reasons=tuple(reasons),
    )


def sand_gold_confirmation_options() -> tuple[dict[str, str], ...]:
    """Return deterministic, mutually distinct scopes for ambiguous questions."""

    return (
        {
            "option_id": "sand_gold_standard",
            "label": "适用标准",
            "question": "砂金地质勘查及成果评价适用哪个标准？",
            "description": "查找应执行的现行勘查标准及其适用范围。",
        },
        {
            "option_id": "sand_gold_indicators",
            "label": "工业指标",
            "question": "砂金矿一般工业指标有哪些具体要求？",
            "description": "查找品位、厚度等一般工业指标。",
        },
        {
            "option_id": "sand_gold_sampling",
            "label": "取样分析",
            "question": "砂金取样、分析及质量检查有哪些具体要求？",
            "description": "查找取样、分析、内检和外检要求。",
        },
        {
            "option_id": "sand_gold_estimation",
            "label": "资源量估算",
            "question": "砂金资源储量估算有哪些具体要求？",
            "description": "查找资源储量估算方法和条件。",
        },
    )
