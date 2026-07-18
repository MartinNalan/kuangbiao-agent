from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, replace
from typing import Any

from .domain_lexicon import matched_lexicon_entries
from .query_classification import (
    QueryClassification,
    build_classification,
    classification_from_payload,
    legacy_intent_for_primary,
)
from .technical_stage_requirements import (
    TECHNICAL_REQUIREMENT_STANDARD_NO,
    TECHNICAL_REQUIREMENT_STANDARD_TITLE,
    stage_requirement_clauses,
    stage_section_from_text,
)


EXPLORATION_TYPE_LABELS = {
    "1": "Ⅰ",
    "I": "Ⅰ",
    "一": "Ⅰ",
    "2": "Ⅱ",
    "II": "Ⅱ",
    "二": "Ⅱ",
    "3": "Ⅲ",
    "III": "Ⅲ",
    "三": "Ⅲ",
    "工": "Ⅰ",
}

COMPARISON_TERMS = ("不一致", "差异", "不同", "比较", "列举", "哪些标准", "哪些规范", "哪些规程")
ENGINEERING_DISTANCE_TERMS = (
    "工程间距",
    "基本工程间距",
    "勘查工程间距",
    "工程距离",
    "工程网度",
    "走向间距",
    "倾向间距",
)
PROJECTION_TERMS = ("矿体外推", "有限外推", "无限外推", "外推距离", "尖推", "平推")
PROJECTION_RATIO_TERMS = ("1/2", "1/4", "二分之一", "四分之一", "一半")
LICENSE_TERMS = ("采矿证", "采矿许可证", "采矿权", "矿证")
SERVICE_MATERIAL_TERMS = (
    "提交什么材料",
    "提交哪些材料",
    "需要什么材料",
    "需要哪些材料",
    "申请材料",
    "申请资料",
    "资料清单",
    "材料清单",
    "资料",
    "要件",
    "必备材料",
    "必备资料",
    "所需材料",
    "所需资料",
    "要交什么",
    "要交哪些",
    "交什么材料",
    "交哪些材料",
)
SERVICE_PROCEDURE_TERMS = (
    "怎么办理",
    "如何办理",
    "办理流程",
    "流程",
    "办理程序",
    "办理依据",
    "依据哪个文件",
    "依据什么文件",
    "按哪个文件",
    "步骤",
    "手续",
)
SERVICE_TIME_LIMIT_TERMS = ("办结时限", "办理时限", "需要多久", "多久办结", "多少个工作日", "时限是多久")
POST_FILING_LICENSE_ACTION_TERMS = (
    "还需要",
    "还需",
    "还要",
    "接下来",
    "下一步",
    "什么手续",
    "哪些手续",
    "怎么办",
)
AUTHORITY_INTENT_TERMS = (
    "哪个机构",
    "哪个机关",
    "去哪个机构",
    "谁负责",
    "哪一级部门",
    "哪个部门",
    "权限归属",
    "去哪里申请",
    "向哪里申请",
    "在哪里申请",
    "申请机关",
    "受理机关",
    "受理部门",
    "找谁",
    "找哪个部门",
    "去哪里备案",
    "在哪里备案",
    "备案机关",
    "哪一级申请",
    "哪一级备案",
    "省里申请",
    "部里申请",
)
AUTHORITY_TOPIC_TERMS = (
    "储量评审",
    "资源储量评审",
    "储量报告评审",
    "储量备案",
    "评审备案",
    "资源储量评审备案",
    "矿产资源储量评审备案",
)
AUTHENTICITY_TERMS = ("真实性", "真实准确", "弄虚作假", "真实性负责")
RESERVE_REPORT_TERMS = ("资源储量报告", "矿产资源储量报告", "储量报告")
EXPLORATION_STAGE_TERMS = ("详查", "勘探", "普查", "勘查程度", "勘查阶段")
TECHNICAL_REQUIREMENT_SATISFACTION_TERMS = (
    "是否满足",
    "能否满足",
    "是否符合",
    "能否符合",
    "能否替代",
    "是否可以替代",
    "能否覆盖",
    "是否还需要",
    "是否必须",
    "还必须做",
)
TECHNICAL_TEST_CONFORMITY_TERMS = (
    "是否算",
    "算不算",
    "是否属于",
    "是否构成",
    "如何认定",
    "样品重量",
    "样品质量",
    "样品量",
    "处理量",
    "运行时长",
    "连续时长",
    "设备",
    "采样",
    "记录",
    "符合半工业试验要求",
    "符合实验室扩大连续试验要求",
)
TECHNICAL_STAGE_REQUIREMENT_TERMS = (
    "矿石加工选冶技术性能",
    "矿石选冶技术性能",
    "加工选冶技术性能",
    "矿石加工选冶试验",
    "矿石选冶试验",
    "选冶试验",
    "选冶要求",
    "加工选冶要求",
    "试验研究程度",
    "选矿试验",
    "选矿试验要求",
    "选矿工艺试验",
    "选矿技术性能",
)
TECHNICAL_STANDARD_SELECTION_TERMS = TECHNICAL_STAGE_REQUIREMENT_TERMS
STANDARD_SELECTION_PHRASES = (
    "哪个标准",
    "哪个规范",
    "哪个规程",
    "什么标准",
    "什么规范",
    "什么规程",
    "哪项标准",
    "哪项规范",
    "哪项规程",
    "那个标准",
    "那个规范",
    "那个规程",
)
TECHNICAL_STUDY_TERMS = (
    "类比研究",
    "工艺矿物学研究",
    "可选性试验",
    "实验室流程试验",
    "实验室扩大连续试验",
    "半工业试验",
    "工业试验",
    "初步测试研究",
    "基本测试研究",
    "详细测试研究",
)
MINING_CONVERSION_TERMS = (
    "转采",
    "探转采",
    "探矿权转采矿权",
    "申请采矿权",
    "采矿权新立",
    "可作为矿山设计开采依据",
    "供矿山设计开采",
    "作为矿山建设设计的依据",
)
TRANSFER_ANCHOR_STANDARD_NUMBERS = ("自然资规〔2023〕4号", "DZ/T 0430-2023")
TRANSFER_EQUIVALENT_TERMS = (
    "可作为矿山设计开采依据",
    "供矿山设计开采",
    "作为矿山建设设计的依据",
)
TRANSFER_REPORT_OBJECT_TERMS = (
    "详查报告",
    "详终报告",
    "详终矿区",
    "地质勘查报告",
)
TRANSFER_CONDITION_TERMS = (
    "可行性研究",
    "工业价值",
    "经济价值",
    "勘探程度要求",
)
PROJECTION_REFERENCE_STANDARD_NUMBERS = ("DZ/T 0338.1-2020", "DZ/T 0338.2-2020")
PROJECTION_REFERENCE_QUERIES = (
    "DZ/T 0338.1-2020 6.2.2.1 矿体外推 经验工程间距",
    "DZ/T 0338.2-2020 5.4.2 有限外推 推断资源量工程间距",
)
COMPANION_MINERAL_TERMS = ("共伴生", "伴生矿产", "伴生矿", "伴生资源")
RESOURCE_TYPE_TERMS = ("资源量类型", "资源储量类型", "类型如何确定", "类型怎么确定", "类型划分")
EXPLORATION_FACTOR_TERMS = ("划分因素", "因素表格", "因素表", "划分表格", "划分表")
BASIC_ANALYSIS_TERMS = ("基本分析项目", "基本分析的项目", "基本分析")
TABLE_OUTPUT_TERMS = ("表格", "因素表", "划分表", "指标表", "工程间距表")
RELATED_DOCUMENT_TERMS = ("其他文件", "还有哪些文件", "还有什么文件", "其他规定", "还有其他规定")
DEFINITION_MARKERS = ("定义", "如何定义", "怎么定义", "是什么意思", "什么是", "概念")
COMPOUND_DEFINITION_TERMS: dict[str, tuple[str, ...]] = {
    "资源储量": ("资源量", "储量"),
}
PREFERRED_DEFINITION_SOURCES: dict[str, tuple[str, str]] = {
    "资源量": ("固体矿产资源储量分类", "GB/T 17766-2020"),
    "储量": ("固体矿产资源储量分类", "GB/T 17766-2020"),
    "探明资源量": ("固体矿产资源储量分类", "GB/T 17766-2020"),
    "控制资源量": ("固体矿产资源储量分类", "GB/T 17766-2020"),
    "推断资源量": ("固体矿产资源储量分类", "GB/T 17766-2020"),
    "证实储量": ("固体矿产资源储量分类", "GB/T 17766-2020"),
    "可信储量": ("固体矿产资源储量分类", "GB/T 17766-2020"),
}
FOLLOW_UP_MARKERS = (
    "还有吗",
    "还有哪些",
    "还有什么",
    "其他文件",
    "其他规定",
    "相关内容",
    "上述",
    "前面",
    "这个文件",
    "这个标准",
    "该文件",
    "该标准",
    "它",
    "我的情况",
    "我这种情况",
    "这种情况",
    "这种情况下",
    "按这个情况",
    "是否可以理解为",
)
FOLLOW_UP_FOCUS_TERMS = (
    "勘查实施方案",
    "矿产资源开发利用方案",
    "矿产资源储量评审备案",
    "资源储量报告",
    "储量报告",
    "采矿许可证",
    "勘查许可证",
    "采矿权",
    "探矿权",
    "矿体外推",
    "工程间距",
    "评审",
    "审查",
    "申请材料",
    "办理程序",
)

