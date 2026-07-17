from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict, dataclass, replace
from typing import Any

from .auth import get_account_store
from .config import Settings, get_settings
from .knowledge_client import KnowledgeClient
from .llm_client import LLMClient
from .prompt_registry import prompt_text
from .query_understanding import (
    PROJECTION_REFERENCE_STANDARD_NUMBERS,
    QueryPlan,
    TRANSFER_ANCHOR_STANDARD_NUMBERS,
    TRANSFER_EQUIVALENT_TERMS,
    TRANSFER_REPORT_OBJECT_TERMS,
    default_document_types,
    default_evidence_groups,
    is_post_filing_license_steps_query,
    normalize_user_query,
    query_plan_from_payload,
    understand_query,
)
from .technical_stage_requirements import (
    TECHNICAL_REQUIREMENT_STANDARD_NO,
    TECHNICAL_REQUIREMENT_STANDARD_TITLE,
    stage_requirement_clauses,
    stage_requirement_label,
)
from .schemas import (
    Limitations,
    QuotaInfo,
    ResearchCoverage,
    ResearchProgress,
    ResearchResult,
    ResearchTaskResponse,
    Source,
)
from .usage_log import UsageLogger


logger = logging.getLogger(__name__)

RESEARCH_CLASSIFICATIONS = {
    "consistent",
    "stricter",
    "looser",
    "equivalent_wording",
    "scope_differs",
    "special_provision",
    "not_covered",
    "insufficient_evidence",
    "possible_conflict",
}

CLASSIFICATION_LABELS = {
    "consistent": "一致",
    "stricter": "更严格",
    "looser": "更宽松",
    "equivalent_wording": "表述不同但实质等价",
    "scope_differs": "适用范围不同",
    "special_provision": "特别规定",
    "not_covered": "未发现直接规定",
    "insufficient_evidence": "证据不足",
    "possible_conflict": "疑似冲突，需人工复核",
}

TRANSFER_RELATION_TERMS = (
    "探矿权转采矿权",
    "转采",
    "采矿权新立",
    "申请采矿权",
    *TRANSFER_EQUIVALENT_TERMS,
)
TRANSFER_OBJECT_TERMS = (
    "详查报告",
    "地质勘查报告",
    "矿产资源储量报告",
    "经评审备案",
    "勘查程度",
    "详查（含）以上程度",
    *TRANSFER_REPORT_OBJECT_TERMS,
)

SERVICE_CHANGE_SECTIONS = {
    "expand_area": "扩大矿区范围",
    "shrink_area": "缩小矿区范围",
    "mineral_or_mining_method": "开采主矿种、开采方式",
    "holder_name": "采矿权人名称",
    "transfer": "转让",
}


@dataclass(frozen=True)
class ResearchPlan:
    canonical_question: str
    intent: str = "cross_document_audit"
    strategy: str = "cross_document_comparison"
    anchor_titles: tuple[str, ...] = ()
    anchor_standard_numbers: tuple[str, ...] = ()
    corpus_title_terms: tuple[str, ...] = ()
    corpus_standard_numbers: tuple[str, ...] = ()
    document_types: tuple[str, ...] = (
        "standard",
        "national_standard",
        "industry_standard",
        "policy_document",
        "law",
        "regulation",
        "department_rule",
        "guidance",
    )
    comparison_dimensions: tuple[str, ...] = ()
    evidence_queries: tuple[str, ...] = ()
    required_evidence_groups: tuple[tuple[str, ...], ...] = ()
    scope_note: str = ""
    planner_used: bool = False
    query_classification: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def _clean_list(value: object, *, limit: int, item_limit: int = 120) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    items: list[str] = []
    for raw in value:
        item = normalize_user_query(str(raw or ""))[:item_limit]
        if item and item not in items:
            items.append(item)
        if len(items) >= limit:
            break
    return tuple(items)


def _clean_groups(value: object) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        return ()
    groups: list[tuple[str, ...]] = []
    for raw in value[:8]:
        group = _clean_list(raw, limit=10, item_limit=80)
        if group:
            groups.append(group)
    return tuple(groups)


def _explicit_titles(question: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in re.findall(r"《([^》]{2,80})》", question) if value.strip()))


def _projection_focus(question: str) -> tuple[str, tuple[str, ...]] | None:
    if "无限外推" in question:
        return (
            "无限外推 见矿工程向外无工程控制 见矿工程外无控制工程 边缘见矿工程外",
            (
                "无限外推",
                "见矿工程向外再没有工程控制",
                "见矿工程向外无工程控制",
                "见矿工程外无控制工程",
                "边缘见矿工程外",
                "边缘见矿工程向外",
            ),
        )
    if "有限外推" in question:
        return (
            "有限外推 相邻工程一个见矿一个不见矿 相邻工程未见矿",
            (
                "有限外推",
                "相邻工程一个见矿",
                "相邻的两个工程一个见矿",
                "相邻工程未见矿",
            ),
        )
    return None


