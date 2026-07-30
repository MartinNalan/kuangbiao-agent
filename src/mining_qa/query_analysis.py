"""Versioned, provider-neutral query analysis for controlled retrieval.

DeepSeek proposes a semantic interpretation.  This module is the deterministic
boundary that preserves the original question, validates model additions, locks
only explicit or approved one-to-one anchors, and compiles provider-specific
text into a retrieval plan.  It does not execute retrieval or generate answers.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .governed_query_routing import route_governed_query


SCHEMA_VERSION = "query_analysis.v1"
PROMPT_VERSION = "query-analysis-deepseek-v2"

QUERY_ANALYSIS_SYSTEM_PROMPT = """你是 GeoWiki 矿产资源知识库的问题分析器。你只分析问题，不回答问题，也不引用或编造答案。

目标：在完整保留原问题事实的前提下，输出自然语言改写、显式锚点、候选推断、歧义、关键词、语义子问题和答案所需证据。输出将由程序校验并编译成精确检索、FTS/BM25 和未来向量检索共同使用的查询计划。

必须遵守：
1. original_question 由程序保存，你不得用改写覆盖原问题。
2. rewritten_question 必须是独立、完整的问句；保留主体、对象、矿种、权利或证件、条件、时间状态、否定、数字、目的和机关关系。
   权利与证件是不同概念：采矿权不得改写为采矿许可证，探矿权不得改写为勘查许可证；反向也不得替换。
3. 不得输出答案、处罚结论、资源量类型结论、办理结论或机构结论。
4. 原问题明示的事实标为 explicit；模型补充的解释标为 inferred。不得把 inferred 伪装成 explicit。
   explicit 的 source_text 必须是原问题中短小、原子化、连续且逐字一致的片段；不得把不连续词语拼接成 source_text，也不得在 source_text 中先行改写。
5. 模型推断的文件、条款、办理事项或机关只能作为 unverified 搜索假设，不能作为硬过滤条件。
6. 一对多或会改变检索范围的推断必须 needs_confirmation=true，并写入 ambiguities 或 search_hypotheses。
   仅用于扩大召回、且不排除其他情形的普通并行搜索 needs_confirmation=false；可以通过并行搜索覆盖的宽泛问题不要强迫用户先确认。
   对“承担什么责任”“有什么责任”“哪些法律责任”等未限定责任类型的问题，默认检索全部可能适用的责任类型，
   不要求用户先在行政责任、刑事责任、民事责任等类型中选择；这些类型只能作为并行搜索方向，最终以条款证据为准。
7. source_preferences 只是软偏好，不得排除任何治理合格的材料；不要输出 basis 字段。
8. 规范性文件、标准、办事指南和解读材料可以按问题性质列为 primary 或 supplementary，但 exclude_by_type 必须为空。
9. evidence_requirements 说明最终证据必须覆盖什么，不要写答案。
10. 对矿石加工选冶试验程度问题，必须把适用矿种勘查规范与 DZ/T 0340《矿产勘查矿石加工选冶技术性能试验研究程度要求》作为并行证据方向；证据需求应保留勘查阶段、矿石选冶难易程度、资源量规模和所比较的试验等级。只有规范原文明示前置、包含或层级关系时，才能提出“高级试验覆盖低级试验”的搜索假设；不得仅凭试验名称或模型记忆推断。两类规范不一致时必须保留冲突，不得自行消解。
11. 输出前逐项对照原问题，自检遗漏事实和新增假设。answer_generated 必须为 false。

安全机械规范化示例：采矿证->采矿许可证；“1类型/I类型/一类型”统一为“类型Ⅰ”（Ⅱ、Ⅲ同理）；第8.2.3条可增加检索别名8.2.3，但原始条款锚点仍须保留。不要生成“类型1”。文件书名号的有无、阶段词“详查阶段/详查”等只属于检索别名，不需要用户确认。

限制：search_hypotheses 和 semantic_subqueries 各不超过3条。不要提出原问题不需要的猜测，例如在只问工程间距时擅自猜测是钻孔间距。只有缺失信息会导致不同法律状态、办理事项、权限、适用范围或结论时，才设置 material=true 并生成确认问题。