_TYPE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:第\s*)?(III|II|I|[123一二三])\s*类\s*型",
    flags=re.IGNORECASE,
)
_SHORT_TYPE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:第\s*)?(III|II|I|[123一二三])\s*类(?!型)",
    flags=re.IGNORECASE,
)
_STANDARD_NO_PATTERN = re.compile(
    r"(?<![A-Z0-9])(?:GB(?:/T)?|DZ/T|TD/T|HJ|AQ|MT/T|YS/T|XB/T|NB/T|EJ/T|SL/T)"
    r"\s*\d+(?:\.\d+)*-\d{4}(?!\d)",
    flags=re.IGNORECASE,
)
_POLICY_NO_PATTERN = re.compile(
    r"(?:自然资规|国土资(?:厅)?发|国土资规|财建|财综字)\s*[〔\[]\s*\d{4}\s*[〕\]]\s*\d+\s*号"
    r"|(?:中华人民共和国国务院令|国务院令|国令)\s*第?\s*\d+\s*号",
    flags=re.IGNORECASE,
)

AUTHORITY_LEVELS = {"unknown", "ministry", "province"}
_LICENSE_NAMES = r"(?:采矿许可证|采矿证|矿证|勘查许可证|探矿证)"
_MINISTRY_ACTORS = r"(?:自然资源部|国土资源部|原国土资源部|部里|部级)"
_PROVINCE_ACTORS = r"(?:省级自然资源主管部门|省自然资源厅|省国土资源厅|省厅|省里|省级)"
_ISSUE_ACTIONS = r"(?:颁发|核发|发放|发证|签发|发给|发的|所发)"
_GRANT_ACTIONS = r"(?:出让|出让权限|配置权限|登记权限)"


@dataclass(frozen=True)
class QueryPlan:
    original_query: str
    normalized_query: str
    retrieval_query: str
    intent: str
    target_exploration_type: str | None = None
    candidate_title_terms: tuple[str, ...] = ()
    standard_numbers: tuple[str, ...] = ()
    focus_terms: tuple[str, ...] = ()
    document_types: tuple[str, ...] = ()
    subject_terms: tuple[str, ...] = ()
    required_terms: tuple[str, ...] = ()
    alternative_terms: tuple[str, ...] = ()
    negative_terms: tuple[str, ...] = ()
    required_evidence_groups: tuple[tuple[str, ...], ...] = ()
    search_mode: str = "default"
    comparison_dimensions: tuple[str, ...] = ()
    scope_origin: str = "none"
    output_mode: str = "default"
    planner_used: bool = False
    planner_confidence: float = 0.0
    license_issuer_level: str = "unknown"
    mining_right_granting_level: str = "unknown"
    filing_authority: str = "unknown"
    authority_role_ambiguous: bool = False
    exhaustive_search: bool = False
    target_terms: tuple[str, ...] = ()
    definition_mode: str = "none"
    definition_slots: tuple[str, ...] = ()
    preferred_definition_sources: tuple[str, ...] = ()
    classification: QueryClassification | None = None

    @property
    def has_candidate_scope(self) -> bool:
        return bool(self.candidate_title_terms or self.standard_numbers)

    @property
    def has_hard_candidate_scope(self) -> bool:
        return self.has_candidate_scope and self.scope_origin in {"user", "deterministic"}

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    def to_llm_payload(self) -> dict[str, Any]:
        return {
            "normalized_query": self.normalized_query,
            "intent": self.intent,
            "target_exploration_type": self.target_exploration_type,
            "candidate_title_terms": self.candidate_title_terms,
            "standard_numbers": self.standard_numbers,
            "document_types": self.document_types,
            "subject_terms": self.subject_terms,
            "required_terms": self.required_terms,
            "alternative_terms": self.alternative_terms,
            "negative_terms": self.negative_terms,
            "required_evidence_groups": self.required_evidence_groups,
            "search_mode": self.search_mode,
            "comparison_dimensions": self.comparison_dimensions,
            "output_mode": self.output_mode,
            "license_issuer_level": self.license_issuer_level,
            "mining_right_granting_level": self.mining_right_granting_level,
            "filing_authority": self.filing_authority,
            "authority_role_ambiguous": self.authority_role_ambiguous,
            "exhaustive_search": self.exhaustive_search,
            "target_terms": self.target_terms,
            "definition_mode": self.definition_mode,
            "definition_slots": self.definition_slots,
            "preferred_definition_sources": self.preferred_definition_sources,
            "classification": self.classification.to_payload() if self.classification else None,
        }


