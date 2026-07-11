from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, replace
from typing import Any


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
ENGINEERING_DISTANCE_TERMS = ("工程间距", "基本工程间距", "勘查工程间距", "工程距离")
PROJECTION_TERMS = ("矿体外推", "有限外推", "无限外推", "尖推", "平推")
PROJECTION_RATIO_TERMS = ("1/2", "1/4", "二分之一", "四分之一", "一半")
LICENSE_TERMS = ("采矿证", "采矿许可证", "采矿权")
SERVICE_MATERIAL_TERMS = (
    "提交什么材料",
    "提交哪些材料",
    "需要什么材料",
    "需要哪些材料",
    "申请材料",
    "申请资料",
    "资料清单",
    "材料清单",
)
SERVICE_PROCEDURE_TERMS = (
    "怎么办理",
    "如何办理",
    "办理流程",
    "办理程序",
    "办理依据",
    "依据哪个文件",
    "依据什么文件",
    "按哪个文件",
)
SERVICE_TIME_LIMIT_TERMS = ("办结时限", "办理时限", "需要多久", "多久办结", "多少个工作日", "时限是多久")
AUTHORITY_INTENT_TERMS = ("哪个机构", "去哪个机构", "谁负责", "哪一级部门", "哪个部门", "权限归属")
AUTHORITY_TOPIC_TERMS = ("储量评审", "储量报告评审", "评审备案", "矿产资源储量评审备案")
AUTHENTICITY_TERMS = ("真实性", "真实准确", "弄虚作假", "真实性负责")
RESERVE_REPORT_TERMS = ("资源储量报告", "矿产资源储量报告", "储量报告")
EXPLORATION_STAGE_TERMS = ("详查", "勘探", "普查", "勘查程度", "勘查阶段")
MINING_CONVERSION_TERMS = ("转采", "探矿权转采矿权", "申请采矿权", "采矿权新立")
RELATED_DOCUMENT_TERMS = ("其他文件", "还有哪些文件", "还有什么文件", "其他规定", "还有其他规定")
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
    planner_used: bool = False
    planner_confidence: float = 0.0
    exhaustive_search: bool = False

    @property
    def has_candidate_scope(self) -> bool:
        return bool(self.candidate_title_terms or self.standard_numbers)

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


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
        ("详查", "勘查程度", "勘查阶段", "地质勘查报告", "核实报告"),
        ("探矿权转采矿权", "转采", "申请采矿权", "采矿权新立"),
        ("依据", "条件", "符合", "达到", "不能替代", "应提交"),
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


