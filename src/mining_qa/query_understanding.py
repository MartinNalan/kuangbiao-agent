from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


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
    exhaustive_search: bool = False

    @property
    def has_candidate_scope(self) -> bool:
        return bool(self.candidate_title_terms or self.standard_numbers) and not self.exhaustive_search


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
            candidate_titles.extend(guide_titles)
            retrieval_terms.extend([*guide_titles, "办理基本流程", "办理方式", "申请材料提交"])
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
        candidate_titles.extend(guide_titles)
        retrieval_terms.extend([*guide_titles, "办结时限", "工作日"])
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