class ResearchPlanner:
    def __init__(self, settings: Settings, llm: LLMClient):
        self.settings = settings
        self.llm = llm

    async def plan(
        self,
        question: str,
        base_plan: QueryPlan | None = None,
    ) -> ResearchPlan:
        fallback = self._fallback(question, base_plan)
        if not self.llm.enabled:
            return fallback
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 geowiki 深度研究规划器。只制定本地私有知识库检索计划，不回答问题，"
                    "不使用互联网，不把模型记忆当作标准证据。识别基准文件、候选文件集合的标题模式、"
                    "文件类型、比较维度和最多3条证据查询。候选集合必须能够从标准目录枚举，"
                    "例如‘各分矿种规范’应使用‘矿产地质勘查规范’作为 corpus_title_terms，"
                    "不能只列出你记得的几个标准号。required_evidence_groups 组间为 AND、组内为 OR，"
                    "用于排除只共享普通关键词但没有目标关系的条款。明确区分基准文件和待审查文件。"
                    "复合行政办理问题必须拆解为独立的证据查询和办理环节：先检索权利取得、配置条件或例外，"
                    "再检索登记变更、申请材料或后续程序。不能因问题含有‘变更’就只审查变更登记文件。"
                    "模型提出的文件名称或文号仅是知识库待核验候选，不得代替检索到的原文。只返回 JSON。\n"
                    f"{prompt_text(self.settings, 'retrieval_planner', primary_intent=(fallback.query_classification or {}).get('primary_intent'))}"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question,
                        "deterministic_fallback": fallback.to_payload(),
                        "output_schema": {
                            "canonical_question": "专业且完整的问题表达",
                            "anchor_titles": ["用户指定的基准文件名称"],
                            "anchor_standard_numbers": [],
                            "corpus_title_terms": ["用于目录枚举的标题共同部分"],
                            "corpus_standard_numbers": [],
                            "document_types": ["industry_standard"],
                            "comparison_dimensions": ["比较维度"],
                            "evidence_queries": ["用于每份候选文件内部检索的查询"],
                            "required_evidence_groups": [["每组至少命中一个术语，组间为AND关系"]],
                            "scope_note": "候选范围说明",
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            raw = await self.llm.complete_json(
                messages,
                max_tokens=self.settings.research_planner_max_tokens,
            )
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                return fallback
        except Exception:
            return fallback

        allowed_types = {
            "standard",
            "national_standard",
            "industry_standard",
            "policy_document",
            "law",
            "regulation",
            "department_rule",
            "guidance",
            "service_guide",
            "administrative_service_guide",
            "policy_attachment",
        }
        document_types = tuple(
            value
            for value in _clean_list(payload.get("document_types"), limit=12)
            if value in allowed_types
        )
        plan = ResearchPlan(
            canonical_question=normalize_user_query(
                str(payload.get("canonical_question") or fallback.canonical_question)
            )[:600],
            intent=fallback.intent,
            strategy=fallback.strategy,
            anchor_titles=_clean_list(payload.get("anchor_titles"), limit=8) or fallback.anchor_titles,
            anchor_standard_numbers=tuple(
                dict.fromkeys(
                    (
                        *fallback.anchor_standard_numbers,
                        *_clean_list(payload.get("anchor_standard_numbers"), limit=8),
                    )
                )
            ),
            corpus_title_terms=_clean_list(payload.get("corpus_title_terms"), limit=12)
            or fallback.corpus_title_terms,
            corpus_standard_numbers=_clean_list(payload.get("corpus_standard_numbers"), limit=20)
            or fallback.corpus_standard_numbers,
            document_types=document_types or fallback.document_types,
            comparison_dimensions=_clean_list(payload.get("comparison_dimensions"), limit=10)
            or fallback.comparison_dimensions,
            evidence_queries=_clean_list(payload.get("evidence_queries"), limit=3, item_limit=500)
            or fallback.evidence_queries,
            required_evidence_groups=_clean_groups(payload.get("required_evidence_groups"))
            or fallback.required_evidence_groups,
            scope_note=normalize_user_query(str(payload.get("scope_note") or fallback.scope_note))[:400],
            planner_used=True,
            query_classification=fallback.query_classification,
        )
        return self._enforce_protected_scope(question, plan, base_plan)

    @staticmethod
    def _enforce_protected_scope(
        question: str,
        plan: ResearchPlan,
        base_plan: QueryPlan | None = None,
    ) -> ResearchPlan:
        base = base_plan or understand_query(question)
        if base.intent == "service_materials":
            fallback = ResearchPlanner._fallback(question, base)
            if is_post_filing_license_steps_query(question):
                return replace(
                    plan,
                    intent="service_materials",
                    strategy="document_inventory",
                    anchor_titles=fallback.anchor_titles,
                    anchor_standard_numbers=(),
                    corpus_title_terms=fallback.corpus_title_terms,
                    corpus_standard_numbers=(),
                    document_types=fallback.document_types,
                    comparison_dimensions=fallback.comparison_dimensions,
                    evidence_queries=fallback.evidence_queries,
                    required_evidence_groups=fallback.required_evidence_groups,
                    scope_note=(
                        "只检索采矿权变更（续期）登记办事指南中的申请材料目录，"
                        "将材料要求转换为评审备案后至领证前的待办手续。"
                    ),
                )
            return replace(
                plan,
                intent="service_materials",
                strategy="document_inventory",
                anchor_titles=tuple(
                    dict.fromkeys(("采矿权申请资料清单及要求", *plan.anchor_titles))
                ),
                anchor_standard_numbers=tuple(
                    dict.fromkeys(("自然资规〔2023〕4号附件4", *plan.anchor_standard_numbers))
                ),
                corpus_title_terms=fallback.corpus_title_terms,
                corpus_standard_numbers=(),
                document_types=fallback.document_types,
                comparison_dimensions=fallback.comparison_dimensions,
                evidence_queries=fallback.evidence_queries,
                required_evidence_groups=fallback.required_evidence_groups,
                scope_note="只检索采矿权申请资料清单及要求、对应政策附件和办事指南，不按发证机关分叉。",
            )
        if base.intent == "exploration_to_mining_eligibility":
            fallback = ResearchPlanner._fallback(question, base)
            return replace(
                plan,
                intent=fallback.intent,
                strategy=fallback.strategy,
                anchor_standard_numbers=tuple(
                    dict.fromkeys((*fallback.anchor_standard_numbers, *plan.anchor_standard_numbers))
                ),
                corpus_title_terms=tuple(
                    dict.fromkeys((*fallback.corpus_title_terms, *plan.corpus_title_terms))
                )[:12],
                corpus_standard_numbers=(),
                document_types=tuple(
                    dict.fromkeys((*fallback.document_types, *plan.document_types))
                ),
                comparison_dimensions=fallback.comparison_dimensions,
                evidence_queries=fallback.evidence_queries,
                required_evidence_groups=(),
                scope_note=(
                    "围绕探矿权转采矿权及其正向等价表述检索一般政策、分矿种特殊规定和报告类型限制。"
                ),
            )
        if base.intent == "technical_stage_requirement":
            fallback = ResearchPlanner._fallback(question, base)
            return replace(
                plan,
                intent=fallback.intent,
                strategy=fallback.strategy,
                anchor_titles=fallback.anchor_titles,
                anchor_standard_numbers=fallback.anchor_standard_numbers,
                corpus_title_terms=fallback.corpus_title_terms,
                corpus_standard_numbers=fallback.corpus_standard_numbers,
                document_types=fallback.document_types,
                comparison_dimensions=fallback.comparison_dimensions,
                evidence_queries=fallback.evidence_queries,
                required_evidence_groups=fallback.required_evidence_groups,
                scope_note=fallback.scope_note,
            )
        focus = _projection_focus(question)
        if not focus:
            return plan
        fallback = ResearchPlanner._fallback(question, base)
        focus_query, focus_group = focus
        groups = list(plan.required_evidence_groups)
        if focus_group not in groups:
            groups.append(focus_group)
        queries = list(plan.evidence_queries)
        if focus_query not in queries:
            queries.append(focus_query)
        return replace(
            plan,
            intent=fallback.intent,
            strategy=fallback.strategy,
            anchor_standard_numbers=tuple(
                dict.fromkeys((*fallback.anchor_standard_numbers, *plan.anchor_standard_numbers))
            ),
            corpus_title_terms=tuple(
                dict.fromkeys((*fallback.corpus_title_terms, *plan.corpus_title_terms))
            )[:12],
            document_types=tuple(
                dict.fromkeys((*fallback.document_types, *plan.document_types))
            ),
            comparison_dimensions=fallback.comparison_dimensions,
            evidence_queries=tuple(dict.fromkeys(queries))[:3],
            required_evidence_groups=tuple(groups),
            scope_note=(
                "围绕用户限定的外推类型，检索分矿种规范，并固定保留 DZ/T 0338.1-2020 "
                "6.2.2.1 与 DZ/T 0338.2-2020 5.4.2 作为无限、有限外推对照。"
            ),
        )

    @staticmethod
    def _fallback(
        question: str,
        base_plan: QueryPlan | None = None,
    ) -> ResearchPlan:
        base = base_plan or understand_query(question)
        post_filing_steps = is_post_filing_license_steps_query(question)
        title_terms: list[str] = []
        document_types = list(base.document_types or default_document_types(base.intent))
        if base.intent == "service_materials":
            if post_filing_steps:
                title_terms.append("采矿权变更（续期）登记临时服务指南")
                document_types = ["service_guide", "administrative_service_guide"]
            else:
                title_terms.append("采矿权申请资料清单及要求")
                document_types = list(default_document_types("service_materials"))
        elif base.intent == "exploration_to_mining_eligibility":
            title_terms.append("矿产地质勘查规范")
            document_types = [
                "policy_document",
                "law",
                "regulation",
                "department_rule",
                "guidance",
                "standard",
                "national_standard",
                "industry_standard",
            ]
        elif base.intent == "technical_stage_requirement":
            title_terms.append(TECHNICAL_REQUIREMENT_STANDARD_TITLE)
            document_types = ["standard", "national_standard", "industry_standard"]
        elif any(term in question for term in ("分矿种规范", "单矿种规范", "各矿种规范", "矿种勘查规范")):
            title_terms.append("矿产地质勘查规范")
            document_types = ["standard", "national_standard", "industry_standard"]
        if "外推" in question:
            title_terms.extend(["矿产地质勘查规范", "固体矿产资源量估算规程"])
            document_types = ["standard", "national_standard", "industry_standard"]
        if not title_terms:
            title_terms.extend(base.candidate_title_terms)
        if not document_types:
            document_types = [
                "standard",
                "national_standard",
                "industry_standard",
                "policy_document",
                "law",
                "regulation",
                "department_rule",
                "guidance",
            ]
        dimensions = base.comparison_dimensions or (
            "适用范围",
            "条件和前提",
            "具体技术要求或数值",
            "例外和特别规定",
        )
        evidence_queries = (base.retrieval_query or base.normalized_query,)
        required_evidence_groups: tuple[tuple[str, ...], ...] = ()
        if base.intent == "service_materials":
            if post_filing_steps:
                dimensions = ("下一步办理事项", "对应申请材料", "适用条件", "缴费或有偿处置")
                evidence_queries = (
                    "采矿权变更（续期）登记 申请材料目录 采矿权登记申请书 矿产资源储量评审备案文件",
                    "矿业权出让收益（价款）缴纳或有偿处置证明材料",
                )
                required_evidence_groups = (
                    ("申请材料目录", "采矿权登记申请书"),
                    ("矿产资源储量评审备案文件", "矿山储量年报"),
                    ("矿业权出让收益", "有偿处置证明材料"),
                )
            else:
                application = ResearchTaskRunner._service_application_label(base.normalized_query)
                dimensions = ("办理类型", "申请材料", "特殊适用条件", "提交形式")
                evidence_queries = (
                    f"采矿权{application or ''}申请资料清单及要求 附件4 必须提交 材料名称",
                    f"采矿权{application or ''}申请 表中标记 要求 提交形式",
                )
                required_evidence_groups = default_evidence_groups("service_materials")
        elif base.intent == "exploration_to_mining_eligibility":
            dimensions = (
                "一般转采条件",
                "分矿种详查报告转采规定",
                "适用矿种和前置条件",
                "报告类型限制",
            )
            evidence_queries = (
                "探矿权转采矿权 经评审备案的矿产资源储量报告 详查（含）以上程度",
                "详查报告 可作为矿山设计开采依据 供矿山设计开采 可行性研究 工业价值",
                "不能替代探矿权转采矿权 地质勘查报告",
            )
            required_evidence_groups = ()
        elif base.intent == "technical_stage_requirement":
            stage_label = stage_requirement_label(base.normalized_query)
            clauses = stage_requirement_clauses(base.normalized_query)
            dimensions = (
                "资源量规模",
                "矿石加工选冶难易程度",
                "工艺矿物学研究程度",
                "矿石加工选冶试验或物化性能测试要求",
            )
            evidence_queries = (
                " ".join(
                    (
                        TECHNICAL_REQUIREMENT_STANDARD_NO,
                        stage_label,
                        *clauses,
                        "资源量规模 矿石加工选冶难易程度",
                    )
                ),
            )
            required_evidence_groups = ()
        elif "选冶" in question or "加工技术性能试验" in question:
            dimensions = (
                "勘查阶段对应的试验研究程度",
                "可选性、实验室流程、扩大连续、半工业和工业试验要求",
                "难选矿石和特殊矿石的加严条件",
                "例外和特别规定",
            )
            evidence_queries = (
                "矿石加工选冶技术性能试验研究程度 普查 详查 勘探 可选性试验 实验室流程试验",
                "扩大连续试验 半工业试验 工业试验 难选矿石 特殊矿石",
            )
            required_evidence_groups = (
                ("选冶", "加工技术性能试验", "加工选冶试验"),
                ("普查", "详查", "勘探", "试验研究程度", "可选性试验", "流程试验"),
            )
        elif "外推" in question:
            dimensions = ("外推类型", "所依据的工程间距", "尖推和平推比例", "适用条件和例外")
            evidence_queries = (
                "矿体有限外推 无限外推 工程间距 基本工程间距 实际工程间距 经验工程间距 "
                "DZ/T 0338.1-2020 6.2.2.1 DZ/T 0338.2-2020 5.4.2",
                "1/2尖推 1/4平推 2/3尖推 1/3平推",
            )
            required_evidence_groups = (
                ("外推", "尖推", "平推", "尖灭"),
                ("工程间距", "基本间距", "实际间距", "经验工程间距"),
                ("1/2", "1/4", "2/3", "1/3", "二分之一", "四分之一"),
            )
            focus = _projection_focus(question)
            if focus:
                focus_query, focus_group = focus
                evidence_queries = tuple(dict.fromkeys((*evidence_queries, focus_query)))[:3]
                required_evidence_groups = (*required_evidence_groups, focus_group)
        return ResearchPlan(
            canonical_question=base.normalized_query,
            intent=base.intent,
            strategy=(
                "document_inventory"
                if base.intent == "service_materials"
                else "relation_discovery"
                if base.intent == "exploration_to_mining_eligibility"
                else "requirements_matrix"
                if base.intent == "technical_stage_requirement"
                else "cross_document_comparison"
            ),
            anchor_titles=tuple(
                dict.fromkeys(
                    (
                        "采矿权变更（续期）登记临时服务指南"
                        if post_filing_steps
                        else "采矿权申请资料清单及要求",
                        *_explicit_titles(question),
                    )
                    if base.intent == "service_materials"
                    else _explicit_titles(question)
                )
            ),
            anchor_standard_numbers=tuple(
                dict.fromkeys(
                    (() if post_filing_steps else ("自然资规〔2023〕4号附件4", *base.standard_numbers))
                    if base.intent == "service_materials"
                    else (
                        *TRANSFER_ANCHOR_STANDARD_NUMBERS,
                        *base.standard_numbers,
                    )
                    if base.intent == "exploration_to_mining_eligibility"
                    else (TECHNICAL_REQUIREMENT_STANDARD_NO,)
                    if base.intent == "technical_stage_requirement"
                    else (
                        *PROJECTION_REFERENCE_STANDARD_NUMBERS,
                        *(number for number in base.standard_numbers if number in question),
                    )
                    if base.intent == "projection_comparison"
                    else tuple(number for number in base.standard_numbers if number in question)
                )
            ),
            corpus_title_terms=tuple(dict.fromkeys(title_terms)),
            corpus_standard_numbers=(
                ()
                if base.intent in {"service_materials", "exploration_to_mining_eligibility"}
                else base.standard_numbers if not title_terms else ()
            ),
            document_types=tuple(dict.fromkeys(document_types)),
            comparison_dimensions=tuple(dimensions),
            evidence_queries=evidence_queries,
            required_evidence_groups=required_evidence_groups,
            scope_note=(
                "按采矿权变更（续期）登记办事指南核对评审备案后至领证前的材料和手续。"
                if post_filing_steps
                else "按知识库目录中的受控文件范围逐份检索。"
            ),
            planner_used=False,
            query_classification=(
                base.classification.to_payload() if base.classification else None
            ),
        )


class ResearchAnalyzer:
    def __init__(self, settings: Settings, llm: LLMClient):
        self.settings = settings
        self.llm = llm

    async def analyze_batch(
        self,
        question: str,
        plan: ResearchPlan,
        indexed_sources: list[tuple[int, Source, str]],
        *,
        allow_split: bool = True,
    ) -> list[dict[str, Any]]:
        if not indexed_sources:
            return []
        if not self.llm.enabled:
            return self._fallback_facts(indexed_sources)
        evidence = [
            {
                "source_index": index,
                "document_id": document_id,
                "title": source.title,
                "standard_no": source.standard_no,
                "clause": source.chapter,
                "quote": source.quote,
            }
            for index, source, document_id in indexed_sources
        ]
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 geowiki 深度研究的证据事实抽取器。只能根据给定条款提取事实，"
                    "不能补充模型常识。每项事实必须引用 source_indices；没有直接证据时不要生成结论。"
                    "比较分类只能使用 consistent、stricter、looser、equivalent_wording、scope_differs、"
                    "special_provision、not_covered、insufficient_evidence、possible_conflict。"
                    "possible_conflict 只表示需要人工复核，不能直接断言法律冲突。"
                    "不能因为某个给定片段没有写到某项内容，就推断整份文件未规定或未提及；"
                    "not_covered 和 insufficient_evidence 由检索覆盖层判断，不在有直接引文的事实中使用。"
                    "每份文档最多提取3项事实，每项 finding 不超过180个汉字；严格区分有限外推和无限外推。"
                    "只返回 JSON。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question,
                        "anchor_titles": plan.anchor_titles,
                        "comparison_dimensions": plan.comparison_dimensions,
                        "evidence": evidence,
                        "output_schema": {
                            "facts": [
                                {
                                    "document_id": "文档ID",
                                    "classification": "consistent",
                                    "dimension": "比较维度",
                                    "finding": "由引文直接支持的具体差异或要求",
                                    "source_indices": [1],
                                }
                            ]
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        values = None
        for attempt in range(2):
            try:
                raw = await self.llm.complete_json(
                    messages,
                    max_tokens=self.settings.research_analysis_max_tokens,
                )
                payload = json.loads(raw)
                values = payload.get("facts") if isinstance(payload, dict) else None
                if isinstance(values, list):
                    break
            except Exception as error:
                if attempt == 1:
                    logger.warning("Research fact extraction failed: %s", type(error).__name__)
        if not isinstance(values, list):
            return await self._split_or_fallback(question, plan, indexed_sources, allow_split)

        valid_indices = {index for index, _, _ in indexed_sources}
        document_by_index = {index: document_id for index, _, document_id in indexed_sources}
        facts: list[dict[str, Any]] = []
        for value in values[:40]:
            if not isinstance(value, dict):
                continue
            classification = str(value.get("classification") or "").strip()
            if classification not in RESEARCH_CLASSIFICATIONS:
                continue
            if classification in {"not_covered", "insufficient_evidence"}:
                continue
            source_indices: list[int] = []
            for raw_index in value.get("source_indices") or []:
                try:
                    index = int(raw_index)
                except (TypeError, ValueError):
                    continue
                if index in valid_indices and index not in source_indices:
                    source_indices.append(index)
            cited_documents = {document_by_index[index] for index in source_indices}
            if not source_indices or len(cited_documents) != 1:
                continue
            document_id = next(iter(cited_documents))
            finding = _strip_out_of_scope_projection(
                _strip_unsupported_absence(
                    normalize_user_query(str(value.get("finding") or ""))[:700]
                ),
                question,
            )
            if not finding:
                continue
            if not (plan.anchor_titles or plan.anchor_standard_numbers):
                if classification == "consistent":
                    classification = "equivalent_wording"
                elif classification in {"stricter", "looser"}:
                    classification = "special_provision"
            facts.append(
                {
                    "document_id": document_id,
                    "classification": classification,
                    "dimension": normalize_user_query(str(value.get("dimension") or ""))[:160],
                    "finding": finding,
                    "source_indices": list(dict.fromkeys(source_indices))[:5],
                }
            )
        if facts:
            return facts
        return await self._split_or_fallback(question, plan, indexed_sources, allow_split)

    async def _split_or_fallback(
        self,
        question: str,
        plan: ResearchPlan,
        indexed_sources: list[tuple[int, Source, str]],
        allow_split: bool,
    ) -> list[dict[str, Any]]:
        if allow_split and len(indexed_sources) > 1:
            midpoint = len(indexed_sources) // 2
            left = await self.analyze_batch(
                question,
                plan,
                indexed_sources[:midpoint],
                allow_split=False,
            )
            right = await self.analyze_batch(
                question,
                plan,
                indexed_sources[midpoint:],
                allow_split=False,
            )
            return [*left, *right]
        return self._fallback_facts(indexed_sources)

    @staticmethod
    def _fallback_facts(indexed_sources: list[tuple[int, Source, str]]) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        for index, source, document_id in indexed_sources:
            quote = normalize_user_query(source.quote or "")[:500]
            if not quote:
                continue
            if any(
                term in quote
                for term in (
                    "无限外推",
                    "见矿工程向外再没有工程控制",
                    "见矿工程外无控制工程",
                    "边缘见矿工程外",
                )
            ):
                dimension = "无限外推规则"
            elif any(term in quote for term in ("有限外推", "相邻工程一个见矿", "相邻的两个工程一个见矿")):
                dimension = "有限外推规则"
            else:
                dimension = "直接条款"
            facts.append(
                {
                    "document_id": document_id,
                    "classification": "special_provision",
                    "dimension": dimension,
                    "finding": quote,
                    "source_indices": [index],
                }
            )
        return facts


def _source_from_hit(hit: dict[str, Any]) -> Source:
    return Source(
        title=hit.get("title") or "未知文件",
        standard_no=hit.get("standard_no"),
        chapter=hit.get("clause_no") or hit.get("section_path"),
        page=hit.get("page") or hit.get("page_start"),
        quote=hit.get("quote") or hit.get("evidence_text") or hit.get("text"),
        score=hit.get("score"),
        source_type=hit.get("source_type", "unavailable"),
        text_access=hit.get("text_access", "unavailable"),
        url=hit.get("url") or hit.get("source_url"),
        source_platform=hit.get("source_platform"),
        source_role=hit.get("source_role"),
        validation_status=hit.get("validation_status"),
    )


def _markdown_cell(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().replace("|", "\\|")


UNSUPPORTED_ABSENCE_PATTERN = re.compile(
    r"未提及|未规定|未说明|没有规定|没有提及|不包含|未采用|未给出"
)


def _strip_unsupported_absence(value: str) -> str:
    parts = re.split(r"([，,；;。])", value)
    kept: list[str] = []
    for index in range(0, len(parts), 2):
        clause = parts[index].strip()
        separator = parts[index + 1] if index + 1 < len(parts) else ""
        if not clause or UNSUPPORTED_ABSENCE_PATTERN.search(clause):
            continue
        clause = re.sub(r"^(?:仅|只)(?=规定|允许|采用|平推|尖推)", "", clause)
        kept.append(clause + separator)
    return "".join(kept).strip("，,；;。 ")


def _strip_out_of_scope_projection(value: str, question: str) -> str:
    opposite = None
    if "无限外推" in question:
        opposite = "有限外推"
    elif "有限外推" in question:
        opposite = "无限外推"
    if not opposite or opposite not in value:
        return value

    parts = re.split(r"([，,；;。])", value)
    kept: list[str] = []
    for index in range(0, len(parts), 2):
        clause = parts[index].strip()
        separator = parts[index + 1] if index + 1 < len(parts) else ""
        if not clause:
            continue
        if opposite in clause:
            clause = clause.split(opposite, 1)[0].rstrip("但而与和及、 ")
        if clause:
            kept.append(clause + separator)
    return "".join(kept).strip("，,；;。 ")


class ResearchTaskRunner:
    def __init__(self) -> None:
        self._running: dict[str, asyncio.Task[None]] = {}
        self._global_semaphore: asyncio.Semaphore | None = None
        self._usage = UsageLogger()

    def schedule(self, task_id: str) -> None:
        current = self._running.get(task_id)
        if current and not current.done():
            return
        task = asyncio.create_task(self._run_guarded(task_id), name=f"research:{task_id}")
        self._running[task_id] = task
        task.add_done_callback(lambda _: self._running.pop(task_id, None))

    async def _run_guarded(self, task_id: str) -> None:
        settings = get_settings()
        if self._global_semaphore is None:
            self._global_semaphore = asyncio.Semaphore(settings.research_global_concurrency)
        async with self._global_semaphore:
            await self._run(task_id, settings)

    async def _run(self, task_id: str, settings: Settings) -> None:
        store = get_account_store(settings)
        llm = LLMClient(settings)
        knowledge = KnowledgeClient(settings)
        try:
            task = store.get_research_task_internal(task_id)
            if task["status"] == "cancelled":
                return
            if not knowledge.enabled:
                raise RuntimeError("knowledge base is not configured")

            store.update_research_task(
                task_id,
                status="planning",
                percent=5,
                message="正在识别基准文件、候选范围和比较维度。",
            )
            saved_query_plan = query_plan_from_payload(
                task["retrieval_question"],
                task.get("query_plan") if isinstance(task.get("query_plan"), dict) else None,
            )
            plan = await ResearchPlanner(settings, llm).plan(
                task["retrieval_question"],
                saved_query_plan,
            )
            store.update_research_task(
                task_id,
                status="retrieving",
                percent=15,
                message="正在从知识库目录枚举候选文件。",
                plan=plan.to_payload(),
            )
            corpus = await knowledge.research_corpus(
                {
                    "title_terms": list(dict.fromkeys((*plan.corpus_title_terms, *plan.anchor_titles))),
                    "standard_numbers": list(
                        dict.fromkeys((*plan.corpus_standard_numbers, *plan.anchor_standard_numbers))
                    ),
                    "document_types": list(plan.document_types),
                    "limit": settings.research_max_documents,
                }
            )
            documents = list(corpus.get("items") or [])
            documents = self._prioritize_documents(documents, plan)
            total_documents = int(corpus.get("total") or len(documents))
            candidate_truncated = bool(corpus.get("truncated"))
            snapshot = corpus.get("knowledge_snapshot")
            store.update_research_task(
                task_id,
                status="retrieving",
                percent=20,
                message=f"已枚举 {total_documents} 份候选文件，开始逐份检索。",
                total_documents=total_documents,
                examined_documents=0,
                evidence_documents=0,
            )
            if not documents:
                await self._finish_insufficient(
                    store,
                    task,
                    plan,
                    snapshot,
                    total_documents,
                    candidate_truncated,
                    "知识库目录未枚举到符合研究范围的可问答文件。",
                    settings,
                )
                return

            sources_by_document, failed_documents = await self._retrieve_documents(
                store,
                task_id,
                task,
                plan,
                documents,
                total_documents,
                knowledge,
                settings,
            )
            sources: list[Source] = []
            source_documents: list[str] = []
            seen_sources: set[tuple[str, str, str]] = set()
            for document in documents:
                document_id = str(document.get("document_id") or "")
                for source in sources_by_document.get(document_id, []):
                    key = (document_id, source.chapter or "", source.quote or "")
                    if key in seen_sources:
                        continue
                    seen_sources.add(key)
                    sources.append(source)
                    source_documents.append(document_id)
                    if len(sources) >= 30:
                        break
                if len(sources) >= 30:
                    break
            if not sources:
                await self._finish_insufficient(
                    store,
                    task,
                    plan,
                    snapshot,
                    total_documents,
                    candidate_truncated,
                    "已逐份检索候选文件，但没有命中可用于比较的直接条款。",
                    settings,
                    examined_documents=len(documents),
                )
                return

            store.update_research_task(
                task_id,
                status="analyzing",
                percent=80,
                message="正在把直接条款转换为结构化事实并比较差异。",
                total_documents=total_documents,
                examined_documents=len(documents),
                evidence_documents=len(sources_by_document),
            )
            indexed_sources = [
                (index, source, source_documents[index - 1])
                for index, source in enumerate(sources, start=1)
            ]
            analyzer = ResearchAnalyzer(settings, llm)
            facts: list[dict[str, Any]] = []
            if plan.intent == "service_materials":
                facts = self._service_material_facts(indexed_sources)
            elif plan.intent == "exploration_to_mining_eligibility":
                facts = self._transfer_facts(indexed_sources)
            elif plan.intent == "projection_comparison":
                facts = self._projection_facts(indexed_sources, task["retrieval_question"])
            elif plan.intent == "technical_stage_requirement":
                facts = self._technical_stage_requirement_facts(indexed_sources)
            else:
                batch_size = settings.research_analysis_batch_size
                for start in range(0, len(indexed_sources), batch_size):
                    facts.extend(
                        await analyzer.analyze_batch(
                            task["retrieval_question"],
                            plan,
                            indexed_sources[start : start + batch_size],
                        )
                    )
            facts, sources = self._compact_fact_sources(facts, sources)
            store.update_research_task(
                task_id,
                status="analyzing",
                percent=92,
                message="正在生成研究结论、对比矩阵和覆盖说明。",
                total_documents=total_documents,
                examined_documents=len(documents),
                evidence_documents=len(sources_by_document),
            )

            answer = await self._render_answer(task["retrieval_question"], plan, facts, sources, llm, settings)
            no_evidence_documents = max(0, len(documents) - len(sources_by_document))
            notes: list[str] = []
            if candidate_truncated:
                notes.append(
                    f"候选目录共 {total_documents} 份，本次按服务器研究上限审查前 {len(documents)} 份。"
                )
            if no_evidence_documents:
                notes.append(f"{no_evidence_documents} 份候选文件未命中可比较的直接条款。")
            if failed_documents:
                notes.append(f"{failed_documents} 份候选文件检索失败。")
            final_status, missing_comparison_coverage = self._research_final_status(
                plan,
                facts,
                candidate_truncated=candidate_truncated,
                failed_documents=failed_documents,
            )
            if missing_comparison_coverage:
                notes.append("可比直接条款不足两份文件，无法形成跨文件差异结论。")
            quota = store.settle_qa_quota(
                task["request_id"],
                "answered",
                len(answer),
                settings.quota_timezone,
            )
            result = ResearchResult(
                task_id=task_id,
                request_id=task["request_id"],
                question=task["question"],
                session_id=task["conversation_id"],
                answer=answer,
                status=final_status,
                quota_cost=int(task["quota_cost"]),
                reserved_quota_units=int(task["reserved_quota_units"]),
                sources=sources[:30],
                limitations=Limitations(
                    has_clause_level_evidence=True,
                    notes=notes,
                ),
                coverage=ResearchCoverage(
                    examined_documents=len(documents),
                    total_documents=total_documents,
                    evidence_documents=len(sources_by_document),
                    candidate_truncated=candidate_truncated,
                    knowledge_snapshot=snapshot,
                    notes=notes,
                ),
                confidence=(
                    "high"
                    if final_status == "completed"
                    else "low"
                    if final_status == "insufficient_evidence"
                    else "medium"
                ),
                quota=QuotaInfo(**quota),
                query_classification=plan.query_classification,
            )
            store.complete_research_task(task_id, final_status, result.model_dump(mode="json"))
            self._save_exchange(store, task, result)
            self._write_usage(task, result)
        except Exception as error:
            logger.exception("Deep research task %s failed", task_id)
            try:
                task = store.get_research_task_internal(task_id)
                store.fail_qa_quota(task["request_id"], settings.quota_timezone)
                store.complete_research_task(
                    task_id,
                    "failed",
                    None,
                    error_code=type(error).__name__,
                )
            except Exception:
                logger.exception("Unable to settle failed deep research task %s", task_id)
        finally:
            await knowledge.aclose()
            await llm.aclose()

    @staticmethod
    def _research_final_status(
        plan: ResearchPlan,
        facts: list[dict[str, Any]],
        *,
        candidate_truncated: bool,
        failed_documents: int,
    ) -> tuple[str, bool]:
        comparison_documents = {
            str(fact.get("document_id") or "")
            for fact in facts
            if fact.get("document_id")
            and fact.get("classification") not in {"insufficient_evidence", "not_covered"}
        }
        missing_comparison_coverage = bool(
            plan.strategy == "cross_document_comparison"
            and len(comparison_documents) < 2
        )
        if missing_comparison_coverage:
            return "insufficient_evidence", True
        if candidate_truncated or failed_documents:
            return "partial", False
        return "completed", False

    async def _retrieve_documents(
        self,
        store,
        task_id: str,
        task: dict[str, Any],
        plan: ResearchPlan,
        documents: list[dict[str, Any]],
        total_documents: int,
        knowledge: KnowledgeClient,
        settings: Settings,
    ) -> tuple[dict[str, list[Source]], int]:
        semaphore = asyncio.Semaphore(settings.research_document_concurrency)
        progress_lock = asyncio.Lock()
        examined = 0
        evidence_documents = 0
        failed_documents = 0
        results: dict[str, list[Source]] = {}
        combined_query = " ".join(
            dict.fromkeys(
                (
                    plan.canonical_question,
                    *plan.evidence_queries,
                    *plan.comparison_dimensions,
                    *(term for group in plan.required_evidence_groups for term in group),
                )
            )
        )[:1500]

        async def retrieve(document: dict[str, Any]) -> None:
            nonlocal examined, evidence_documents, failed_documents
            document_id = str(document.get("document_id") or "")
            async with semaphore:
                base = understand_query(combined_query)
                scoped_plan: QueryPlan = replace(
                    base,
                    original_query=task["retrieval_question"],
                    normalized_query=plan.canonical_question,
                    retrieval_query=combined_query,
                    intent=(
                        plan.intent
                        if plan.intent in {
                            "service_materials",
                            "exploration_to_mining_eligibility",
                            "technical_stage_requirement",
                        }
                        else "cross_document_audit"
                    ),
                    candidate_title_terms=(),
                    standard_numbers=(),
                    document_types=(str(document.get("document_type") or "standard"),),
                    required_evidence_groups=plan.required_evidence_groups,
                    search_mode="scoped",
                    comparison_dimensions=plan.comparison_dimensions,
                    scope_origin="none",
                    planner_used=plan.planner_used,
                    exhaustive_search=False,
                )
                filters = dict(task.get("filters") or {})
                filters["document_id"] = document_id
                try:
                    top_k = (
                        30
                        if plan.intent == "service_materials"
                        else 20
                        if plan.intent == "technical_stage_requirement"
                        else 6
                    )
                    response = await knowledge.search(
                        combined_query,
                        filters,
                        scoped_plan,
                        top_k=top_k,
                        allow_web_supplement=False,
                    )
                    per_document_limit = (
                        24
                        if plan.intent == "service_materials"
                        else 1
                        if plan.intent in {
                            "exploration_to_mining_eligibility",
                            "projection_comparison",
                        }
                        else 8
                        if plan.intent == "technical_stage_requirement"
                        else 2
                    )
                    sources = [
                        _source_from_hit(hit)
                        for hit in response.results
                        if (hit.get("quote") or hit.get("evidence_text"))
                        and (hit.get("clause_no") or hit.get("section_path"))
                        and self._hit_matches_research_plan(hit, plan)
                        and not self._hit_is_normative_reference_list(
                            hit,
                            task["retrieval_question"],
                        )
                    ][:per_document_limit]
                    if sources:
                        results[document_id] = sources
                except Exception:
                    failed_documents += 1
                    logger.exception("Research retrieval failed for document %s", document_id)
                async with progress_lock:
                    examined += 1
                    if document_id in results:
                        evidence_documents += 1
                    percent = 20 + int(55 * examined / max(1, len(documents)))
                    store.update_research_task(
                        task_id,
                        status="retrieving",
                        percent=percent,
                        message=f"正在逐份检索：已审查 {examined}/{len(documents)} 份。",
                        total_documents=total_documents,
                        examined_documents=examined,
                        evidence_documents=evidence_documents,
                    )

        await asyncio.gather(*(retrieve(document) for document in documents))
        return results, failed_documents

    @staticmethod
    def _prioritize_documents(
        documents: list[dict[str, Any]],
        plan: ResearchPlan,
    ) -> list[dict[str, Any]]:
        anchors = {
            re.sub(r"\s+", "", number).upper()
            for number in plan.anchor_standard_numbers
            if number
        }
        if not anchors:
            return documents
        return sorted(
            documents,
            key=lambda document: (
                re.sub(r"\s+", "", str(document.get("standard_no") or "")).upper()
                not in anchors,
            ),
        )

    @staticmethod
    def _transfer_facts(
        indexed_sources: list[tuple[int, Source, str]],
    ) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        for index, source, document_id in indexed_sources:
            quote = normalize_user_query(source.quote or "")[:700]
            if not quote:
                continue
            if "不能替代探矿权转采矿权" in quote:
                dimension = "报告类型限制"
            elif any(term in quote for term in TRANSFER_EQUIVALENT_TERMS):
                dimension = "分矿种详查报告转采规定"
            else:
                dimension = "一般转采条件"
            facts.append(
                {
                    "document_id": document_id,
                    "classification": "special_provision",
                    "dimension": dimension,
                    "finding": quote,
                    "source_indices": [index],
                }
            )
        return facts

    @staticmethod
    def _service_material_facts(
        indexed_sources: list[tuple[int, Source, str]],
    ) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        for index, source, document_id in indexed_sources:
            quote = normalize_user_query(source.quote or "")[:1200]
            if not quote:
                continue
            facts.append(
                {
                    "document_id": document_id,
                    "classification": "special_provision",
                    "dimension": "申请材料与适用条件",
                    "finding": quote,
                    "source_indices": [index],
                }
            )
        return facts

    @staticmethod
    def _technical_stage_requirement_facts(
        indexed_sources: list[tuple[int, Source, str]],
    ) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        for index, source, document_id in indexed_sources:
            if re.sub(r"\s+", "", source.standard_no or "").upper() != (
                TECHNICAL_REQUIREMENT_STANDARD_NO.replace(" ", "").upper()
            ):
                continue
            if not re.fullmatch(r"6\.[3-5]\.[1-4]", source.chapter or ""):
                continue
            facts.append(
                {
                    "document_id": document_id,
                    "classification": "special_provision",
                    "dimension": "条件化试验研究要求",
                    "finding": normalize_user_query(source.quote or "")[:900],
                    "source_indices": [index],
                }
            )
        return facts

    @classmethod
    def _projection_facts(
        cls,
        indexed_sources: list[tuple[int, Source, str]],
        question: str,
    ) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str, str, str]] = set()
        finite_focus = "有限外推" in question
        infinite_focus = "无限外推" in question
        for index, source, document_id in indexed_sources:
            quote = normalize_user_query(source.quote or "")[:1200]
            if not quote:
                continue
            for segment in cls._projection_segments(quote):
                fact = cls._projection_fact(segment)
                if fact is None:
                    continue
                projection_type = str(fact["projection_type"])
                if finite_focus and projection_type != "有限外推":
                    continue
                evidence_role = "primary"
                if infinite_focus and projection_type != "无限外推":
                    normalized_standard = re.sub(r"\s+", "", source.standard_no or "").upper()
                    if normalized_standard != "DZ/T0338.2-2020":
                        continue
                    evidence_role = "finite_contrast"
                key = (
                    document_id,
                    projection_type,
                    str(fact["trigger_condition"]),
                    str(fact["distance_basis"]),
                    str(fact["pointed_ratio"]),
                    str(fact["flat_ratio"]),
                )
                if key in seen:
                    continue
                seen.add(key)
                facts.append(
                    {
                        "document_id": document_id,
                        "classification": "special_provision",
                        "dimension": f"{projection_type}规则",
                        "evidence_role": evidence_role,
                        **fact,
                        "source_indices": [index],
                    }
                )
        return facts

    @staticmethod
    def _projection_segments(quote: str) -> list[str]:
        clean = re.sub(r"\s+", " ", quote).strip()
        sentences = [
            value.strip()
            for value in re.split(r"(?<=[。；;])\s*", clean)
            if value.strip()
        ]
        markers = (
            "有限外推",
            "无限外推",
            "外推",
            "尖推",
            "平推",
            "尖灭",
            "一个见矿",
            "未见矿",
            "不见矿",
            "边缘见矿工程",
        )
        segments: list[str] = []
        for position, sentence in enumerate(sentences):
            if not any(marker in sentence for marker in markers):
                continue
            context = sentence
            for offset in (1, 2):
                if position + offset >= len(sentences):
                    break
                following = sentences[position + offset]
                if not any(
                    term in following
                    for term in (
                        "工程间距",
                        "尖推",
                        "平推",
                        "尖灭",
                        "部分见矿",
                        "实际工程间距",
                        "推断资源量工程间距",
                    )
                ):
                    break
                context = f"{context}{following}"
            if not any(term in context for term in ("有限外推", "无限外推", "一个见矿", "边缘见矿工程")) and position:
                previous = sentences[position - 1]
                if any(term in previous for term in ("有限外推", "无限外推", "一个见矿", "边缘见矿工程")):
                    context = f"{previous}{context}"
            if "部分见矿时" in context:
                before, _, after = context.partition("部分见矿时")
                split_parts = [before.strip(), f"相邻工程部分见矿时{after}".strip()]
            elif "有限外推" in context and "无限外推" in context:
                split_parts = [
                    part.strip()
                    for part in re.split(r"(?=(?:有限外推|无限外推))", context)
                    if part.strip()
                ]
            else:
                split_parts = [context]
            segments.extend(split_parts or [context])
        if not segments and any(marker in clean for marker in markers):
            segments.append(clean)
        return list(dict.fromkeys(segment[:520] for segment in segments))

    @staticmethod
    def _projection_fact(segment: str) -> dict[str, Any] | None:
        compact = re.sub(r"\s+", "", segment)
        has_infinite = any(
            term in compact
            for term in (
                "无限外推",
                "见矿工程向外再没有工程控制",
                "见矿工程向外无工程控制",
                "见矿工程外无控制工程",
                "边缘见矿工程外",
                "边缘见矿工程向外",
            )
        )
        has_finite = any(
            term in compact
            for term in (
                "有限外推",
                "相邻的两个工程一个见矿",
                "相邻工程一个见矿",
                "一个见矿另一个不见矿",
                "一个见矿一个未见矿",
                "相邻工程部分见矿",
            )
        )
        if not has_infinite and not has_finite:
            return None
        projection_type = "无限外推" if has_infinite and not has_finite else "有限外推"

        trigger = ""
        trigger_patterns = (
            r"(相邻的两个工程一个见矿[，,]?另一个不见矿时)",
            r"(相邻工程一个见矿[，,]?另一个(?:不见矿|未见矿)时)",
            r"(相邻工程中一个工程(?:见矿|部分见矿)[，,]?另一个工程(?:不见矿|未见矿)时)",
            r"(相邻工程部分见矿时)",
            r"(见矿工程向外再没有工程控制时)",
            r"(见矿工程向外无工程控制时)",
            r"(边缘见矿工程向外(?:无|没有)工程控制时)",
        )
        for pattern in trigger_patterns:
            match = re.search(pattern, segment)
            if match:
                trigger = match.group(1)
                break
        if not trigger:
            trigger = "相邻工程见矿情况" if projection_type == "有限外推" else "边缘见矿工程外无工程控制"

        relationship = ""
        if re.search(r"实际工程间距大于推断资源量工程间距", compact):
            relationship = "实际工程间距大于推断资源量工程间距时改用推断资源量工程间距"
        elif re.search(r"实际工程间距(?:小于|不大于|≤)推断资源量工程间距", compact):
            relationship = "实际工程间距不大于推断资源量工程间距时采用实际工程间距"

        bases: list[str] = []
        for term in (
            "推断资源量工程间距",
            "经验工程间距",
            "基本勘查工程间距",
            "基本工程间距",
            "实际工程间距",
            "相应工程间距",
            "工程间距",
        ):
            if term in compact and not any(term in current or current in term for current in bases):
                bases.append(term)
        if relationship:
            distance_basis = relationship
        elif bases:
            distance_basis = "、".join(bases[:3])
        else:
            distance_basis = "条款未明确命名距离基准"

        def ratios(action: str) -> str:
            values: list[str] = []
            for action_match in re.finditer(rf"(?:{action})", compact):
                prefix = compact[max(0, action_match.start() - 14) : action_match.start()]
                matches = list(
                    re.finditer(r"1/2|1/4|2/3|1/3|二分之一|四分之一", prefix)
                )
                if matches:
                    values.append(matches[-1].group(0))
            normalized = [
                {"二分之一": "1/2", "四分之一": "1/4"}.get(value, value)
                for value in values
            ]
            return "、".join(dict.fromkeys(normalized))

        pointed_ratio = ratios("尖推|尖灭")
        flat_ratio = ratios("平推")
        if (
            not pointed_ratio
            and not flat_ratio
            and distance_basis == "条款未明确命名距离基准"
        ):
            return None
        adjacent_condition = ""
        if "部分见矿" in compact:
            adjacent_condition = "相邻工程部分见矿"
        elif any(term in compact for term in ("一个见矿另一个不见矿", "一个见矿一个未见矿", "一个见矿，另一个不见矿")):
            adjacent_condition = "相邻工程一个见矿、另一个未见矿"

        finding_parts = [trigger, f"距离基准：{distance_basis}"]
        if pointed_ratio:
            finding_parts.append(f"尖推/尖灭：{pointed_ratio}")
        if flat_ratio:
            finding_parts.append(f"平推：{flat_ratio}")
        if adjacent_condition:
            finding_parts.append(adjacent_condition)
        return {
            "projection_type": projection_type,
            "trigger_condition": trigger,
            "distance_basis": distance_basis,
            "distance_relationship": relationship or None,
            "adjacent_engineering_condition": adjacent_condition or None,
            "pointed_ratio": pointed_ratio or None,
            "flat_ratio": flat_ratio or None,
            "exceptions": None,
            "finding": "；".join(finding_parts),
            "source_clause": segment[:420].strip(),
        }

    @staticmethod
    def _compact_fact_sources(
        facts: list[dict[str, Any]],
        sources: list[Source],
    ) -> tuple[list[dict[str, Any]], list[Source]]:
        used_indices: list[int] = []
        for fact in facts:
            for index in fact.get("source_indices", []):
                if isinstance(index, int) and 1 <= index <= len(sources) and index not in used_indices:
                    used_indices.append(index)
        used_indices = used_indices[:24]
        remap = {old: new for new, old in enumerate(used_indices, start=1)}
        compact_facts: list[dict[str, Any]] = []
        for fact in facts:
            mapped = [remap[index] for index in fact.get("source_indices", []) if index in remap]
            if not mapped:
                continue
            compact = dict(fact)
            compact["source_indices"] = mapped
            compact_facts.append(compact)
        compact_sources = [sources[index - 1] for index in used_indices]
        return compact_facts, compact_sources

    @staticmethod
    def _hit_matches_evidence_groups(
        hit: dict[str, Any],
        groups: tuple[tuple[str, ...], ...],
    ) -> bool:
        if not groups:
            return True
        context = " ".join(
            str(hit.get(key) or "")
            for key in ("title", "standard_no", "clause_no", "section_path", "quote", "evidence_text")
        )
        return all(any(term and term in context for term in group) for group in groups)

    @classmethod
    def _hit_matches_research_plan(
        cls,
        hit: dict[str, Any],
        plan: ResearchPlan,
    ) -> bool:
        if plan.intent == "projection_comparison":
            standard_no = re.sub(r"\s+", "", str(hit.get("standard_no") or "")).upper()
            clause = str(hit.get("clause_no") or hit.get("section_path") or "")
            if standard_no == "DZ/T0338.2-2020" and clause == "5.4.2":
                focus_markers = {
                    "无限外推",
                    "有限外推",
                    "见矿工程向外再没有工程控制",
                    "见矿工程向外无工程控制",
                    "见矿工程外无控制工程",
                    "边缘见矿工程外",
                    "边缘见矿工程向外",
                    "相邻工程一个见矿",
                    "相邻的两个工程一个见矿",
                    "相邻工程未见矿",
                }
                base_groups = tuple(
                    group
                    for group in plan.required_evidence_groups
                    if not set(group).issubset(focus_markers)
                )
                return cls._hit_matches_evidence_groups(hit, base_groups)
        if plan.intent != "exploration_to_mining_eligibility":
            return cls._hit_matches_evidence_groups(hit, plan.required_evidence_groups)
        context = " ".join(
            str(hit.get(key) or "")
            for key in ("title", "standard_no", "clause_no", "section_path", "quote", "evidence_text")
        )
        return bool(cls._transfer_direct_quote(context))

    @staticmethod
    def _hit_is_normative_reference_list(hit: dict[str, Any], question: str) -> bool:
        if any(
            term in question
            for term in (
                "规范性引用文件",
                "引用了哪些",
                "引用哪些",
                "哪些规范引用",
                "哪些标准引用",
                "哪些文件引用",
                "引用了",
                "引用标准",
                "被引用",
                "引用关系",
                "引用情况",
                "参考标准",
            )
        ):
            return False
        chapter = re.sub(
            r"\s+",
            "",
            str(hit.get("clause_no") or hit.get("section_path") or ""),
        )
        quote = re.sub(
            r"\s+",
            " ",
            str(hit.get("quote") or hit.get("evidence_text") or hit.get("text") or ""),
        ).strip()
        reference_count = len(
            re.findall(
                r"\b(?:GB(?:/T)?|DZ/T|DZ|HJ|NB/T|MT/T|YS/T|JB/T|AQ|TD/T)\s*\d{2,}",
                quote,
                flags=re.IGNORECASE,
            )
        )
        if "规范性引用文件" in chapter:
            return True
        if chapter in {"2", "2.0", "第2章"} and reference_count >= 2:
            return True
        page = hit.get("page") or hit.get("page_start")
        try:
            early_page = int(page) <= 4
        except (TypeError, ValueError):
            early_page = bool(re.fullmatch(r"第?[1-4]页", chapter))
        return early_page and reference_count >= 3

    async def _render_answer(
        self,
        question: str,
        plan: ResearchPlan,
        facts: list[dict[str, Any]],
        sources: list[Source],
        llm: LLMClient,
        settings: Settings,
    ) -> str:
        if plan.intent == "service_materials":
            return self._render_service_material_answer(plan, sources)
        if plan.intent == "exploration_to_mining_eligibility":
            return self._render_transfer_answer(sources)
        if plan.intent == "projection_comparison":
            return self._render_projection_comparison(question, facts, sources)
        if plan.intent == "technical_stage_requirement":
            return self._render_technical_stage_requirement_answer(question, sources)
        summary = "本次研究已按知识库候选范围逐份检索，并仅使用命中的直接条款形成下列比较结果。"
        document_count = len({fact.get("document_id") for fact in facts if fact.get("document_id")})
        if document_count:
            summary = (
                f"本次研究在 {document_count} 份文件中形成了可回溯到直接条款的结构化事实。"
                "下表按当前问题的比较维度列出直接依据和适用条件。"
            )
        if plan.intent == "projection_comparison" and facts:
            has_finite = any(fact.get("dimension") == "有限外推规则" for fact in facts)
            has_infinite = any(fact.get("dimension") == "无限外推规则" for fact in facts)
            if "无限外推" in question and has_finite:
                summary = (
                    f"本次在 {document_count} 份文件中形成直接证据。下表以无限外推规定为主，"
                    "并单列有限外推条款作为距离基准对照；有限外推证据不作为无限外推规定使用。"
                )
            elif has_finite and has_infinite:
                summary = (
                    f"本次在 {document_count} 份文件中形成直接证据，并分别标注有限外推与无限外推。"
                    "比较时必须同时核对外推类型、距离基准、比例和适用条件。"
                )
        if llm.enabled and facts and plan.intent != "projection_comparison":
            try:
                source_by_index = {index: source for index, source in enumerate(sources, start=1)}
                summary_facts = []
                for fact in facts:
                    indices = [
                        index
                        for index in fact.get("source_indices", [])
                        if index in source_by_index
                    ]
                    source = source_by_index[indices[0]] if indices else None
                    summary_facts.append(
                        {
                            **fact,
                            "document_id": None,
                            "document_label": (
                                f"{source.standard_no or ''}《{source.title}》"
                                if source
                                else "未知文件"
                            ),
                        }
                    )
                completion = await llm.complete_detailed(
                    [
                        {
                            "role": "system",
                            "content": (
                                "你是 geowiki 深度研究摘要器。只概括给定结构化事实，不能增加新标准、"
                                "新数值或模型常识。用2至4句中文说明主要一致点、差异和不确定性。"
                                "必须保持用户问题限定的外推类型，严禁把有限外推当作无限外推，或反向替换。"
                                "只有至少两份文件在同一比较维度上有直接事实时，才能概括为一致。"
                                "\n"
                                f"{prompt_text(settings, 'research_summary', primary_intent=(plan.query_classification or {}).get('primary_intent'))}"
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "question": question,
                                    "comparison_dimensions": plan.comparison_dimensions,
                                    "facts": summary_facts,
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ],
                    max_tokens=min(700, settings.research_answer_max_tokens),
                    temperature=settings.research_answer_temperature,
                )
                if completion.content and self._summary_matches_scope(question, completion.content):
                    summary = completion.content
            except Exception:
                pass

        source_by_index = {index: source for index, source in enumerate(sources, start=1)}
        document_meta: dict[str, Source] = {}
        for index, source in source_by_index.items():
            document_id = next(
                (
                    fact["document_id"]
                    for fact in facts
                    if index in fact.get("source_indices", [])
                ),
                f"source-{index}",
            )
            document_meta.setdefault(document_id, source)

        lines = ["**研究结论**", "", summary.strip(), "", "**对比结果**", ""]
        lines.extend(
            [
                "| 文件 | 判定 | 比较维度 | 具体发现 | 依据条款 |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for fact in facts[:40]:
            indices = [index for index in fact.get("source_indices", []) if index in source_by_index]
            source = source_by_index[indices[0]] if indices else document_meta.get(fact["document_id"])
            file_label = (
                f"{source.standard_no or ''}《{source.title}》" if source else fact["document_id"]
            )
            clauses = "、".join(
                dict.fromkeys(source_by_index[index].chapter or "相关条款" for index in indices)
            )
            lines.append(
                "| "
                + " | ".join(
                    [
                        _markdown_cell(file_label),
                        _markdown_cell(CLASSIFICATION_LABELS.get(fact["classification"], fact["classification"])),
                        _markdown_cell(fact.get("dimension") or "未标注"),
                        _markdown_cell(fact.get("finding")),
                        _markdown_cell(clauses or "相关条款"),
                    ]
                )
                + " |"
            )

        return "\n".join(lines).strip()

    @staticmethod
    def _stage_requirement_quote(text: str, clause: str) -> str:
        compact = re.sub(r"\s+", " ", text or "").strip()
        compact = re.sub(r"-\s*\d+\s*[一二三四五六七八九十]+", "", compact)
        compact = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", compact)
        section = clause.rsplit(".", 1)[0]
        next_section = str(int(section.split(".")[0]) + 1)
        match = re.search(
            rf"({re.escape(clause)}\s+.*?)(?=\s+{re.escape(section)}\.[0-9]+\s|\s+{re.escape(next_section)}\.\d|$)",
            compact,
        )
        return match.group(1).strip() if match else compact

    @staticmethod
    def _split_stage_requirement_quote(quote: str, clause: str) -> tuple[str, str]:
        text = re.sub(r"\s+", " ", quote or "").strip()
        text = re.sub(rf"^{re.escape(clause)}\s*", "", text)
        if "，在" in text:
            condition, requirement = text.split("，在", 1)
            return condition.strip(), f"在{requirement.strip()}"
        return text or "相关条件", "见该条款原文"

    @classmethod
    def _render_technical_stage_requirement_answer(
        cls,
        question: str,
        sources: list[Source],
    ) -> str:
        expected_clauses = stage_requirement_clauses(question)
        by_clause = {
            source.chapter: source
            for source in sources
            if re.sub(r"\s+", "", source.standard_no or "").upper()
            == TECHNICAL_REQUIREMENT_STANDARD_NO.replace(" ", "").upper()
        }
        if not expected_clauses or not all(clause in by_clause for clause in expected_clauses):
            return (
                "**研究结论**\n\n"
                "未取得该阶段完整的条件矩阵条款，不能仅凭单条或章节标题概括技术要求。"
            )
        lines = [
            "**研究结论**",
            "",
            f"{stage_requirement_label(question)}矿石加工选冶技术性能要求取决于资源量规模和矿石加工选冶难易程度，"
            "不能仅按矿种给出一个统一试验等级。",
            "",
            f"依据 **{TECHNICAL_REQUIREMENT_STANDARD_NO}《{TECHNICAL_REQUIREMENT_STANDARD_TITLE}》**，"
            "完整条件矩阵如下：",
            "",
            "| 资源量规模与矿石类型 | 试验研究要求 | 依据条款 |",
            "| --- | --- | --- |",
        ]
        for clause in expected_clauses:
            quote = cls._stage_requirement_quote(by_clause[clause].quote or "", clause)
            condition, requirement = cls._split_stage_requirement_quote(quote, clause)
            lines.append(f"| {_markdown_cell(condition)} | {_markdown_cell(requirement)} | {clause} |")
        lines.extend(
            [
                "",
                "若补充资源量规模及矿石属于易选、较易选还是难选/新类型，可据此确定唯一适用行；"
                "在此之前，上表已覆盖该阶段的全部条件组合。",
            ]
        )
        return "\n".join(lines)

    @classmethod
    def _render_projection_comparison(
        cls,
        question: str,
        facts: list[dict[str, Any]],
        sources: list[Source],
    ) -> str:
        source_by_index = {index: source for index, source in enumerate(sources, start=1)}

        def source_for(fact: dict[str, Any]) -> Source | None:
            return next(
                (
                    source_by_index[index]
                    for index in fact.get("source_indices", [])
                    if index in source_by_index
                ),
                None,
            )

        primary = [fact for fact in facts if fact.get("evidence_role") != "finite_contrast"]
        contrasts = [fact for fact in facts if fact.get("evidence_role") == "finite_contrast"]
        signatures = {
            (
                fact.get("projection_type"),
                fact.get("trigger_condition"),
                fact.get("distance_basis"),
                fact.get("pointed_ratio"),
                fact.get("flat_ratio"),
            )
            for fact in primary
        }
        labels = [
            source.standard_no or f"《{source.title}》"
            for fact in primary
            if (source := source_for(fact)) is not None
        ]
        labels = list(dict.fromkeys(labels))

        summary_parts: list[str] = []
        if primary:
            focus = "有限外推" if "有限外推" in question else "无限外推" if "无限外推" in question else "矿体外推"
            summary_parts.append(
                f"本次在 {len(set(labels))} 份文件中提取到 {len(signatures)} 类可比的{focus}规则。"
            )
        switch_facts = [
            fact
            for fact in primary
            if fact.get("distance_relationship")
            and "实际工程间距大于推断资源量工程间距" in str(fact.get("distance_relationship"))
        ]
        if switch_facts:
            switch_labels = list(
                dict.fromkeys(
                    source.standard_no or source.title
                    for fact in switch_facts
                    if (source := source_for(fact)) is not None
                )
            )
            summary_parts.append(
                f"{('、'.join(switch_labels))}明确比较实际工程间距与推断资源量工程间距："
                "实际间距较大时，改按推断资源量工程间距计算外推距离。"
            )
        partial_facts = [
            fact
            for fact in primary
            if fact.get("pointed_ratio") == "2/3" or fact.get("flat_ratio") == "1/3"
        ]
        if partial_facts:
            partial_labels = list(
                dict.fromkeys(
                    source.standard_no or source.title
                    for fact in partial_facts
                    if (source := source_for(fact)) is not None
                )
            )
            summary_parts.append(
                f"{('、'.join(partial_labels))}对相邻工程部分见矿的情形另设 2/3 尖推、1/3 平推，"
                "不能与普通的 1/2 尖推规则合并。"
            )
        if len(signatures) > 1 and not switch_facts and not partial_facts:
            summary_parts.append("具体差异集中在距离基准、外推比例和相邻工程见矿条件，见下表逐项对照。")
        if contrasts:
            summary_parts.append("表末的有限外推条款仅作距离基准对照，不作为无限外推结论。")
        if not summary_parts:
            summary_parts.append("本次没有形成可比较的结构化外推事实。")

        lines = ["**研究结论**", "", "".join(summary_parts), "", "**具体差异**", ""]
        lines.extend(
            [
                "| 文件 | 外推类型 | 触发条件 | 距离基准 | 尖推/尖灭 | 平推 | 适用说明 | 依据条款 |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for fact in [*primary, *contrasts][:40]:
            source = source_for(fact)
            file_label = (
                f"{source.standard_no or ''}《{source.title}》" if source else str(fact.get("document_id") or "未知文件")
            )
            applicability = str(fact.get("adjacent_engineering_condition") or "按该条款触发条件适用")
            if fact.get("evidence_role") == "finite_contrast":
                applicability = f"有限外推对照；{applicability}"
            lines.append(
                "| "
                + " | ".join(
                    [
                        _markdown_cell(file_label),
                        _markdown_cell(fact.get("projection_type") or "未标注"),
                        _markdown_cell(fact.get("trigger_condition") or "未标注"),
                        _markdown_cell(fact.get("distance_basis") or "未标注"),
                        _markdown_cell(fact.get("pointed_ratio") or "未规定"),
                        _markdown_cell(fact.get("flat_ratio") or "未规定"),
                        _markdown_cell(applicability),
                        _markdown_cell(source.chapter if source else "相关条款"),
                    ]
                )
                + " |"
            )
        return "\n".join(lines).strip()

    @classmethod
    def _render_service_material_answer(
        cls,
        plan: ResearchPlan,
        sources: list[Source],
    ) -> str:
        if is_post_filing_license_steps_query(plan.canonical_question):
            transition_answer = cls._render_post_filing_license_steps(sources)
            if transition_answer:
                return transition_answer
        application, change_section = cls._service_application_scope(plan)
        section_prefix = f"附件4 > {application}" if application else None
        if application == "变更" and change_section:
            section_prefix = f"附件4 > 变更 > {change_section}"
        relevant = [
            source
            for source in sources
            if source.title == "采矿权申请资料清单及要求"
            and (not section_prefix or (source.chapter or "").startswith(section_prefix))
        ]
        material_sources = [
            source for source in relevant if re.search(r">\s*材料\s*\d+", source.chapter or "")
        ]

        def material_sequence(source: Source) -> int:
            match = re.search(r"材料\s*(\d+)", source.chapter or "")
            return int(match.group(1)) if match else 999

        material_sources.sort(key=material_sequence)
        if application and material_sources:
            application_name = (
                f"变更（{change_section}）" if application == "变更" and change_section else application
            )
            lines = [
                "**研究结论**",
                "",
                f"采矿权{application_name}申请应按 **自然资规〔2023〕4号附件4《采矿权申请资料清单及要求》** 核对材料。",
                "",
                "**直接材料依据**",
                "",
            ]
            lines.extend(f"- {source.quote}" for source in material_sources[:24])
            lines.extend(
                [
                    "",
                    "表中“要求”栏的特殊规定优先于▲/—标记；还需结合油气/非油气、申请主体和具体变更事项核验适用条件。",
                ]
            )
            return "\n".join(lines).strip()
        if relevant:
            return "\n".join(
                [
                    "**研究结论**",
                    "",
                    "采矿权申请资料分为新立、延续、变更和注销四类，不能合并为一套统一要件。",
                    "",
                    *[f"- {source.quote}" for source in relevant[:8]],
                ]
            ).strip()
        return (
            "**深度研究未形成可引用结论。**\n\n"
            "未检索到自然资规〔2023〕4号附件4《采矿权申请资料清单及要求》的直接材料记录。"
        )

    @staticmethod
    def _render_post_filing_license_steps(sources: list[Source]) -> str | None:
        candidates = [
            source
            for source in sources
            if "采矿权变更（续期）登记临时服务指南" in source.title
            and "申请材料" in f"{source.chapter or ''} {source.quote or ''}"
            and "矿产资源储量评审备案文件" in (source.quote or "")
            and "矿业权出让收益" in (source.quote or "")
        ]
        if not candidates:
            return None
        source = max(candidates, key=lambda item: len(item.quote or ""))
        lines = [
                "**研究结论**",
                "",
                "资源储量评审备案完成后，还需要继续办理采矿权变更（续期）登记申请。",
                "",
                f"根据《{source.title}》的申请材料目录，需要处理以下 5 项：",
                "",
                "1. **填写并提交采矿权登记申请书。**",
                "2. **确认企业法人营业执照信息可被在线核验。** 该材料由登记机关核查，无需申请人另行提交。",
                "3. **按适用情形提交不动产权证书（采矿权）或原采矿许可证。**",
                "4. **提交矿产资源储量评审备案文件或指南要求的矿山储量年报。** 非油气续期通常提交当年或上一年度矿山储量年报；累计查明资源量发生重大变化时提交评审备案文件。",
                "5. **完成矿业权出让收益（价款）缴纳或有偿处置，并取得相应证明材料。** 可使用缴款通知书、分期缴款批复、成交确认书、出让合同、缴纳票据或征收机关书面意见等证明。",
                "",
                "该结论适用于上述自然资源部采矿权变更（续期）办事指南覆盖的情形；其他登记类型或地方发证事项应核对对应办事指南。",
            ]
        if source.url:
            lines.extend(["", f"**官方来源**：[《{source.title}》]({source.url})"])
        return "\n".join(lines)

    @staticmethod
    def _service_application_label(question: str) -> str | None:
        if any(term in question for term in ("首次", "新立")):
            return "新立"
        if any(term in question for term in ("延续", "续期")):
            return "延续"
        if "注销" in question:
            return "注销"
        if any(term in question for term in ("变更", "转让", "转移")):
            return "变更"
        return None

    @classmethod
    def _service_application_scope(
        cls,
        plan: ResearchPlan,
    ) -> tuple[str | None, str | None]:
        classification = plan.query_classification or {}
        application = {
            "new": "新立",
            "renewal": "延续",
            "change": "变更",
            "cancellation": "注销",
        }.get(str(classification.get("application_type") or ""))
        change_section = SERVICE_CHANGE_SECTIONS.get(
            str(classification.get("change_subtype") or "")
        )
        return application or cls._service_application_label(plan.canonical_question), change_section

    @classmethod
    def _render_transfer_answer(cls, sources: list[Source]) -> str:
        general = []
        special = []
        limitations = []
        seen: set[tuple[str, str]] = set()
        for source in sources:
            quote = cls._transfer_direct_quote(source.quote or "")
            if not quote:
                continue
            key = (source.standard_no or "", source.title)
            if key in seen:
                continue
            seen.add(key)
            item = (source, quote)
            if "不能替代探矿权转采矿权" in quote:
                limitations.append(item)
            elif any(term in quote for term in TRANSFER_EQUIVALENT_TERMS):
                special.append(item)
            elif "探矿权转采矿权" in quote:
                general.append(item)

        special.sort(
            key=lambda item: (
                0
                if "可作为矿山设计开采依据" in item[1]
                else 1
                if "供矿山设计开采" in item[1]
                else 2,
                item[0].standard_no or "",
            )
        )

        lines = [
            "**研究结论**",
            "",
            "判断详查报告能否用于转采，应以条款是否明确支持探矿权转采矿权，或是否规定该报告可作为/供矿山设计开采、作为矿山建设设计依据为核心。后类受控表述在本项目的矿业权业务语义中，属于满足条款条件时可以转采的正向依据。",
        ]
        if general:
            lines.extend(["", "**一般转采规定**", ""])
            for source, quote in general[:4]:
                lines.append(
                    f"- **{source.standard_no or '未知文号'}《{source.title}》**"
                    f"（{source.chapter or '相关条款'}）：{quote}"
                )
        if special:
            lines.extend(["", "**分矿种特殊规定**", ""])
            for source, quote in special[:8]:
                lines.append(
                    f"- **{source.standard_no or '未知标准号'}《{source.title}》**"
                    f"（{source.chapter or '相关条款'}）：{quote}"
                )
        if limitations:
            lines.extend(["", "**报告类型限制**", ""])
            for source, quote in limitations[:4]:
                lines.append(
                    f"- **{source.standard_no or '未知标准号'}《{source.title}》**"
                    f"（{source.chapter or '相关条款'}）：{quote}"
                )
        if not any((general, special, limitations)):
            lines.extend(
                [
                    "",
                    "本次没有检索到直接表达转采关系或“可作为矿山设计开采依据”的条款，不能使用普通详查阶段定义替代。",
                ]
            )
        return "\n".join(lines).strip()

    @staticmethod
    def _transfer_direct_quote(text: str) -> str:
        clean = re.sub(r"\s+", " ", text).strip()
        patterns = (
            r"(探矿权转采矿权，应当依据经评审备案的矿产资源储量报告。资源储量规模为大型的非煤矿山、大中型煤矿应当达到勘探程度，其他矿山应当达到详查（含）以上程度。)",
            r"(矿产资源储量核实报告不能替代探矿权转采矿权时应提交的地质勘查报告。)",
            r"((?:卤水.*?|深层固体盐类.*?|详查报告.*?)(?:可作为矿山设计开采依据|供矿山设计开采|作为矿山建设设计的依据).*?。)",
        )
        for pattern in patterns:
            match = re.search(pattern, clean)
            if match:
                return match.group(1).strip()
        for sentence in re.split(r"(?<=[。！？；;])\s*", clean):
            compact = re.sub(r"\s+", "", sentence)
            if (
                any(re.sub(r"\s+", "", term) in compact for term in TRANSFER_EQUIVALENT_TERMS)
                and any(re.sub(r"\s+", "", term) in compact for term in TRANSFER_REPORT_OBJECT_TERMS)
                and ("详终" in compact or "可行性研究" in compact or "工业价值" in compact)
            ):
                return sentence[:700].strip()
        for sentence in re.split(r"(?<=[。！？；;])\s*", clean):
            if "探矿权转采矿权" not in sentence:
                continue
            if not any(term in sentence for term in TRANSFER_OBJECT_TERMS):
                continue
            if not any(term in sentence for term in ("依据", "条件", "达到", "应提交", "不能替代")):
                continue
            return sentence[:700].strip()
        return ""

    @staticmethod
    def _summary_matches_scope(question: str, summary: str) -> bool:
        if re.search(r"\b(?:compilation_|chunk-)[A-Za-z0-9_-]+", summary):
            return False
        if UNSUPPORTED_ABSENCE_PATTERN.search(summary):
            return False
        if "无限外推" in question and "有限外推" in summary:
            return False
        if "有限外推" in question and "无限外推" in summary:
            return False
        return True

    async def _finish_insufficient(
        self,
        store,
        task: dict[str, Any],
        plan: ResearchPlan,
        snapshot: str | None,
        total_documents: int,
        candidate_truncated: bool,
        reason: str,
        settings: Settings,
        *,
        examined_documents: int = 0,
    ) -> None:
        answer = (
            "**深度研究未形成可引用结论。**\n\n"
            f"{reason}\n\n"
            "系统没有使用模型常识替代标准正文。建议补充目标文件正文、缩小候选范围，"
            "或由知识库管理员完成缺失文件入库后重新研究。"
        )
        quota = store.settle_qa_quota(
            task["request_id"],
            "insufficient_evidence",
            len(answer),
            settings.quota_timezone,
        )
        notes = [reason, plan.scope_note]
        result = ResearchResult(
            task_id=task["task_id"],
            request_id=task["request_id"],
            question=task["question"],
            session_id=task["conversation_id"],
            answer=answer,
            status="insufficient_evidence",
            quota_cost=int(task["quota_cost"]),
            reserved_quota_units=int(task["reserved_quota_units"]),
            limitations=Limitations(has_clause_level_evidence=False, notes=notes),
            coverage=ResearchCoverage(
                examined_documents=examined_documents,
                total_documents=total_documents,
                evidence_documents=0,
                candidate_truncated=candidate_truncated,
                knowledge_snapshot=snapshot,
                notes=notes,
            ),
            confidence="low",
            quota=QuotaInfo(**quota),
            query_classification=plan.query_classification,
        )
        store.complete_research_task(
            task["task_id"],
            "insufficient_evidence",
            result.model_dump(mode="json"),
        )
        self._save_exchange(store, task, result)
        self._write_usage(task, result)

    @staticmethod
    def _save_exchange(store, task: dict[str, Any], result: ResearchResult) -> None:
        try:
            store.save_exchange(
                task["user_id"],
                task["conversation_id"],
                task["request_id"],
                task["question"],
                result.answer,
                {
                    "mode": "deep",
                    "task_id": task["task_id"],
                    "status": result.status,
                    "confidence": result.confidence,
                    "sources": [source.model_dump(mode="json") for source in result.sources],
                    "limitations": result.limitations.model_dump(mode="json"),
                    "coverage": result.coverage.model_dump(mode="json"),
                    "quota": result.quota.model_dump(mode="json") if result.quota else None,
                },
            )
        except Exception:
            logger.exception("Unable to persist deep research conversation %s", task["task_id"])

    def _write_usage(self, task: dict[str, Any], result: ResearchResult) -> None:
        self._usage.write(
            {
                "user_id": task["user_id"],
                "credential_id": task.get("api_key_id"),
                "auth_type": "api_key" if task.get("api_key_id") else "session",
                "endpoint": "/api/research/tasks",
                "method": "BACKGROUND",
                "request_id": task["request_id"],
                "task_id": task["task_id"],
                "status": result.status,
                "source_count": len(result.sources),
                "quota_consumed_units": result.quota.consumed_units if result.quota else 0,
                "quota_remaining": result.quota.remaining if result.quota else None,
                "examined_documents": result.coverage.examined_documents,
                "total_documents": result.coverage.total_documents,
            }
        )


def research_task_response(task: dict[str, Any], quota: dict[str, Any] | None = None) -> ResearchTaskResponse:
    result_quota = (task.get("result") or {}).get("quota") if isinstance(task.get("result"), dict) else None
    effective_quota = result_quota or quota
    return ResearchTaskResponse(
        task_id=task["task_id"],
        request_id=task["request_id"],
        question=task["question"],
        session_id=task["conversation_id"],
        status=task["status"],
        quota_cost=int(task["quota_cost"]),
        reserved_quota_units=int(task["reserved_quota_units"]),
        progress=ResearchProgress(
            stage=task["stage"],
            percent=int(task["progress_percent"]),
            message=task["status_message"] or "",
            examined_documents=int(task["examined_documents"]),
            total_documents=int(task["total_documents"]),
            evidence_documents=int(task["evidence_documents"]),
        ),
        result_available=task.get("result") is not None,
        quota=QuotaInfo(**effective_quota) if effective_quota else None,
        query_classification=(
            (task.get("result") or {}).get("query_classification")
            if isinstance(task.get("result"), dict)
            else (task.get("query_plan") or {}).get("classification")
        )
        or (task.get("query_plan") or {}).get("classification"),
        created_at=task["created_at"],
        started_at=task.get("started_at"),
        finished_at=task.get("finished_at"),
    )


research_runner = ResearchTaskRunner()