def apply_semantic_plan(base: QueryPlan, payload: dict[str, Any] | None) -> QueryPlan:
    if not payload:
        groups = base.required_evidence_groups or default_evidence_groups(base.intent)
        document_types = base.document_types or default_document_types(base.intent)
        return replace(base, required_evidence_groups=groups, document_types=document_types)

    canonical = normalize_user_query(str(payload.get("canonical_query") or ""))[:500]
    semantic_intent = re.sub(r"[^a-z0-9_]+", "", str(payload.get("intent") or "").lower())[:80]
    try:
        confidence = max(0.0, min(1.0, float(payload.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    intent = base.intent
    if semantic_intent and (base.intent in {"general", "projection_rule"} or confidence >= 0.6):
        intent = semantic_intent

    search_mode = str(payload.get("search_mode") or "default").strip().lower()
    if search_mode not in {"default", "scoped", "comparison", "exhaustive", "catalog"}:
        search_mode = "default"

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
    candidate_titles = tuple(dict.fromkeys((*base.candidate_title_terms, *semantic_titles)))
    standards = tuple(dict.fromkeys((*base.standard_numbers, *semantic_standards)))
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
    groups = default_groups or semantic_groups
    protected_terms = {term for group in groups for term in group}
    negative_terms = () if default_groups else tuple(
        term
        for term in _clean_terms(payload.get("negative_terms"))
        if not any(term in protected or protected in term for protected in protected_terms)
    )
    dimensions = _clean_terms(payload.get("comparison_dimensions"), limit=8)
    retrieval_parts = [
        canonical or base.retrieval_query,
        *subject_terms,
        *required_terms,
        *alternative_terms,
        *candidate_titles,
        *standards,
    ]
    retrieval_query = " ".join(dict.fromkeys(part for part in retrieval_parts if part))
    return replace(
        base,
        normalized_query=canonical or base.normalized_query,
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
        planner_used=True,
        planner_confidence=confidence,
        exhaustive_search=base.exhaustive_search or search_mode in {"comparison", "exhaustive"},
    )


def query_plan_from_payload(query: str, payload: dict[str, Any] | None) -> QueryPlan:
    base = understand_query(query)
    if not payload:
        return apply_semantic_plan(base, None)
    allowed = {
        "canonical_query": payload.get("normalized_query") or payload.get("canonical_query"),
        "intent": payload.get("intent"),
        "candidate_titles": payload.get("candidate_title_terms") or payload.get("candidate_titles"),
        "standard_numbers": payload.get("standard_numbers"),
        "document_types": payload.get("document_types"),
        "subject_terms": payload.get("subject_terms"),
        "required_terms": payload.get("required_terms"),
        "alternative_terms": payload.get("alternative_terms"),
        "negative_terms": payload.get("negative_terms"),
        "required_evidence_groups": payload.get("required_evidence_groups"),
        "search_mode": payload.get("search_mode"),
        "comparison_dimensions": payload.get("comparison_dimensions"),
        "confidence": payload.get("planner_confidence") or payload.get("confidence"),
    }
    plan = apply_semantic_plan(base, allowed)
    target_type = canonical_exploration_type(payload.get("target_exploration_type"))
    focus_terms = _clean_terms(payload.get("focus_terms"))
    return replace(
        plan,
        target_exploration_type=target_type or plan.target_exploration_type,
        focus_terms=focus_terms or plan.focus_terms,
        planner_used=bool(payload.get("planner_used", plan.planner_used)),
        exhaustive_search=bool(payload.get("exhaustive_search", plan.exhaustive_search)),
    )


def canonical_exploration_type(value: object) -> str | None:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\s+", "", text).upper()
    text = text.removeprefix("第").removesuffix("类型").removesuffix("类")
    return EXPLORATION_TYPE_LABELS.get(text)


def normalize_user_query(query: str) -> str:
    normalized = unicodedata.normalize("NFKC", query or "")
    normalized = normalized.replace("勘察", "勘查").replace("工程距离", "工程间距")
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


def service_guide_title_terms(query: str) -> tuple[str, ...]:
    terms: list[str] = []
    if "探矿权首次登记" in query:
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
    has_service_materials = (bool(guide_titles) or has_license) and any(
        term in normalized for term in SERVICE_MATERIAL_TERMS
    )
    has_service_procedure = (bool(guide_titles) or has_license) and any(
        term in normalized for term in SERVICE_PROCEDURE_TERMS
    )
    has_service_time_limit = bool(guide_titles) and any(term in normalized for term in SERVICE_TIME_LIMIT_TERMS)
    has_authenticity = any(term in normalized for term in AUTHENTICITY_TERMS) and any(
        term in normalized for term in RESERVE_REPORT_TERMS
    )
    has_exploration_to_mining = any(term in normalized for term in EXPLORATION_STAGE_TERMS) and any(
        term in normalized for term in MINING_CONVERSION_TERMS
    )
    has_authority = any(term in normalized for term in AUTHORITY_INTENT_TERMS) and any(
        term in normalized for term in AUTHORITY_TOPIC_TERMS
    )
    broad_comparison = has_comparison and (
        has_projection
        or any(term in normalized for term in ("不同标准", "不同规范", "哪些标准", "哪些规范", "哪些规程"))
    )

    candidate_titles: list[str] = []
    intent = "general"
    retrieval_terms: list[str] = []
    standards = list(_standard_numbers(normalized))
    focus_terms: list[str] = []

    if has_authenticity:
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
        if "采矿权申请资料清单及要求" in guide_titles:
            standards.append("自然资规〔2023〕4号")
        if guide_titles:
            candidate_titles.extend(guide_titles)
            retrieval_terms.extend([*guide_titles, "申请材料", "申请材料目录"])
        else:
            candidate_titles.extend(["采矿权延续", "矿产资源勘查开采登记管理"])
            standards.append("自然资规〔2023〕4号")
            retrieval_terms.extend(
                [
                    "采矿权延续登记",
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
        retrieval_terms.extend(
            [
                "探矿权转采矿权",
                "详查",
                "勘查程度",
                "经评审备案的矿产资源储量报告",
                "地质勘查报告",
                "核实报告不能替代",
            ]
        )
    elif has_engineering_distance:
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
        topic = re.split(r"[;；]\s*追问[:：]", normalized, maxsplit=1)[0].strip()
        retrieval_terms.append(topic or normalized)
        focus_terms.extend(term for term in FOLLOW_UP_FOCUS_TERMS if term in topic)

    if any(term in normalized for term in ("沙金", "砂金")) and any(
        term in normalized for term in ("哪个标准", "哪个规范", "使用", "适用", "采用")
    ):
        intent = "standard_selection"
        candidate_titles.append("金属砂矿类")
        retrieval_terms.extend(["金属砂矿类", "砂金", "DZ/T 0208-2020"])

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

    return QueryPlan(
        original_query=original,
        normalized_query=normalized,
        retrieval_query=" ".join(deduped_terms),
        intent=intent,
        target_exploration_type=target_type,
        candidate_title_terms=tuple(dict.fromkeys(candidate_titles)),
        standard_numbers=tuple(dict.fromkeys(standards)),
        focus_terms=tuple(dict.fromkeys(focus_terms)),
        exhaustive_search=broad_comparison or has_related_documents,
    )