DEFAULT_EVIDENCE_GROUPS: dict[str, tuple[tuple[str, ...], ...]] = {
    "projection_comparison": (
        ("无限外推", "有限外推", "外推", "尖推", "平推"),
        ("工程间距", "基本间距", "实际间距", "经验工程间距"),
        ("1/2", "1/4", "2/3", "1/3", "二分之一", "四分之一", "尖推", "平推"),
    ),
    "projection_rule": (
        ("外推", "尖推", "平推"),
        ("工程间距", "基本间距", "实际间距", "经验工程间距"),
    ),
    "projection_numeric_rule": (
        ("无限外推",),
        ("工程间距", "经验工程间距"),
        ("1/2", "二分之一"),
    ),
    "engineering_distance_lookup": (
        ("工程间距", "基本勘查工程间距"),
        ("坑探", "钻探", "穿脉", "沿脉", "走向", "倾斜"),
    ),
    "authority_responsibility": (
        ("负责", "权限", "评审备案范围"),
        ("自然资源部", "省级自然资源主管部门"),
        ("勘查许可证", "采矿许可证", "许可证"),
    ),
    "legal_responsibility": (("真实性负责", "不得弄虚作假"),),
    "service_materials": (
        ("申请材料", "申请资料", "材料清单"),
        ("提交", "提供", "附件"),
    ),
    "service_procedure_basis": (
        ("办理流程", "办理方式", "登记管理", "办理依据"),
        ("采矿权", "探矿权", "矿业权"),
    ),
    "service_time_limit": (("办结时限", "工作日", "日内办结"),),
    "exploration_to_mining_eligibility": (
        (
            "详查",
            "详终",
            "勘查程度",
            "勘查阶段",
            "地质勘查报告",
            "核实报告",
        ),
        (
            "探矿权转采矿权",
            "转采",
            "申请采矿权",
            "采矿权新立",
            "可作为矿山设计开采依据",
            "供矿山设计开采",
            "作为矿山建设设计的依据",
        ),
        ("依据", "条件", "符合", "达到", "不能替代", "应提交"),
    ),
    "companion_resource_type": (
        ("伴生矿产", "伴生矿"),
        ("资源储量类型", "资源量类型", "推断资源量", "降低资源储量类型"),
        ("基本分析", "组合分析"),
    ),
    "exploration_type_factors": (
        ("勘查类型划分因素", "矿体规模", "形态变化", "厚度稳定", "构造", "组分分布"),
        ("附录E", "表 E.", "表E."),
    ),
    "basic_analysis_items": (
        ("基本分析项目", "基本分析"),
        ("分析项目", "TFe", "mFe", "全铁", "磁性铁"),
    ),
}

DEFAULT_DOCUMENT_TYPES: dict[str, tuple[str, ...]] = {
    "engineering_distance_lookup": ("standard", "national_standard", "industry_standard"),
    "projection_rule": ("standard", "national_standard", "industry_standard"),
    "projection_numeric_rule": ("standard", "national_standard", "industry_standard"),
    "projection_comparison": ("standard", "national_standard", "industry_standard"),
    "authority_responsibility": ("policy_document", "law", "regulation", "department_rule"),
    "legal_responsibility": ("law", "regulation", "department_rule"),
    "service_materials": (
        "service_guide",
        "administrative_service_guide",
        "policy_attachment",
        "policy_document",
    ),
    "service_procedure_basis": (
        "service_guide",
        "administrative_service_guide",
        "policy_document",
    ),
    "service_time_limit": ("service_guide", "administrative_service_guide"),
    "standard_selection": ("standard", "national_standard", "industry_standard"),
    "exploration_to_mining_eligibility": (
        "policy_document",
        "law",
        "regulation",
        "department_rule",
        "guidance",
        "standard",
        "national_standard",
        "industry_standard",
    ),
    "companion_resource_type": ("standard", "national_standard", "industry_standard"),
    "exploration_type_factors": ("standard", "national_standard", "industry_standard"),
    "basic_analysis_items": ("standard", "national_standard", "industry_standard"),
    "definition_explanation": ("standard", "national_standard", "industry_standard"),
    "technical_requirement_sufficiency": ("standard", "national_standard", "industry_standard", "guidance"),
    "technical_test_conformity_verification": ("standard", "national_standard", "industry_standard", "guidance"),
    "technical_stage_requirement": ("standard", "national_standard", "industry_standard", "guidance"),
}


PROTECTED_QUERY_INTENTS = {
    "engineering_distance_lookup",
    "projection_numeric_rule",
    "authority_responsibility",
    "legal_responsibility",
    "service_materials",
    "service_procedure_basis",
    "service_time_limit",
    "standard_selection",
    "exploration_to_mining_eligibility",
    "companion_resource_type",
    "exploration_type_factors",
    "basic_analysis_items",
    "projection_comparison",
    "definition_explanation",
    "technical_requirement_sufficiency",
    "technical_test_conformity_verification",
    "technical_stage_requirement",
}


def default_evidence_groups(intent: str) -> tuple[tuple[str, ...], ...]:
    return DEFAULT_EVIDENCE_GROUPS.get(intent, ())


def default_document_types(intent: str) -> tuple[str, ...]:
    return DEFAULT_DOCUMENT_TYPES.get(intent, ())


def _clean_terms(values: Any, *, limit: int = 16) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    cleaned: list[str] = []
    for value in values:
        text = normalize_user_query(str(value or ""))[:120]
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return tuple(cleaned)