只返回严格 JSON 对象：
{
  "items": [{
    "query_id": "...",
    "rewritten_question": "完整问句？",
    "question_type": ["可扩展的英文或拼音标识"],
    "anchors": [{
      "type": "可扩展锚点类型",
      "source_text": "原问题中的连续原文；推断项可为空",
      "normalized_value": "规范表达或候选解释",
      "interpretation": "explicit|inferred",
      "needs_confirmation": false
    }],
    "ambiguities": [{
      "field": "字段名",
      "description": "歧义说明",
      "material": true,
      "confirmation_question": "必要时向用户确认的问题"
    }],
    "search_hypotheses": [{
      "query": "仅用于增加并行搜索的假设",
      "purpose": "为什么要搜索",
      "needs_confirmation": false
    }],
    "source_preferences": {
      "primary": ["law|regulation|policy_document|service_guide|technical_standard|interpretive_material 等"],
      "supplementary": [],
      "exclude_by_type": []
    },
    "lexical_terms": ["只列检索词，不写答案"],
    "semantic_subqueries": [{"query": "完整问句？", "purpose": "证据目标"}],
    "evidence_requirements": [{
      "description": "需要证据回答的事项",
      "required_elements": ["subject|condition|action|authority|legal_effect|number|exception 等"],
      "must_have_clause_level_evidence": true
    }],
    "self_check": {
      "preserved_original_facts": ["已保留的原问题事实"],
      "missing_original_facts": [],
      "added_assumptions": ["所有模型新增解释"],
      "answer_generated": false
    }
  }]
}
"""


class AnchorState(StrEnum):
    LOCKED = "locked"
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class AnchorOrigin(StrEnum):
    ORIGINAL_EXPLICIT = "original_explicit"
    PROGRAM_DETECTED = "program_detected"
    PROGRAM_NORMALIZED = "program_normalized"
    MODEL_INFERRED = "model_inferred"
    USER_CONFIRMED = "user_confirmed"


class RetrievalAction(StrEnum):
    HARD_FILTER_EXACT_ROUTE = "hard_filter_exact_route"
    CANDIDATE_RETENTION = "candidate_retention"
    RANKING_BOOST = "ranking_boost"
    PARALLEL_SEARCH_ONLY = "parallel_search_only"
    EVIDENCE_VERIFICATION = "evidence_verification"


STRICT_MODEL_CONFIG = ConfigDict(extra="forbid")


class QueryMetadata(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    schema_version: str = SCHEMA_VERSION
    prompt_version: str = PROMPT_VERSION
    prompt_sha256: str
    model: str
    generated_at: str
    original_question_sha256: str


class PrivacyPolicy(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    raw_question_storage: str = "runtime_or_private_evaluation_only"
    production_log_policy: str = "hash_and_structural_metadata_only"
    sensitive_data_detected: bool = False
    detected_categories: list[str] = Field(default_factory=list)


class Normalization(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    source_text: str
    target_text: str
    status: str = "approved_one_to_one"
    origin: AnchorOrigin = AnchorOrigin.PROGRAM_NORMALIZED


class Anchor(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    anchor_id: str
    type: str
    raw_text: str
    canonical_text: str
    state: AnchorState
    origin: AnchorOrigin
    needs_confirmation: bool = False
    retrieval_actions: list[RetrievalAction] = Field(default_factory=list)


class Ambiguity(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    field: str
    description: str
    material: bool = True
    confirmation_question: str = ""


class SearchHypothesis(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    hypothesis_id: str
    query: str
    purpose: str
    status: str = "unverified"
    search_only: bool = True
    needs_confirmation: bool = False
    origin: AnchorOrigin = AnchorOrigin.MODEL_INFERRED


class SourcePreferences(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    primary: list[str] = Field(default_factory=list)
    supplementary: list[str] = Field(default_factory=list)
    exclude_by_type: list[str] = Field(default_factory=list)
    routing_mode: str = "prefer_not_exclude"

    @model_validator(mode="after")
    def forbid_type_exclusion(self) -> "SourcePreferences":
        if self.exclude_by_type:
            raise ValueError("source_preferences.exclude_by_type must remain empty")
        if self.routing_mode != "prefer_not_exclude":
            raise ValueError("source preferences must remain soft")
        return self


class EvidenceRequirement(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    requirement_id: str
    description: str
    required_elements: list[str] = Field(default_factory=list)
    must_have_clause_level_evidence: bool = True


class QuerySelfCheck(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    preserved_original_facts: list[str] = Field(default_factory=list)
    missing_original_facts: list[str] = Field(default_factory=list)
    added_assumptions: list[str] = Field(default_factory=list)
    answer_generated: bool = False


class HardFilter(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    field: str
    value: str
    scope: str = "exact_route_only"
    anchor_id: str


class ProtectedCandidate(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    anchor_id: str
    value: str
    reason: str


class ParallelQuery(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    query: str
    purpose: str
    hypothesis_id: str | None = None


class RetrievalPlan(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    original_query: str
    exact_queries: list[str] = Field(default_factory=list)
    lexical_query: str
    lexical_terms: list[str] = Field(default_factory=list)
    semantic_queries: list[str] = Field(default_factory=list)
    parallel_queries: list[ParallelQuery] = Field(default_factory=list)
    hard_filters: list[HardFilter] = Field(default_factory=list)
    protected_candidates: list[ProtectedCandidate] = Field(default_factory=list)
    confirmation_pending: list[str] = Field(default_factory=list)
    original_route_independent: bool = True
    source_preferences_are_filters: bool = False
    governed_intent: str = "other"
    governed_mapping_id: str | None = None
    governed_mapping_applied: bool = False
    retrieval_allowed: bool = True
    confirmation_question: str | None = None


class ConfirmationItem(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    item_id: str
    target_type: str
    target_id: str
    question: str


class ConfirmationState(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    required: bool = False
    mode: str = "deferred_parallel_search"
    items: list[ConfirmationItem] = Field(default_factory=list)


class ValidationReport(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    status: str
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)


class QueryAnalysis(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    query_id: str
    original_question: str
    rewritten_question: str
    question_type: list[str] = Field(default_factory=list)
    metadata: QueryMetadata
    privacy: PrivacyPolicy
    normalizations: list[Normalization] = Field(default_factory=list)
    anchors: list[Anchor] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    search_hypotheses: list[SearchHypothesis] = Field(default_factory=list)
    source_preferences: SourcePreferences
    evidence_requirements: list[EvidenceRequirement] = Field(default_factory=list)
    self_check: QuerySelfCheck
    retrieval_plan: RetrievalPlan
    confirmation: ConfirmationState
    validation: ValidationReport


SAFE_NORMALIZATIONS: dict[str, str] = {
    "采矿证": "采矿许可证",
    "矿证": "采矿许可证",
    "省自然资源厅": "省级自然资源主管部门",
    "省里发的": "省级自然资源主管部门颁发的",
}

ROMAN_TYPE_MAP = {
    "1": "Ⅰ",
    "I": "Ⅰ",
    "一": "Ⅰ",
    "Ⅰ": "Ⅰ",
    "2": "Ⅱ",
    "II": "Ⅱ",
    "二": "Ⅱ",
    "Ⅱ": "Ⅱ",
    "3": "Ⅲ",
    "III": "Ⅲ",
    "三": "Ⅲ",
    "Ⅲ": "Ⅲ",
}

EXACT_IDENTITY_TYPES = {
    "standard_no",
    "document_no",
    "document_title",
    "clause_no",
    "appendix_no",
    "table_no",
}

EVIDENCE_CRITICAL_TYPES = {
    "number",
    "date",
    "time_status",
    "negation",
    "exception",
    "condition",
}

MATERIAL_INFERENCE_TYPES = {
    "administrative_matter",
    "issuing_authority",
    "authority",
    "jurisdiction",
    "legal_status",
    "right_status",
    "procedure",
    "resource_type",
}

CONTROLLED_EXPLICIT_TERMS: dict[str, str] = {
    "采矿许可证": "license_type",
    "采矿证": "license_type",
    "勘查许可证": "license_type",
    "采矿权": "right_type",
    "探矿权": "right_type",
    "新立": "administrative_matter",
    "延续": "administrative_matter",
    "变更": "administrative_matter",
    "注销": "administrative_matter",
    "评审备案": "administrative_matter",
    "基本分析": "technical_method",
    "组合分析": "technical_method",
    "光谱分析": "technical_method",
    "矿产资源储量报告": "document_object",
    "储量报告": "document_object",
    "申请材料": "administrative_material",
    "资源量类型": "technical_target",
    "伴生组分": "mineral_component",
    "金矿": "mineral",
    "铁矿": "mineral",
    "煤矿": "mineral",
}

NEGATION_TERMS = (
    "不得",
    "不能",
    "不可以",
    "不应",
    "未办理",
    "未取得",
    "没有",
    "除外",
    "但是",
    "情节严重",
    "仅限",
)

TIME_STATUS_TERMS = (
    "超过有效期",
    "超过了有效期",
    "有效期届满",
    "已经届满",
    "尚未到期",
    "已经过期",
)

SEMANTIC_NEGATION_TERMS = ("不得", "不能", "不可以", "不应", "未", "没有", "无", "除外")
SEMANTIC_EXPIRY_TERMS = ("超过有效期", "超过了有效期", "有效期届满", "已经届满", "已经过期", "过期")

LIABILITY_TYPE_TERMS = (
    "行政责任",
    "刑事责任",
    "民事责任",
    "纪律责任",
    "法律责任",
    "责任类型",
)

SPECIFIC_LIABILITY_TYPE_TERMS = (
    "行政责任",
    "刑事责任",
    "民事责任",
    "纪律责任",
)

BROAD_LIABILITY_PATTERNS = (
    re.compile(r"(?:承担|负有|负|追究|有|涉及)(?:何种|什么|哪些|怎样的)?(?:法律)?责任"),
    re.compile(r"(?:法律)?责任(?:是什么|有哪些|包括什么|如何承担|怎么承担)"),
    re.compile(r"(?:何种|什么|哪些|怎样的)(?:法律)?责任"),
)

STANDARD_NO_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:GB(?:/T)?|DZ/T|MT/T|NB/T|TD/T|HJ|AQ)\s*[0-9.]+(?:[-—－][0-9]{4})?",
    re.IGNORECASE,
)
POLICY_NO_RE = re.compile(
    r"(?:自然资|国土资|地质矿产|财综|法释)[\u4e00-\u9fff]*〔\d{4}〕\d+号|国令第\d+号|第[一二三四五六七八九十百零〇\d]+号主席令"
)
DOCUMENT_TITLE_RE = re.compile(r"《[^》\n]{2,80}》")
CLAUSE_RE = re.compile(
    r"第(?:[一二三四五六七八九十百零〇]+|\d+(?:\.\d+)*)条"
    r"(?:第(?:[一二三四五六七八九十百零〇]+|\d+)款)?|(?<!\d)\d+(?:\.\d+){1,4}(?!\d)"
)
APPENDIX_RE = re.compile(r"附件\s*[A-Za-z一二三四五六七八九十\d]+|附录\s*[A-Za-z一二三四五六七八九十\d]+")
NUMBER_RE = re.compile(
    r"(?<!\d)\d+(?:\.\d+)?\s*(?:%|％|万元|元|年|个月|月|日|工作日|米|m|千米|km|倍|件|项)(?![\u4e00-\u9fffA-Za-z])",
    re.IGNORECASE,
)
EXPLORATION_TYPE_RE = re.compile(r"(?:类型)?\s*([123一二三ⅠⅡⅢI]+)\s*类型?", re.IGNORECASE)

ANSWER_LEAK_PATTERNS = (
    STANDARD_NO_RE,
    POLICY_NO_RE,
    DOCUMENT_TITLE_RE,
    re.compile(r"第[一二三四五六七八九十百零〇\d]+条"),
    re.compile(r"\d+(?:\.\d+)?\s*(?:万元|元|%|％|米|m|倍)", re.IGNORECASE),
    re.compile(r"划为(?:推断|控制|探明)资源量"),
    re.compile(r"由(?:自然资源部|省级自然资源主管部门)负责"),
)

SENSITIVE_PATTERNS: dict[str, re.Pattern[str]] = {
    "mobile_phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "national_id": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "license_identifier": re.compile(
        r"(?:许可证|采矿权|探矿权)(?:号|编号)[：:\s]*[A-Za-z0-9\-—－]{6,}",
        re.IGNORECASE,
    ),
    "detailed_address": re.compile(r"(?:地址|住址|所在地)[：:]\s*[^，。；;\n]{6,}"),
}

FORBIDDEN_CONCEPT_SUBSTITUTIONS = (
    ("采矿权", "采矿许可证"),
    ("采矿许可证", "采矿权"),
    ("探矿权", "勘查许可证"),
    ("勘查许可证", "探矿权"),
)


def prompt_sha256() -> str:
    return hashlib.sha256(QUERY_ANALYSIS_SYSTEM_PROMPT.encode("utf-8")).hexdigest()


def question_sha256(question: str) -> str:
    return hashlib.sha256(question.encode("utf-8")).hexdigest()


def stable_query_id(question: str) -> str:
    return f"query-{question_sha256(question)[:16]}"


def normalize_text(value: object) -> str:
    text = str(value or "").strip()
    return re.sub(r"\s+", " ", text)


def comparable_text(value: object) -> str:
    return unicodedata.normalize("NFKC", normalize_text(value))


def unique_text(values: Iterable[object]) -> list[str]:
    if isinstance(values, (str, bytes)):
        values = [values]
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = normalize_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def ungrounded_verifiable_facts(
    text: str,
    original_question: str,
    normalization_targets: Iterable[str] = (),
) -> list[str]:
    """Return document/clause/result facts added outside the user question.

    Such facts may be useful as unverified parallel-search hypotheses, but they
    must not enter a primary semantic rewrite where they would quietly steer
    retrieval toward a model-proposed answer or document.
    """

    allowed = set(normalization_targets)
    output: list[str] = []
    for pattern in ANSWER_LEAK_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0)
            if value in original_question or value in allowed or value in output:
                continue
            output.append(value)
    return output


def sensitive_categories(question: str) -> list[str]:
    return [name for name, pattern in SENSITIVE_PATTERNS.items() if pattern.search(question)]


def forbidden_concept_substitution(question: str, candidate: str) -> bool:
    for explicit, replacement in FORBIDDEN_CONCEPT_SUBSTITUTIONS:
        if explicit in question and replacement not in question and replacement in candidate:
            return True
    return False


def approved_normalization(source_text: str) -> str | None:
    source = normalize_text(source_text)
    if not source:
        return None
    approved = SAFE_NORMALIZATIONS.get(source)
    if approved:
        return approved
    match = re.fullmatch(r"(?:类型)?\s*([123一二三ⅠⅡⅢI]+)\s*类型?", source, re.IGNORECASE)
    if match and match.group(1) in ROMAN_TYPE_MAP:
        return f"类型{ROMAN_TYPE_MAP[match.group(1)]}"
    match = re.search(r"([123一二三ⅠⅡⅢI]+)类型", source, re.IGNORECASE)
    if match and match.group(1) in ROMAN_TYPE_MAP:
        return source.replace(match.group(0), f"类型{ROMAN_TYPE_MAP[match.group(1)]}")
    if source.startswith("《") and source.endswith("》"):
        return source[1:-1]
    clause = re.fullmatch(r"第(\d+(?:\.\d+)*)条", source)
    if clause:
        return clause.group(1)
    if source.endswith("阶段") and len(source) <= 12:
        return source.removesuffix("阶段")
    return None


def safe_normalization(source_text: str, proposed: str) -> str | None:
    source = normalize_text(source_text)
    target = normalize_text(proposed)
    if not source or not target:
        return None
    if comparable_text(source) == comparable_text(target):
        return target
    approved = approved_normalization(source)
    return approved if approved and comparable_text(target) == comparable_text(approved) else None


def anchor_actions(anchor_type: str, state: AnchorState) -> list[RetrievalAction]:
    if state == AnchorState.CANDIDATE:
        return [RetrievalAction.PARALLEL_SEARCH_ONLY]
    if state == AnchorState.REJECTED:
        return []
    actions: list[RetrievalAction] = [
        RetrievalAction.CANDIDATE_RETENTION,
        RetrievalAction.RANKING_BOOST,
    ]
    if anchor_type in EXACT_IDENTITY_TYPES:
        actions.insert(0, RetrievalAction.HARD_FILTER_EXACT_ROUTE)
    if anchor_type in EVIDENCE_CRITICAL_TYPES:
        actions.append(RetrievalAction.EVIDENCE_VERIFICATION)
    return actions


def _detected_anchor_specs(question: str) -> list[tuple[str, str]]:
    specs: list[tuple[str, str]] = []
    patterns = (
        ("standard_no", STANDARD_NO_RE),
        ("document_no", POLICY_NO_RE),
        ("document_title", DOCUMENT_TITLE_RE),
        ("clause_no", CLAUSE_RE),
        ("appendix_no", APPENDIX_RE),
        ("number", NUMBER_RE),
    )
    for anchor_type, pattern in patterns:
        specs.extend((anchor_type, match.group(0)) for match in pattern.finditer(question))
    for term, anchor_type in CONTROLLED_EXPLICIT_TERMS.items():
        if term in question:
            specs.append((anchor_type, term))
    for term in NEGATION_TERMS:
        if term in question:
            anchor_type = "exception" if term in {"除外", "但是", "情节严重", "仅限"} else "negation"
            specs.append((anchor_type, term))
    for term in TIME_STATUS_TERMS:
        if term in question:
            specs.append(("time_status", term))
    for match in EXPLORATION_TYPE_RE.finditer(question):
        raw = match.group(0).strip()
        if raw and match.group(1) in ROMAN_TYPE_MAP:
            specs.append(("exploration_type", raw))
    return [(kind, text) for kind, text in specs if text]


def _model_added_answer(payload: dict[str, Any]) -> bool:
    forbidden_keys = {"answer", "final_answer", "conclusion", "处罚结论", "办理结论"}
    return any(key in payload and payload.get(key) not in (None, "", [], {}) for key in forbidden_keys)


def _build_anchors(
    question: str,
    model_anchors: list[dict[str, Any]],
) -> tuple[list[Anchor], list[Normalization], list[SearchHypothesis], list[str], list[str]]:
    anchors: list[Anchor] = []
    normalizations: list[Normalization] = []
    hypotheses: list[SearchHypothesis] = []
    errors: list[str] = []
    warnings: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    broad_liability = _asks_all_applicable_liability(question)

    def add_anchor(
        anchor_type: str,
        raw_text: str,
        canonical_text: str,
        state: AnchorState,
        origin: AnchorOrigin,
        needs_confirmation: bool,
    ) -> None:
        key = (anchor_type, raw_text, canonical_text)
        if key in seen:
            if state in {AnchorState.LOCKED, AnchorState.CONFIRMED}:
                for index, existing in enumerate(anchors):
                    if (existing.type, existing.raw_text, existing.canonical_text) != key:
                        continue
                    if existing.state == AnchorState.CANDIDATE:
                        anchors[index] = existing.model_copy(
                            update={
                                "state": state,
                                "origin": origin,
                                "needs_confirmation": needs_confirmation,
                                "retrieval_actions": anchor_actions(anchor_type, state),
                            }
                        )
                    break
            return
        seen.add(key)
        anchors.append(
            Anchor(
                anchor_id=f"A{len(anchors) + 1:02d}",
                type=anchor_type or "concept",
                raw_text=raw_text,
                canonical_text=canonical_text,
                state=state,
                origin=origin,
                needs_confirmation=needs_confirmation,
                retrieval_actions=anchor_actions(anchor_type, state),
            )
        )

    for item in model_anchors:
        if not isinstance(item, dict):
            warnings.append("ignored_non_object_model_anchor")
            continue
        anchor_type = normalize_text(item.get("type")) or "concept"
        raw = normalize_text(item.get("source_text"))
        proposed = normalize_text(item.get("normalized_value")) or raw
        interpretation = normalize_text(item.get("interpretation"))
        needs_confirmation = bool(item.get("needs_confirmation"))
        if broad_liability and _liability_type_item(anchor_type, proposed):
            needs_confirmation = False
        explicit = interpretation == "explicit"
        if explicit:
            if not raw or raw not in question:
                warnings.append(f"explicit_anchor_demoted_not_verbatim:{raw or '<empty>'}")
                if proposed:
                    add_anchor(
                        anchor_type,
                        "",
                        proposed,
                        AnchorState.CANDIDATE,
                        AnchorOrigin.MODEL_INFERRED,
                        needs_confirmation or anchor_type in MATERIAL_INFERENCE_TYPES,
                    )
                continue
            approved = approved_normalization(raw)
            canonical = approved or raw
            origin = AnchorOrigin.PROGRAM_NORMALIZED if approved and approved != raw else AnchorOrigin.ORIGINAL_EXPLICIT
            add_anchor(anchor_type, raw, canonical, AnchorState.LOCKED, origin, False)
            if approved and approved != raw:
                normalizations.append(Normalization(source_text=raw, target_text=approved))
            if proposed and forbidden_concept_substitution(question, proposed):
                warnings.append(f"forbidden_concept_substitution_rejected:{raw}->{proposed}")
            elif proposed and comparable_text(proposed) not in {
                comparable_text(raw),
                comparable_text(canonical),
            }:
                hypothesis_id = f"H{len(hypotheses) + 1:02d}"
                hypotheses.append(
                    SearchHypothesis(
                        hypothesis_id=hypothesis_id,
                        query=proposed,
                        purpose=f"未经确认的规范化解释：{raw}",
                        needs_confirmation=needs_confirmation or anchor_type in MATERIAL_INFERENCE_TYPES,
                    )
                )
                warnings.append(f"unsafe_normalization_demoted_to_hypothesis:{raw}->{proposed}")
        else:
            if not proposed:
                warnings.append("ignored_empty_inferred_anchor")
                continue
            add_anchor(
                anchor_type,
                raw if raw in question else "",
                proposed,
                AnchorState.CANDIDATE,
                AnchorOrigin.MODEL_INFERRED,
                needs_confirmation or anchor_type in MATERIAL_INFERENCE_TYPES,
            )

    for anchor_type, raw in _detected_anchor_specs(question):
        canonical = approved_normalization(raw) or raw
        origin = AnchorOrigin.PROGRAM_NORMALIZED if canonical != raw else AnchorOrigin.PROGRAM_DETECTED
        add_anchor(anchor_type, raw, canonical, AnchorState.LOCKED, origin, False)
        if canonical != raw and not any(item.source_text == raw for item in normalizations):
            normalizations.append(Normalization(source_text=raw, target_text=canonical))

    return anchors, normalizations, hypotheses, errors, warnings


def _hard_filters(anchors: list[Anchor]) -> list[HardFilter]:
    filters: list[HardFilter] = []
    for anchor in anchors:
        if anchor.state not in {AnchorState.LOCKED, AnchorState.CONFIRMED}:
            continue
        if anchor.type not in EXACT_IDENTITY_TYPES:
            continue
        filters.append(
            HardFilter(
                field=anchor.type,
                value=anchor.canonical_text,
                anchor_id=anchor.anchor_id,
            )
        )
    return filters


def _grounded_lexical_terms(
    question: str,
    anchors: list[Anchor],
    model_terms: list[object],
) -> tuple[list[str], list[str], list[str]]:
    grounded_values = {
        value
        for anchor in anchors
        if anchor.state in {AnchorState.LOCKED, AnchorState.CONFIRMED}
        for value in (anchor.raw_text, anchor.canonical_text)
        if value
    }
    accepted: list[str] = []
    demoted: list[str] = []
    rejected: list[str] = []
    for term in unique_text(model_terms):
        if forbidden_concept_substitution(question, term):
            rejected.append(term)
            continue
        if term in question or term in grounded_values:
            accepted.append(term)
        else:
            demoted.append(term)
    accepted.extend(anchor.canonical_text for anchor in anchors if anchor.state == AnchorState.LOCKED)
    return unique_text(accepted), unique_text(demoted), unique_text(rejected)


def _source_preferences(payload: dict[str, Any]) -> SourcePreferences:
    source = payload.get("source_preferences") or {}
    if not isinstance(source, dict):
        source = {}
    primary = unique_text(source.get("primary") or [])
    supplementary = unique_text(source.get("supplementary") or [])
    if not primary and not supplementary:
        primary = ["all_governed_sources"]
    return SourcePreferences(
        primary=primary,
        supplementary=supplementary,
        exclude_by_type=[],
        routing_mode="prefer_not_exclude",
    )


def _asks_all_applicable_liability(question: str) -> bool:
    """Return true when the user asks broadly for applicable liabilities.

    This is a recall-expanding interpretation, not a legal conclusion.  An
    explicitly named liability type remains part of the original question and
    is therefore outside this default.
    """

    if any(term in question for term in SPECIFIC_LIABILITY_TYPE_TERMS):
        return False
    return any(pattern.search(question) for pattern in BROAD_LIABILITY_PATTERNS)


def _liability_type_item(*values: object) -> bool:
    text = " ".join(normalize_text(value) for value in values if value is not None)
    return any(term in text for term in LIABILITY_TYPE_TERMS) or (
        "责任" in text and any(term in text for term in ("行政", "刑事", "民事", "类型", "所有"))
    )


def _ambiguities(payload: dict[str, Any], question: str) -> list[Ambiguity]:
    output: list[Ambiguity] = []
    broad_liability = _asks_all_applicable_liability(question)
    for item in payload.get("ambiguities") or []:
        if not isinstance(item, dict):
            continue
        description = normalize_text(item.get("description"))
        if not description:
            continue
        field = normalize_text(item.get("field")) or "unspecified"
        confirmation_question = normalize_text(item.get("confirmation_question"))
        material = bool(item.get("material", True))
        if broad_liability and _liability_type_item(field, description, confirmation_question):
            material = False
        output.append(
            Ambiguity(
                field=field,
                description=description,
                material=material,
                confirmation_question=confirmation_question,
            )
        )
    return output


def _hypotheses(
    payload: dict[str, Any],
    initial: list[SearchHypothesis],
    demoted_terms: list[str],
    question: str,
) -> list[SearchHypothesis]:
    output = list(initial)
    seen = {item.query for item in output}
    broad_liability = _asks_all_applicable_liability(question)
    for item in payload.get("search_hypotheses") or []:
        if not isinstance(item, dict):
            continue
        query = normalize_text(item.get("query"))
        if not query or query in seen:
            continue
        seen.add(query)
        output.append(
            SearchHypothesis(
                hypothesis_id=f"H{len(output) + 1:02d}",
                query=query,
                purpose=normalize_text(item.get("purpose")) or "模型提出的并行搜索假设",
                needs_confirmation=(
                    bool(item.get("needs_confirmation", False))
                    and not (broad_liability and _liability_type_item(query, item.get("purpose")))
                ),
            )
        )
    for term in demoted_terms:
        if term in seen:
            continue
        seen.add(term)
        output.append(
            SearchHypothesis(
                hypothesis_id=f"H{len(output) + 1:02d}",
                query=term,
                purpose="未在原问题中明示的模型检索词",
                needs_confirmation=False,
            )
        )
    return output


def _evidence_requirements(payload: dict[str, Any]) -> list[EvidenceRequirement]:
    output: list[EvidenceRequirement] = []
    for item in payload.get("evidence_requirements") or []:
        if isinstance(item, str):
            description = normalize_text(item)
            elements: list[str] = []
            clause_level = True
        elif isinstance(item, dict):
            description = normalize_text(item.get("description"))
            elements = unique_text(item.get("required_elements") or [])
            clause_level = bool(item.get("must_have_clause_level_evidence", True))
        else:
            continue
        if not description:
            continue
        output.append(
            EvidenceRequirement(
                requirement_id=f"E{len(output) + 1:02d}",
                description=description,
                required_elements=elements,
                must_have_clause_level_evidence=clause_level,
            )
        )
    return output


def _confirmation_state(
    anchors: list[Anchor],
    ambiguities: list[Ambiguity],
    hypotheses: list[SearchHypothesis],
) -> ConfirmationState:
    items: list[ConfirmationItem] = []
    for anchor in anchors:
        if anchor.state == AnchorState.CANDIDATE and anchor.needs_confirmation:
            items.append(
                ConfirmationItem(
                    item_id=f"C{len(items) + 1:02d}",
                    target_type="anchor",
                    target_id=anchor.anchor_id,
                    question=f"是否确认将“{anchor.canonical_text}”作为检索事项或条件？",
                )
            )
    for ambiguity in ambiguities:
        if ambiguity.material and ambiguity.confirmation_question:
            items.append(
                ConfirmationItem(
                    item_id=f"C{len(items) + 1:02d}",
                    target_type="ambiguity",
                    target_id=ambiguity.field,
                    question=ambiguity.confirmation_question,
                )
            )
    for hypothesis in hypotheses:
        if hypothesis.needs_confirmation:
            items.append(
                ConfirmationItem(
                    item_id=f"C{len(items) + 1:02d}",
                    target_type="hypothesis",
                    target_id=hypothesis.hypothesis_id,
                    question=f"是否确认“{hypothesis.query}”是主要检索方向？",
                )
            )
    return ConfirmationState(required=bool(items), items=items)


def _validation_report(
    analysis: QueryAnalysis,
    compiler_errors: list[str],
    compiler_warnings: list[str],
) -> ValidationReport:
    errors = list(compiler_errors)
    warnings = list(compiler_warnings)
    original = analysis.original_question
    rewritten = analysis.rewritten_question

    if not rewritten or not rewritten.endswith(("?", "？")):
        errors.append("rewritten_question_not_complete_question")
    if analysis.self_check.answer_generated:
        errors.append("model_generated_answer")
    if analysis.self_check.missing_original_facts:
        errors.append("self_check_reports_missing_original_facts")
    if analysis.source_preferences.exclude_by_type:
        errors.append("source_type_exclusion_forbidden")
    if analysis.retrieval_plan.original_query != original:
        errors.append("original_route_not_preserved")
    if not analysis.retrieval_plan.original_route_independent:
        errors.append("original_route_not_independent")
    if analysis.retrieval_plan.source_preferences_are_filters:
        errors.append("source_preferences_used_as_filters")

    anchors_by_id = {anchor.anchor_id: anchor for anchor in analysis.anchors}

    def anchor_preserved(anchor: Anchor) -> bool:
        if anchor.raw_text in rewritten or anchor.canonical_text in rewritten:
            return True
        if comparable_text(anchor.raw_text) in comparable_text(rewritten):
            return True
        if comparable_text(anchor.canonical_text) in comparable_text(rewritten):
            return True
        if anchor.type in {"negation", "exception"}:
            return any(term in rewritten for term in SEMANTIC_NEGATION_TERMS)
        if anchor.type in {"time_status", "status"}:
            return any(term in rewritten for term in SEMANTIC_EXPIRY_TERMS)
        if anchor.type == "condition":
            child_terms = [
                term
                for term in CONTROLLED_EXPLICIT_TERMS
                if term in anchor.raw_text
            ]
            terms_preserved = bool(child_terms) and all(term in rewritten for term in child_terms)
            negation_preserved = not any(term in anchor.raw_text for term in NEGATION_TERMS) or any(
                term in rewritten for term in SEMANTIC_NEGATION_TERMS
            )
            return terms_preserved and negation_preserved
        return False

    for anchor in analysis.anchors:
        if anchor.state == AnchorState.LOCKED:
            if not anchor.raw_text or anchor.raw_text not in original:
                errors.append(f"locked_anchor_not_in_original:{anchor.anchor_id}")
            if not anchor_preserved(anchor):
                if anchor.type in EXACT_IDENTITY_TYPES | EVIDENCE_CRITICAL_TYPES | {
                    "mineral",
                    "right_type",
                    "license_type",
                    "technical_method",
                    "exploration_type",
                }:
                    errors.append(f"locked_anchor_missing_from_rewrite:{anchor.anchor_id}")
                else:
                    warnings.append(f"noncritical_anchor_paraphrased:{anchor.anchor_id}")
        if anchor.state == AnchorState.CANDIDATE:
            if RetrievalAction.PARALLEL_SEARCH_ONLY not in anchor.retrieval_actions:
                errors.append(f"candidate_anchor_not_parallel_only:{anchor.anchor_id}")
            forbidden = {
                RetrievalAction.HARD_FILTER_EXACT_ROUTE,
                RetrievalAction.CANDIDATE_RETENTION,
                RetrievalAction.RANKING_BOOST,
            }
            if forbidden.intersection(anchor.retrieval_actions):
                errors.append(f"candidate_anchor_overprotected:{anchor.anchor_id}")

    for hard_filter in analysis.retrieval_plan.hard_filters:
        anchor = anchors_by_id.get(hard_filter.anchor_id)
        if anchor is None:
            errors.append(f"hard_filter_missing_anchor:{hard_filter.anchor_id}")
            continue
        if anchor.state not in {AnchorState.LOCKED, AnchorState.CONFIRMED}:
            errors.append(f"hard_filter_on_unconfirmed_anchor:{hard_filter.anchor_id}")
        if hard_filter.scope != "exact_route_only":
            errors.append(f"global_hard_filter_forbidden:{hard_filter.anchor_id}")

    primary_queries = " ".join(
        [analysis.rewritten_question, analysis.retrieval_plan.lexical_query]
        + analysis.retrieval_plan.semantic_queries
    )
    for value in ungrounded_verifiable_facts(
        primary_queries,
        original,
        (normalization.target_text for normalization in analysis.normalizations),
    ):
        errors.append(f"possible_answer_leak:{value}")

    for anchor_type, raw in _detected_anchor_specs(original):
        if not any(anchor.raw_text == raw for anchor in analysis.anchors):
            errors.append(f"deterministic_anchor_missing:{anchor_type}:{raw}")

    checks = {
        "schema_version_current": analysis.metadata.schema_version == SCHEMA_VERSION,
        "original_question_hashed": analysis.metadata.original_question_sha256 == question_sha256(original),
        "original_route_preserved": analysis.retrieval_plan.original_query == original,
        "original_route_independent": analysis.retrieval_plan.original_route_independent,
        "no_source_type_exclusion": not analysis.source_preferences.exclude_by_type,
        "no_answer_generated": not analysis.self_check.answer_generated,
        "all_hard_filters_exact_route_only": all(
            item.scope == "exact_route_only" for item in analysis.retrieval_plan.hard_filters
        ),
    }
    for name, passed in checks.items():
        if not passed:
            errors.append(f"failed_check:{name}")

    return ValidationReport(
        status="pass" if not errors else "fail",
        errors=sorted(set(errors)),
        warnings=sorted(set(warnings)),
        checks=checks,
    )


def compile_query_analysis(
    question: str,
    model_payload: dict[str, Any],
    *,
    query_id: str | None = None,
    model: str = "deepseek-v4-flash",
    generated_at: str | None = None,
) -> QueryAnalysis:
    """Compile untrusted model JSON into the controlled v1 query record."""

    original = normalize_text(question)
    if not original:
        raise ValueError("question is required")
    if not isinstance(model_payload, dict):
        raise TypeError("model_payload must be an object")
    governed_route = route_governed_query(original)

    compiler_errors: list[str] = []
    compiler_warnings: list[str] = []
    payload_original = normalize_text(model_payload.get("original_question"))
    if payload_original and payload_original != original:
        compiler_errors.append("model_original_question_mismatch")
    if _model_added_answer(model_payload):
        compiler_errors.append("model_returned_answer_field")
    raw_preferences = model_payload.get("source_preferences") or {}
    if isinstance(raw_preferences, dict) and raw_preferences.get("exclude_by_type"):
        compiler_warnings.append("model_source_type_exclusions_ignored")

    rewritten = normalize_text(model_payload.get("rewritten_question"))
    model_anchors = model_payload.get("anchors") or []
    if not isinstance(model_anchors, list):
        model_anchors = []
        compiler_errors.append("model_anchors_not_array")
    anchors, normalizations, initial_hypotheses, anchor_errors, anchor_warnings = _build_anchors(
        original,
        model_anchors,
    )
    for source_text, target_text in governed_route.normalizations:
        if not any(
            item.source_text == source_text and item.target_text == target_text
            for item in normalizations
        ):
            normalizations.append(
                Normalization(source_text=source_text, target_text=target_text)
            )
    compiler_errors.extend(anchor_errors)
    compiler_warnings.extend(anchor_warnings)

    model_terms = model_payload.get("lexical_terms") or []
    if not isinstance(model_terms, list):
        model_terms = []
        compiler_errors.append("model_lexical_terms_not_array")
    lexical_terms, demoted_terms, rejected_terms = _grounded_lexical_terms(original, anchors, model_terms)
    if demoted_terms:
        compiler_warnings.extend(f"ungrounded_lexical_term_demoted:{term}" for term in demoted_terms)
    if rejected_terms:
        compiler_warnings.extend(f"forbidden_lexical_substitution_rejected:{term}" for term in rejected_terms)
    hypotheses = _hypotheses(model_payload, initial_hypotheses, demoted_terms, original)
    ambiguities = _ambiguities(model_payload, original)
    if governed_route.confirmation_required and governed_route.confirmation_prompt:
        ambiguities.append(
            Ambiguity(
                field="sand_gold_requirement_scope",
                description="砂金问题可能指适用标准或不同类别的具体技术要求。",
                material=True,
                confirmation_question=governed_route.confirmation_prompt,
            )
        )
    evidence_requirements = _evidence_requirements(model_payload)
    source_preferences = _source_preferences(model_payload)

    semantic_queries = (
        [governed_route.semantic_query, rewritten]
        if governed_route.alias_triggered and rewritten
        else [governed_route.semantic_query]
        if governed_route.alias_triggered
        else [rewritten]
        if rewritten
        else []
    )
    parallel_queries = [
        ParallelQuery(
            query=hypothesis.query,
            purpose=hypothesis.purpose,
            hypothesis_id=hypothesis.hypothesis_id,
        )
        for hypothesis in hypotheses
    ]
    for item in model_payload.get("semantic_subqueries") or []:
        if not isinstance(item, dict):
            continue
        query = normalize_text(item.get("query"))
        if not query:
            continue
        purpose = normalize_text(item.get("purpose")) or "模型拆分的证据目标"
        parallel_queries.append(ParallelQuery(query=query, purpose=purpose))
        added_facts = ungrounded_verifiable_facts(
            query,
            original,
            (normalization.target_text for normalization in normalizations),
        )
        if added_facts:
            compiler_warnings.append(
                "semantic_subquery_demoted_to_parallel:" + "|".join(added_facts)
            )
        else:
            semantic_queries.append(query)

    exact_queries = unique_text(
        anchor.canonical_text
        for anchor in anchors
        if anchor.state in {AnchorState.LOCKED, AnchorState.CONFIRMED}
        and anchor.type in EXACT_IDENTITY_TYPES
    )
    if not exact_queries:
        exact_queries = unique_text(
            anchor.canonical_text
            for anchor in anchors
            if anchor.state == AnchorState.LOCKED
        )

    protected_candidates = [
        ProtectedCandidate(
            anchor_id=anchor.anchor_id,
            value=anchor.canonical_text,
            reason="用户明示或程序确认的一对一锚点不得在候选截断时丢失",
        )
        for anchor in anchors
        if RetrievalAction.CANDIDATE_RETENTION in anchor.retrieval_actions
    ]

    self_payload = model_payload.get("self_check") or {}
    if not isinstance(self_payload, dict):
        self_payload = {}
        compiler_errors.append("model_self_check_not_object")
    answer_generated = bool(self_payload.get("answer_generated")) or _model_added_answer(model_payload)
    added_assumptions = unique_text(self_payload.get("added_assumptions") or [])
    added_assumptions.extend(
        hypothesis.query for hypothesis in hypotheses if hypothesis.query not in added_assumptions
    )
    self_check = QuerySelfCheck(
        preserved_original_facts=unique_text(self_payload.get("preserved_original_facts") or []),
        missing_original_facts=unique_text(self_payload.get("missing_original_facts") or []),
        added_assumptions=unique_text(added_assumptions),
        answer_generated=answer_generated,
    )

    confirmation = _confirmation_state(anchors, ambiguities, hypotheses)
    privacy_categories = sensitive_categories(original)
    now = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    qid = normalize_text(query_id or model_payload.get("query_id")) or stable_query_id(original)
    hard_filters = _hard_filters(anchors)
    confirmation_pending = unique_text(item.question for item in confirmation.items)

    analysis = QueryAnalysis(
        query_id=qid,
        original_question=original,
        rewritten_question=rewritten,
        question_type=unique_text(model_payload.get("question_type") or ["uncertain"]),
        metadata=QueryMetadata(
            prompt_sha256=prompt_sha256(),
            model=model,
            generated_at=now,
            original_question_sha256=question_sha256(original),
        ),
        privacy=PrivacyPolicy(
            sensitive_data_detected=bool(privacy_categories),
            detected_categories=privacy_categories,
        ),
        normalizations=normalizations,
        anchors=anchors,
        ambiguities=ambiguities,
        search_hypotheses=hypotheses,
        source_preferences=source_preferences,
        evidence_requirements=evidence_requirements,
        self_check=self_check,
        retrieval_plan=RetrievalPlan(
            original_query=original,
            exact_queries=exact_queries,
            lexical_query=(
                governed_route.lexical_query
                if governed_route.alias_triggered
                else " ".join(lexical_terms)
            ),
            lexical_terms=lexical_terms,
            semantic_queries=unique_text(semantic_queries),
            parallel_queries=parallel_queries,
            hard_filters=hard_filters,
            protected_candidates=protected_candidates,
            confirmation_pending=confirmation_pending,
            governed_intent=governed_route.governed_intent,
            governed_mapping_id=governed_route.mapping_id,
            governed_mapping_applied=governed_route.mapping_applied,
            retrieval_allowed=governed_route.retrieval_allowed,
            confirmation_question=governed_route.confirmation_prompt,
        ),
        confirmation=confirmation,
        validation=ValidationReport(status="pending"),
    )
    report = _validation_report(analysis, compiler_errors, compiler_warnings)
    analysis = analysis.model_copy(update={"validation": report})
    fallback_errors = [
        error
        for error in report.errors
        if error.startswith("locked_anchor_missing_from_rewrite:")
    ]
    if report.errors and len(fallback_errors) == len(report.errors):
        fallback_rewrite = original if original.endswith(("?", "？")) else original + "？"
        fallback_plan = analysis.retrieval_plan.model_copy(
            update={
                "semantic_queries": unique_text(
                    [fallback_rewrite] + analysis.retrieval_plan.semantic_queries[1:]
                )
            }
        )
        analysis = analysis.model_copy(
            update={
                "rewritten_question": fallback_rewrite,
                "retrieval_plan": fallback_plan,
            }
        )
        compiler_warnings = compiler_warnings + ["semantic_rewrite_fell_back_to_original"]
        report = _validation_report(analysis, compiler_errors, compiler_warnings)
        analysis = analysis.model_copy(update={"validation": report})
    return analysis


def safe_log_record(analysis: QueryAnalysis) -> dict[str, Any]:
    """Return a production-safe structural record with no raw/free-form query text."""

    return {
        "query_id": analysis.query_id,
        "schema_version": analysis.metadata.schema_version,
        "prompt_version": analysis.metadata.prompt_version,
        "model": analysis.metadata.model,
        "generated_at": analysis.metadata.generated_at,
        "original_question_sha256": analysis.metadata.original_question_sha256,
        "privacy": analysis.privacy.model_dump(mode="json"),
        "question_type": analysis.question_type,
        "anchor_summary": [
            {
                "type": anchor.type,
                "state": anchor.state,
                "origin": anchor.origin,
                "needs_confirmation": anchor.needs_confirmation,
                "retrieval_actions": anchor.retrieval_actions,
            }
            for anchor in analysis.anchors
        ],
        "ambiguity_count": len(analysis.ambiguities),
        "hypothesis_count": len(analysis.search_hypotheses),
        "evidence_requirement_elements": sorted(
            {
                element
                for requirement in analysis.evidence_requirements
                for element in requirement.required_elements
            }
        ),
        "confirmation_required": analysis.confirmation.required,
        "governed_routing": {
            "intent": analysis.retrieval_plan.governed_intent,
            "mapping_id": analysis.retrieval_plan.governed_mapping_id,
            "mapping_applied": analysis.retrieval_plan.governed_mapping_applied,
            "retrieval_allowed": analysis.retrieval_plan.retrieval_allowed,
        },
        "validation": analysis.validation.model_dump(mode="json"),
    }


def analyses_to_jsonl(analyses: Iterable[QueryAnalysis]) -> str:
    return "".join(
        json.dumps(item.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")) + "\n"
        for item in analyses
    )


def query_analysis_json_schema() -> dict[str, Any]:
    schema = QueryAnalysis.model_json_schema()
    schema["$id"] = "https://geowiki.local/schemas/query_analysis.v1.schema.json"
    schema["title"] = "GeoWiki Query Analysis v1"
    return schema