def _clean_groups(values: Any) -> tuple[tuple[str, ...], ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    groups: list[tuple[str, ...]] = []
    for value in values[:8]:
        group = _clean_terms(value, limit=8)
        if group:
            groups.append(group)
    return tuple(groups)


def _authority_level(value: object) -> str:
    level = str(value or "").strip().lower()
    return level if level in AUTHORITY_LEVELS else "unknown"


def _level_from_actor_action(query: str, actor_pattern: str, action_pattern: str) -> bool:
    patterns = (
        rf"{actor_pattern}\s*(?:直接|依法|本级)?\s*{action_pattern}.{{0,16}}{_LICENSE_NAMES}",
        rf"{_LICENSE_NAMES}.{{0,16}}(?:由|是|为|属于)?\s*{actor_pattern}.{{0,4}}{action_pattern}",
        rf"{_LICENSE_NAMES}\s*(?:由|是|为)\s*{actor_pattern}(?:\s*{action_pattern})?",
    )
    return any(re.search(pattern, query) for pattern in patterns)


def extract_authority_roles(query: str) -> tuple[str, str, str, bool]:
    normalized = normalize_user_query(query)
    ministry_issuer = _level_from_actor_action(
        normalized,
        _MINISTRY_ACTORS,
        _ISSUE_ACTIONS,
    )
    province_issuer = _level_from_actor_action(
        normalized,
        _PROVINCE_ACTORS,
        _ISSUE_ACTIONS,
    )
    issuer = "unknown"
    if ministry_issuer != province_issuer:
        issuer = "ministry" if ministry_issuer else "province"

    ministry_grant = bool(
        re.search(rf"{_MINISTRY_ACTORS}.{{0,8}}{_GRANT_ACTIONS}", normalized)
        or re.search(rf"{_GRANT_ACTIONS}.{{0,12}}(?:属于|归于|归|由|在)?\s*{_MINISTRY_ACTORS}", normalized)
    )
    province_grant = bool(
        re.search(rf"{_PROVINCE_ACTORS}.{{0,8}}{_GRANT_ACTIONS}", normalized)
        or re.search(rf"{_GRANT_ACTIONS}.{{0,12}}(?:属于|归于|归|由|在)?\s*{_PROVINCE_ACTORS}", normalized)
    )
    granting = "unknown"
    if ministry_grant != province_grant:
        granting = "ministry" if ministry_grant else "province"

    filing = issuer if issuer in {"ministry", "province"} else "unknown"
    mentions_both_levels = bool(
        re.search(_MINISTRY_ACTORS, normalized)
        and re.search(_PROVINCE_ACTORS, normalized)
    )
    role_language = any(
        term in normalized
        for term in ("颁发", "核发", "发证", "出让", "权限", "本级已颁发", "我的情况")
    )
    ambiguous = issuer == "unknown" and mentions_both_levels and role_language
    return issuer, granting, filing, ambiguous


def apply_semantic_plan(base: QueryPlan, payload: dict[str, Any] | None) -> QueryPlan:
    if not payload:
        groups = tuple(
            dict.fromkeys(
                (*default_evidence_groups(base.intent), *base.required_evidence_groups)
            )
        )
        document_types = base.document_types or default_document_types(base.intent)
        return replace(base, required_evidence_groups=groups, document_types=document_types)

    canonical = normalize_user_query(str(payload.get("canonical_query") or ""))[:500]
    semantic_intent = re.sub(r"[^a-z0-9_]+", "", str(payload.get("intent") or "").lower())[:80]
    try:
        confidence = max(0.0, min(1.0, float(payload.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    intent = base.intent
    if (
        semantic_intent
        and base.intent not in PROTECTED_QUERY_INTENTS
        and (base.intent in {"general", "projection_rule"} or confidence >= 0.6)
    ):
        intent = semantic_intent

    search_mode = str(payload.get("search_mode") or "default").strip().lower()
    if search_mode not in {"default", "scoped", "comparison", "exhaustive", "catalog"}:
        search_mode = "default"
    if base.intent in PROTECTED_QUERY_INTENTS:
        search_mode = base.search_mode

    output_mode = str(payload.get("output_mode") or base.output_mode or "default").strip().lower()
    if output_mode not in {"default", "table"}:
        output_mode = base.output_mode
    if base.intent in PROTECTED_QUERY_INTENTS:
        output_mode = base.output_mode
    elif base.output_mode == "table":
        output_mode = "table"

    subject_terms = _clean_terms(payload.get("subject_terms"))
    required_terms = _clean_terms(payload.get("required_terms"))
    alternative_terms = _clean_terms(payload.get("alternative_terms"), limit=24)
    semantic_titles = _clean_terms(payload.get("candidate_titles"))
    semantic_standards = tuple(
        dict.fromkeys(
            number
            for value in _clean_terms(payload.get("standard_numbers"))
            for number in _standard_numbers(value)
        )
    )
    broad_search = search_mode in {"comparison", "exhaustive"}
    if broad_search:
        semantic_titles = tuple(term for term in semantic_titles if term in base.normalized_query)
        semantic_standards = tuple(
            number for number in semantic_standards if number in base.normalized_query
        )
    if base.intent in PROTECTED_QUERY_INTENTS:
        semantic_titles = ()
        semantic_standards = ()
    candidate_titles = tuple(dict.fromkeys((*base.candidate_title_terms, *semantic_titles)))
    standards = tuple(dict.fromkeys((*base.standard_numbers, *semantic_standards)))
    scope_origin = base.scope_origin
    if scope_origin == "none" and (semantic_titles or semantic_standards):
        scope_origin = "llm"
    raw_document_types = _clean_terms(payload.get("document_types"), limit=12)
    document_types_list: list[str] = []
    for document_type in raw_document_types:
        if document_type == "standard":
            document_types_list.extend(("standard", "national_standard", "industry_standard"))
        else:
            document_types_list.append(document_type)
    document_types = tuple(
        dict.fromkeys((*default_document_types(intent), *document_types_list))
    )
    default_groups = default_evidence_groups(intent)
    semantic_groups = _clean_groups(payload.get("required_evidence_groups"))
    protected_groups = tuple(
        dict.fromkeys((*default_groups, *base.required_evidence_groups))
    )
    groups = protected_groups or semantic_groups
    protected_terms = {term for group in groups for term in group}
    semantic_negative_terms = () if protected_groups else tuple(
        term
        for term in _clean_terms(payload.get("negative_terms"))
        if not any(term in protected or protected in term for protected in protected_terms)
    )
    negative_terms = tuple(dict.fromkeys((*base.negative_terms, *semantic_negative_terms)))
    dimensions = _clean_terms(payload.get("comparison_dimensions"), limit=8)
    semantic_issuer = _authority_level(payload.get("license_issuer_level"))
    semantic_granting = _authority_level(payload.get("mining_right_granting_level"))
    quoted_generic_authority_rule = (
        "自然资源部负责本级已颁发勘查许可证或采矿许可证" in base.normalized_query
        and "其他由省级自然资源主管部门负责" in base.normalized_query
        and base.license_issuer_level == "unknown"
    )
    if confidence < 0.8 or quoted_generic_authority_rule:
        semantic_issuer = "unknown"
    if confidence < 0.8:
        semantic_granting = "unknown"
    issuer = (
        base.license_issuer_level
        if base.license_issuer_level != "unknown"
        else semantic_issuer
    )
    granting = (
        base.mining_right_granting_level
        if base.mining_right_granting_level != "unknown"
        else semantic_granting
    )
    filing = issuer if issuer in {"ministry", "province"} else base.filing_authority
    retrieval_parts = [
        base.retrieval_query,
        canonical if canonical != base.normalized_query else "",
        *subject_terms,
        *required_terms,
        *alternative_terms,
        *candidate_titles,
        *standards,
    ]
    retrieval_query = " ".join(dict.fromkeys(part for part in retrieval_parts if part))
    return replace(
        base,
        normalized_query=(
            base.normalized_query
            if base.intent in PROTECTED_QUERY_INTENTS
            else canonical or base.normalized_query
        ),
        retrieval_query=retrieval_query or base.retrieval_query,
        intent=intent,
        candidate_title_terms=candidate_titles,
        standard_numbers=standards,
        document_types=document_types,
        subject_terms=subject_terms,
        required_terms=required_terms,
        alternative_terms=alternative_terms,
        negative_terms=negative_terms,
        required_evidence_groups=groups,
        search_mode=search_mode,
        comparison_dimensions=dimensions,
        scope_origin=scope_origin,
        output_mode=output_mode,
        planner_used=True,
        planner_confidence=confidence,
        license_issuer_level=issuer,
        mining_right_granting_level=granting,
        filing_authority=filing,
        authority_role_ambiguous=(
            base.authority_role_ambiguous and issuer == "unknown"
        ),
        exhaustive_search=(
            base.exhaustive_search
            or (
                base.intent not in PROTECTED_QUERY_INTENTS
                and search_mode in {"comparison", "exhaustive"}
            )
        ),
    )


def query_plan_from_payload(query: str, payload: dict[str, Any] | None) -> QueryPlan:
    base = understand_query(query)
    if not payload:
        return apply_semantic_plan(base, None)
    payload_scope_origin = str(payload.get("scope_origin") or "").strip().lower()
    payload_retrieval_query = normalize_user_query(str(payload.get("retrieval_query") or ""))
    semantic_target_query = (
        payload_scope_origin == "semantic_target" and bool(payload_retrieval_query)
    )
    if semantic_target_query:
        # A semantic evidence-target query may intentionally explore a second
        # legal relation. Do not reconstruct the deterministic anchor from the
        # original question on the KB service. Explicit user scopes never use
        # this branch and remain protected below.
        base = replace(
            base,
            retrieval_query=payload_retrieval_query,
            candidate_title_terms=(),
            standard_numbers=(),
            scope_origin="semantic_target",
        )
    target_document_types: tuple[str, ...] = ()
    if semantic_target_query:
        allowed_document_types = {
            "standard",
            "national_standard",
            "industry_standard",
            "policy_document",
            "policy_attachment",
            "law",
            "regulation",
            "department_rule",
            "guidance",
            "service_guide",
            "administrative_service_guide",
            "amendment",
        }
        target_types: list[str] = []
        for document_type in _clean_terms(payload.get("document_types"), limit=12):
            if document_type == "standard":
                target_types.extend(("standard", "national_standard", "industry_standard"))
            elif document_type in allowed_document_types:
                target_types.append(document_type)
        target_document_types = tuple(dict.fromkeys(target_types))
    protected = base.intent in PROTECTED_QUERY_INTENTS
    allowed = {
        "canonical_query": payload.get("normalized_query") or payload.get("canonical_query"),
        "intent": payload.get("intent"),
        "candidate_titles": None if protected else (
            payload.get("candidate_title_terms") or payload.get("candidate_titles")
        ),
        "standard_numbers": None if protected else payload.get("standard_numbers"),
        "document_types": payload.get("document_types"),
        "subject_terms": payload.get("subject_terms"),
        "required_terms": payload.get("required_terms"),
        "alternative_terms": payload.get("alternative_terms"),
        "negative_terms": payload.get("negative_terms"),
        "required_evidence_groups": payload.get("required_evidence_groups"),
        "search_mode": payload.get("search_mode"),
        "comparison_dimensions": payload.get("comparison_dimensions"),
        "output_mode": payload.get("output_mode"),
        "license_issuer_level": payload.get("license_issuer_level"),
        "mining_right_granting_level": payload.get("mining_right_granting_level"),
        "confidence": payload.get("planner_confidence") or payload.get("confidence"),
    }
    plan = apply_semantic_plan(base, allowed)
    target_type = canonical_exploration_type(payload.get("target_exploration_type"))
    focus_terms = _clean_terms(payload.get("focus_terms"))
    classification = classification_from_payload(
        payload.get("classification"),
        plan.classification
        or build_classification(
            plan.normalized_query,
            plan.intent,
            document_types=plan.document_types,
            license_issuer_level=plan.license_issuer_level,
            confidence=plan.planner_confidence or 0.72,
        ),
    )
    resolved_intent = legacy_intent_for_primary(classification.primary_intent, plan.intent)
    if (
        plan.intent in {
            "technical_requirement_sufficiency",
            "technical_test_conformity_verification",
            "technical_stage_requirement",
        }
        and classification.primary_intent == "technical_method"
    ):
        resolved_intent = plan.intent
    return replace(
        plan,
        intent=resolved_intent,
        target_exploration_type=target_type or plan.target_exploration_type,
        focus_terms=focus_terms or plan.focus_terms,
        document_types=(
            target_document_types or plan.document_types
            if semantic_target_query
            else classification.document_types or plan.document_types
        ),
        classification=classification,
        planner_used=bool(payload.get("planner_used", plan.planner_used)),
        required_evidence_groups=(
            () if semantic_target_query else plan.required_evidence_groups
        ),
        exhaustive_search=(
            plan.exhaustive_search
            if protected
            else bool(payload.get("exhaustive_search", plan.exhaustive_search))
        ),
    )


def canonical_exploration_type(value: object) -> str | None:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\s+", "", text).upper()
    text = text.removeprefix("第").removesuffix("类型").removesuffix("类")
    return EXPLORATION_TYPE_LABELS.get(text)


def normalize_user_query(query: str) -> str:
    normalized = unicodedata.normalize("NFKC", query or "")
    normalized = (
        normalized.replace("勘察", "勘查")
        .replace("工程距离", "工程间距")
        .replace("实验室流程实验", "实验室流程试验")
        .replace("相差报告", "储量报告")
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()

    def replace_type(match: re.Match[str]) -> str:
        canonical = canonical_exploration_type(match.group(1))
        return f"{canonical}类型" if canonical else match.group(0)

    normalized = _TYPE_PATTERN.sub(replace_type, normalized)
    if "勘查" in normalized and any(term in normalized for term in ENGINEERING_DISTANCE_TERMS):
        normalized = _SHORT_TYPE_PATTERN.sub(replace_type, normalized)
    return normalized


def _standard_numbers(query: str) -> tuple[str, ...]:
    numbers: list[str] = []
    seen = set()
    for match in _STANDARD_NO_PATTERN.finditer(query.upper()):
        value = re.sub(r"\s+", " ", match.group(0)).strip()
        if value not in seen:
            numbers.append(value)
            seen.add(value)
    for match in _POLICY_NO_PATTERN.finditer(query):
        value = re.sub(r"\s+", "", match.group(0)).strip()
        value = value.replace("[", "〔").replace("]", "〕")
        if value not in seen:
            numbers.append(value)
            seen.add(value)
    return tuple(numbers)


def is_context_dependent_follow_up(query: str) -> bool:
    normalized = normalize_user_query(query)
    if not normalized:
        return False
    return any(marker in normalized for marker in FOLLOW_UP_MARKERS)


def contextualize_follow_up(query: str, previous_user_question: str | None) -> str:
    current = normalize_user_query(query)
    previous = normalize_user_query(previous_user_question or "")
    if not previous or not is_context_dependent_follow_up(current):
        return current
    previous = previous.rstrip("?？。；; ")
    current = current.rstrip()
    return f"{previous}；追问：{current}"


def is_post_filing_license_steps_query(query: str) -> bool:
    normalized = normalize_user_query(query)
    has_filing = any(
        term in normalized
        for term in (
            "矿产资源储量评审备案",
            "资源储量评审备案",
            "储量评审备案",
            "储量备案",
        )
    )
    has_license_target = any(
        term in normalized
        for term in (
            "领取采矿证",
            "领采矿证",
            "拿采矿证",
            "取得采矿证",
            "领取采矿许可证",
    "取得采矿许可证",
    "采矿权登记",
    "领证",
        )
    )
    return bool(
        has_filing
        and has_license_target
        and any(term in normalized for term in POST_FILING_LICENSE_ACTION_TERMS)
    )


def service_guide_title_terms(query: str) -> tuple[str, ...]:
    terms: list[str] = []
    if is_post_filing_license_steps_query(query):
        terms.append("采矿权变更（续期）登记临时服务指南")
    elif any(term in query for term in ("压矿", "压覆审批", "压覆矿产资源")) and "审批" in query:
        terms.extend(["建设项目压覆矿产资源审批", "压覆重要矿产资源审批"])
    elif "探矿权首次登记" in query:
        terms.append("探矿权首次登记")
    elif "采矿许可" in query and "开采方式" in query:
        terms.append("采矿许可变更（开采方式）")
    elif "矿产资源储量评审备案" in query or "储量评审备案" in query:
        terms.append("矿产资源储量评审备案")
    elif "矿产资源开采方案" in query or "开采方案" in query:
        terms.append("矿产资源开采方案")
    elif "矿产资源勘查方案" in query or "勘查方案" in query:
        terms.append("矿产资源勘查方案")
    elif any(term in query for term in ("采矿权", "采矿证", "采矿许可证", "采矿许可")) and any(
        term in query for term in ("延续", "续期")
    ):
        terms.extend(["采矿权变更（续期）", "采矿许可延续"])

    is_mining_right_application = "采矿" in query and any(
        term in query for term in ("首次", "新立", "延续", "续期", "注销", "变更", "转让", "转移")
    )
    if is_mining_right_application:
        terms.append("采矿权申请资料清单及要求")
    return tuple(dict.fromkeys(terms))


def _clean_definition_target(value: str, query: str) -> str:
    target = normalize_user_query(value)
    target = re.sub(r"^[请问一下帮我解释说明]+", "", target).strip()
    target = re.sub(r"^.*?》(?:中|里)?", "", target).strip()
    target = _STANDARD_NO_PATTERN.sub("", target).strip()
    if _standard_numbers(query) and "中" in target:
        target = target.rsplit("中", 1)[-1].strip()
    target = re.sub(r"^(?:在|根据|按照|标准|术语和定义|术语定义)\s*", "", target).strip()
    return target.strip(" ：:，,。；;?？\"'“”")[:40]


def extract_definition_request(
    query: str,
) -> tuple[tuple[str, ...], str, tuple[str, ...], tuple[str, ...]]:
    normalized = normalize_user_query(query)
    if not normalized or not any(marker in normalized for marker in DEFINITION_MARKERS):
        return (), "none", (), ()

    candidates: list[str] = []
    patterns = (
        r"什么是\s*[\"“]?([^，。；;？！?\"”]{1,40})",
        r"([^，。；;？！?]{1,50}?)(?:的)?(?:定义|概念)(?:是什么|如何规定|如何定义|怎么定义)?(?:[？?。]|$)",
        r"([^，。；;？！?]{1,40}?)(?:是什么意思)(?:[？?。]|$)",
        r"([^，。；;？！?]{1,30}?)(?:是什么)(?:[？?。]|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        target = _clean_definition_target(match.group(1), normalized)
        if target:
            candidates.append(target)
            break
    if not candidates:
        return (), "none", (), ()

    raw_target = candidates[0]
    target_terms = tuple(
        dict.fromkeys(
            term.strip()
            for term in re.split(r"[、/]|(?:和|与|及)", raw_target)
            if term.strip()
        )
    )
    if not target_terms:
        return (), "none", (), ()

    definition_mode = "comparison" if len(target_terms) > 1 else "exact"
    slots: list[str] = []
    for term in target_terms:
        components = COMPOUND_DEFINITION_TERMS.get(term)
        if components:
            definition_mode = "compound"
            slots.extend(components)
        else:
            slots.append(term)
    definition_slots = tuple(dict.fromkeys(slots))

    preferred_sources: list[str] = []
    for term in definition_slots:
        source = PREFERRED_DEFINITION_SOURCES.get(term)
        if source:
            preferred_sources.extend(source)
    return (
        target_terms,
        definition_mode,
        definition_slots,
        tuple(dict.fromkeys(preferred_sources)),
    )


def understand_query(query: str) -> QueryPlan:
    original = (query or "").strip()
    normalized = normalize_user_query(original)
    target_type_match = re.search(r"([ⅠⅡⅢ])类型", normalized)
    target_type = target_type_match.group(1) if target_type_match else None

    has_engineering_distance = any(term in normalized for term in ENGINEERING_DISTANCE_TERMS)
    has_projection = any(term in normalized for term in PROJECTION_TERMS)
    has_comparison = any(term in normalized for term in COMPARISON_TERMS)
    has_related_documents = any(term in normalized for term in RELATED_DOCUMENT_TERMS)
    has_license = any(term in normalized for term in LICENSE_TERMS)
    guide_titles = service_guide_title_terms(normalized)
    post_filing_license_steps = is_post_filing_license_steps_query(normalized)
    has_mining_right_change_materials = any(
        term in normalized
        for term in ("扩大范围", "缩小范围", "矿区范围", "开采方式", "开采主矿种", "采矿权人名称", "转让")
    ) and any(term in normalized for term in SERVICE_MATERIAL_TERMS)
    has_service_materials = post_filing_license_steps or has_mining_right_change_materials or (
        (bool(guide_titles) or has_license)
        and any(term in normalized for term in SERVICE_MATERIAL_TERMS)
    )
    post_filing_workflow = any(
        term in normalized
        for term in ("资源储量评审备案", "储量评审备案", "储量备案", "评审备案")
    ) and "备案后" in normalized and any(term in normalized for term in ("登记", "流程", "步骤", "手续"))
    has_service_procedure = (bool(guide_titles) or has_license or post_filing_workflow) and any(
        term in normalized for term in SERVICE_PROCEDURE_TERMS
    )
    has_service_time_limit = bool(guide_titles) and any(term in normalized for term in SERVICE_TIME_LIMIT_TERMS)
    has_authenticity = any(term in normalized for term in AUTHENTICITY_TERMS) and any(
        term in normalized for term in RESERVE_REPORT_TERMS
    )
    has_mining_conversion = any(term in normalized for term in MINING_CONVERSION_TERMS)
    has_exploration_to_mining = has_mining_conversion and (
        any(term in normalized for term in EXPLORATION_STAGE_TERMS)
        or "探转采" in normalized
    )
    has_companion_resource_type = any(term in normalized for term in COMPANION_MINERAL_TERMS) and (
        any(term in normalized for term in RESOURCE_TYPE_TERMS)
        or (
            any(term in normalized for term in ("资源量", "资源储量"))
            and any(term in normalized for term in ("类型", "划为", "降低", "相同"))
        )
    )
    has_exploration_type_factors = "勘查类型" in normalized and any(
        term in normalized for term in EXPLORATION_FACTOR_TERMS
    )
    has_basic_analysis_items = any(term in normalized for term in BASIC_ANALYSIS_TERMS) and any(
        term in normalized for term in ("项目", "哪些", "什么", "包括", "内容", "需要测", "测哪些", "测定")
    )
    has_technical_requirement_sufficiency = (
        any(term in normalized for term in TECHNICAL_REQUIREMENT_SATISFACTION_TERMS)
        and any(term in normalized for term in TECHNICAL_STUDY_TERMS)
        and (
            any(term in normalized for term in EXPLORATION_STAGE_TERMS)
            or "要求" in normalized
            or "研究程度" in normalized
            or sum(term in normalized for term in TECHNICAL_STUDY_TERMS) >= 2
        )
    )
    has_technical_test_conformity = (
        any(term in normalized for term in TECHNICAL_STUDY_TERMS)
        and any(term in normalized for term in TECHNICAL_TEST_CONFORMITY_TERMS)
    )
    has_technical_stage_requirement = (
        any(term in normalized for term in EXPLORATION_STAGE_TERMS)
        and any(term in normalized for term in TECHNICAL_STAGE_REQUIREMENT_TERMS)
        and not has_technical_requirement_sufficiency
        and not has_technical_test_conformity
    )
    has_technical_standard_selection = (
        any(term in normalized for term in TECHNICAL_STANDARD_SELECTION_TERMS)
        and any(term in normalized for term in STANDARD_SELECTION_PHRASES)
    )
    has_authority = any(term in normalized for term in AUTHORITY_INTENT_TERMS) and any(
        term in normalized for term in AUTHORITY_TOPIC_TERMS
    )
    license_issuer, granting_level, filing_authority, authority_role_ambiguous = (
        extract_authority_roles(normalized)
    )
    broad_comparison = has_comparison and (
        has_projection
        or any(term in normalized for term in ("不同标准", "不同规范", "哪些标准", "哪些规范", "哪些规程"))
    )

    candidate_titles: list[str] = []
    intent = "general"
    retrieval_terms: list[str] = []
    explicit_standards = list(_standard_numbers(normalized))
    standards = list(explicit_standards)
    focus_terms: list[str] = []
    output_mode = "table" if any(term in normalized for term in TABLE_OUTPUT_TERMS) else "default"
    search_mode = "default"

    target_terms, definition_mode, definition_slots, preferred_definition_sources = (
        extract_definition_request(normalized)
    )

    if target_terms:
        intent = "definition_explanation"
        preferred_titles = [
            value
            for index, value in enumerate(preferred_definition_sources)
            if index % 2 == 0
        ]
        preferred_numbers = [
            value
            for index, value in enumerate(preferred_definition_sources)
            if index % 2 == 1
        ]
        candidate_titles.extend(preferred_titles)
        standards.extend(preferred_numbers)
        retrieval_terms.extend(
            [
                *(f"{term} 定义" for term in definition_slots),
                *definition_slots,
                "术语和定义",
            ]
        )
    elif has_authenticity:
        intent = "legal_responsibility"
        candidate_titles.append("矿产资源法实施条例")
        standards.append("国令第839号")
        retrieval_terms.extend(
            [
                "中华人民共和国矿产资源法实施条例",
                "第四十三条",
                "矿业权人",
                "储量报告",
                "真实性负责",
                "不得弄虚作假",
            ]
        )
    elif has_service_materials:
        intent = "service_materials"
        if post_filing_license_steps:
            candidate_titles.append("采矿权变更（续期）登记临时服务指南")
            retrieval_terms.extend(
                [
                    "采矿权变更（续期）登记临时服务指南",
                    "申请材料目录",
                    "采矿权登记申请书",
                    "矿产资源储量评审备案文件",
                    "矿业权出让收益（价款）缴纳或有偿处置证明材料",
                ]
            )
        elif "采矿权申请资料清单及要求" in guide_titles:
            standards.append("自然资规〔2023〕4号")
        if guide_titles and not post_filing_license_steps:
            candidate_titles.extend(guide_titles)
            retrieval_terms.extend([*guide_titles, "申请材料", "申请材料目录"])
        elif not post_filing_license_steps:
            candidate_titles.extend(["采矿权申请资料清单及要求", "矿产资源勘查开采登记管理"])
            standards.append("自然资规〔2023〕4号")
            retrieval_terms.extend(
                [
                    "采矿权申请资料清单及要求",
                    "附件4",
                    "申请材料",
                    "申请资料",
                ]
            )
    elif has_service_procedure:
        intent = "service_procedure_basis"
        if guide_titles:
            procedure_titles = tuple(
                title for title in guide_titles if title != "采矿权申请资料清单及要求"
            )
            candidate_titles.extend(procedure_titles)
            retrieval_terms.extend([*procedure_titles, "办理基本流程", "办理方式", "申请材料提交"])
        else:
            candidate_titles.append("矿产资源勘查开采登记管理")
            standards.append("自然资规〔2023〕4号")
            retrieval_terms.extend(
                [
                    "采矿权登记办理",
                    "自然资源部关于进一步完善矿产资源勘查开采登记管理的通知",
                    "自然资规〔2023〕4号",
                    "采矿权申请资料清单及要求",
                    "附件4",
                ]
            )
    elif has_service_time_limit:
        intent = "service_time_limit"
        time_limit_titles = tuple(
            title for title in guide_titles if title != "采矿权申请资料清单及要求"
        )
        candidate_titles.extend(time_limit_titles)
        retrieval_terms.extend([*time_limit_titles, "办结时限", "工作日"])
    elif has_exploration_to_mining:
        intent = "exploration_to_mining_eligibility"
        search_mode = "comparison"
        retrieval_terms.extend(
            [
                "探矿权转采矿权",
                "详查",
                "勘查程度",
                "经评审备案的矿产资源储量报告",
                "地质勘查报告",
                "核实报告不能替代",
                "可作为矿山设计开采依据",
                "供矿山设计开采",
                "作为矿山建设设计的依据",
                *TRANSFER_ANCHOR_STANDARD_NUMBERS,
            ]
        )
    elif has_companion_resource_type:
        intent = "companion_resource_type"
        candidate_titles.append("矿产资源综合勘查评价规范")
        standards.append("GB/T 25283-2023")
        retrieval_terms.extend(
            [
                "矿产资源综合勘查评价规范",
                "共生伴生矿产资源储量类型确定",
                "9.2",
                "9.3",
                "9.4",
                "伴生矿产基本分析",
                "伴生矿产组合分析",
                "推断资源量",
            ]
        )
    elif has_exploration_type_factors:
        intent = "exploration_type_factors"
        output_mode = "table"
        if any(term in normalized for term in ("金矿", "岩金")):
            candidate_titles.append("岩金")
            standards.append("DZ/T 0205-2020")
            retrieval_terms.extend(
                [
                    "岩金矿床勘查类型划分因素",
                    "附录E",
                    "E.1",
                    "表 E.1",
                    "表 E.2",
                    "表 E.3",
                    "表 E.4",
                    "表 E.5",
                ]
            )
    elif has_basic_analysis_items:
        intent = "basic_analysis_items"
        if any(term in normalized for term in ("铁矿", "锰矿", "铬矿")):
            candidate_titles.append("铁、锰、铬")
            standards.append("DZ/T 0200-2020")
        retrieval_terms.extend([normalized, "基本分析项目", "化学分析项目"])
    elif has_technical_stage_requirement:
        intent = "technical_stage_requirement"
        stage_section = stage_section_from_text(normalized)
        retrieval_terms.extend(
            [
                normalized,
                TECHNICAL_REQUIREMENT_STANDARD_TITLE,
                TECHNICAL_REQUIREMENT_STANDARD_NO,
                stage_section or "",
                *stage_requirement_clauses(normalized),
                "资源量规模",
                "矿石加工选冶难易程度",
            ]
        )
    elif has_technical_standard_selection:
        # Questions such as "哪个规范规定了金矿选矿试验要求" ask for the
        # governing document. They should not fall through to a generic LLM
        # answer, which can invent plausible-looking standard titles.
        intent = "standard_selection"
        search_mode = "catalog"
        candidate_titles.append(TECHNICAL_REQUIREMENT_STANDARD_TITLE)
        standards.append(TECHNICAL_REQUIREMENT_STANDARD_NO)
        retrieval_terms.extend(
            [
                TECHNICAL_REQUIREMENT_STANDARD_TITLE,
                TECHNICAL_REQUIREMENT_STANDARD_NO,
                "矿石加工选冶技术性能",
                "选矿试验",
            ]
        )
    elif has_technical_test_conformity:
        intent = "technical_test_conformity_verification"
        retrieval_terms.extend(
            [
                normalized,
                "矿石加工选冶技术性能试验研究程度要求",
                "样品代表性",
                "试验规模",
                "设备",
                "运行时间",
                "试验记录",
            ]
        )
    elif has_technical_requirement_sufficiency:
        intent = "technical_requirement_sufficiency"
        retrieval_terms.extend(
            [
                normalized,
                "矿石加工选冶技术性能试验研究程度要求",
                "详查阶段",
                "试验研究程度分类",
                "可选性试验",
                "实验室流程试验",
                "在可选性试验的基础上",
                "必要时",
                "满足要求 替代 覆盖",
            ]
        )
    elif has_engineering_distance and not (has_projection and has_comparison):
        intent = "engineering_distance_lookup"
        if any(term in normalized for term in ("金矿", "岩金")):
            candidate_titles.append("岩金")
            retrieval_terms.extend(
                [
                    "岩金",
                    "参考基本勘查工程间距",
                    "表 F.1",
                    "勘查工程间距",
                    f"{target_type}类型" if target_type else "勘查类型",
                    "坑探",
                    "钻探",
                    "穿脉",
                    "沿脉",
                    "走向",
                    "倾斜",
                ]
            )
    elif has_projection and has_comparison:
        intent = "projection_comparison"
        search_mode = "comparison"
        retrieval_terms.extend(
            [
                "矿体外推 工程间距 尖推 平推",
                *PROJECTION_REFERENCE_QUERIES,
            ]
        )
    elif "无限外推" in normalized and (
        any(term in normalized for term in PROJECTION_RATIO_TERMS)
        or any(term in normalized for term in ("多少", "怎么推", "如何外推", "比例"))
    ):
        intent = "projection_numeric_rule"
        candidate_titles.append("固体矿产资源量估算规程 第1部分：通则")
        standards.append("DZ/T 0338.1-2020")
        retrieval_terms.extend(
            [
                "6.2.2.1",
                "无限外推",
                "见矿工程向外再没有工程控制",
                "经验工程间距1/2尖推",
            ]
        )
    elif has_projection:
        intent = "projection_rule"

    if intent == "general" and has_comparison and any(
        term in normalized
        for term in ("各矿种", "分矿种", "单矿种", "各类规范", "各规范", "各标准", "各文件")
    ):
        intent = "cross_document_audit"
        search_mode = "comparison"
        retrieval_terms.extend(["比较主题", "适用条件", "具体差异", normalized])

    if intent == "general" and any(
        term in normalized
        for term in ("还有效", "是否有效", "现行", "废止", "替代", "最新版", "新版本")
    ):
        intent = "standard_selection"
        search_mode = "catalog"
        retrieval_terms.extend(["标准状态", "现行", "废止", "替代", normalized])

    if has_authority and intent == "general":
        intent = "authority_responsibility"
        candidate_titles.append("深化矿产资源管理改革若干事项")
        standards.append("自然资规〔2023〕6号")
        retrieval_terms.extend(
            [
                "矿产资源储量评审备案范围和权限",
                "自然资源部负责本级已颁发勘查许可证或采矿许可证",
                "其他由省级自然资源主管部门负责",
            ]
        )

    if has_related_documents and intent == "general":
        intent = "related_documents"
        search_mode = "exhaustive"
        topic = re.split(r"[;；]\s*追问[:：]", normalized, maxsplit=1)[0].strip()
        retrieval_terms.append(topic or normalized)
        focus_terms.extend(term for term in FOLLOW_UP_FOCUS_TERMS if term in topic)

    if intent == "general" and any(
        term in normalized
        for term in ("全套资料", "全套文件", "全套执行文件", "全套标准", "项目需要哪些标准", "各阶段分别用什么规范", "项目资料体系")
    ):
        intent = "related_documents"
        search_mode = "exhaustive"
        retrieval_terms.extend(["项目阶段", "专业", "文件清单", normalized])

    if any(term in normalized for term in ("沙金", "砂金")) and any(
        term in normalized for term in ("哪个标准", "哪个规范", "使用", "适用", "采用")
    ):
        intent = "standard_selection"
        candidate_titles.append("金属砂矿类")
        retrieval_terms.extend(["金属砂矿类", "砂金", "DZ/T 0208-2020"])

    # The lexicon expands retrieval only after deterministic/LLM intent selection.
    # Background and retrieval-only entries may enrich any in-scope plan, but never
    # replace the selected primary intent or inject their negative constraints.
    governed_retrieval_matches = [
        match
        for match in matched_lexicon_entries(normalized, purpose="retrieval")
        if match.get("intent_label") in {intent, "background_context"}
        or not match.get("intent_trigger_enabled", True)
    ]
    for match in governed_retrieval_matches:
        retrieval_terms.append(match["canonical_term"])
        retrieval_terms.extend(match.get("positive_expansions") or [])
        # Background context may add terminology for recall, but its evidence
        # constraints belong to the business intent it was authored for. Carrying
        # them into every query pollutes unrelated retrieval (for example, an
        # authority pattern in a mineral-processing question).
        if match.get("intent_label") == intent:
            retrieval_terms.extend(match.get("evidence_required_patterns") or [])

    governed_constraint_matches = [
        match
        for match in governed_retrieval_matches
        if match.get("intent_label") == intent
    ]
    governed_negative_terms = tuple(
        dict.fromkeys(
            term
            for match in governed_constraint_matches
            for term in (match.get("negative_terms") or [])
            if term
        )
    )
    default_evidence_terms = {
        term for group in default_evidence_groups(intent) for term in group
    }
    governed_evidence_groups = tuple(
        (pattern,)
        for pattern in dict.fromkeys(
            pattern
            for match in governed_constraint_matches
            for pattern in (match.get("evidence_required_patterns") or [])
            if pattern
        )
        if not any(
            pattern in default_term or default_term in pattern
            for default_term in default_evidence_terms
        )
    )

    if not retrieval_terms:
        retrieval_terms.append(normalized)
    elif normalized and intent != "related_documents":
        retrieval_terms.append(normalized)
    retrieval_terms.extend(standards)

    deduped_terms: list[str] = []
    seen_terms = set()
    for term in retrieval_terms:
        clean = term.strip()
        if clean and clean not in seen_terms:
            deduped_terms.append(clean)
            seen_terms.add(clean)

    scope_origin = "user" if explicit_standards else (
        "deterministic" if candidate_titles or standards else "none"
    )
    document_types = default_document_types(intent)
    # Status verification is a governance lookup, not a standard-body search.
    # It must include policies, amendments and regulations even though the
    # legacy renderer continues to use the standard_selection intent.
    if any(
        term in normalized
        for term in ("还有效", "是否有效", "现行", "废止", "替代", "最新版", "新版本")
    ):
        document_types = (
            "standard",
            "national_standard",
            "industry_standard",
            "amendment",
            "policy_document",
            "policy_attachment",
            "law",
            "regulation",
            "department_rule",
            "guidance",
            "service_guide",
            "administrative_service_guide",
        )
    classification = build_classification(
        normalized,
        intent,
        document_types=document_types,
        license_issuer_level=license_issuer,
    )
    return QueryPlan(
        original_query=original,
        normalized_query=normalized,
        retrieval_query=" ".join(deduped_terms),
        intent=intent,
        target_exploration_type=target_type,
        candidate_title_terms=tuple(dict.fromkeys(candidate_titles)),
        standard_numbers=tuple(dict.fromkeys(standards)),
        focus_terms=tuple(dict.fromkeys(focus_terms)),
        negative_terms=governed_negative_terms,
        required_evidence_groups=governed_evidence_groups,
        scope_origin=scope_origin,
        output_mode=output_mode,
        search_mode=search_mode,
        license_issuer_level=license_issuer,
        mining_right_granting_level=granting_level,
        filing_authority=filing_authority,
        authority_role_ambiguous=authority_role_ambiguous,
        target_terms=target_terms,
        definition_mode=definition_mode,
        definition_slots=definition_slots,
        preferred_definition_sources=preferred_definition_sources,
        document_types=document_types,
        classification=classification,
        exhaustive_search=(
            (
                broad_comparison
                and (
                    intent == "projection_comparison"
                    or intent not in PROTECTED_QUERY_INTENTS
                )
            )
            or has_related_documents
        ),
    )
