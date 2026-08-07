from __future__ import annotations

import json
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Literal, get_args

from .config import Settings
from .llm_client import LLMClient
from .llm_observability import llm_call_context
from .prompt_layout import structured_prompt_messages, unwrap_output_schema_envelope
from .prompt_registry import prompt_text
from .query_classification import ALLOWED_DOCUMENT_TYPES, controlled_document_types
from .query_understanding import (
    QueryPlan,
    TRANSFER_REPORT_OBJECT_TERMS,
    apply_semantic_plan,
    is_reserve_filing_materials_query,
    normalize_user_query,
)
from .technical_stage_requirements import (
    stage_requirement_evidence_refs,
    stage_requirement_label,
)


PlannerIntent = Literal[
    "general",
    "standard_selection",
    "engineering_distance_lookup",
    "projection_comparison",
    "projection_numeric_rule",
    "projection_rule",
    "authority_responsibility",
    "service_materials",
    "service_procedure_basis",
    "service_time_limit",
    "legal_responsibility",
    "exploration_to_mining_eligibility",
    "companion_resource_type",
    "exploration_type_factors",
    "basic_analysis_items",
    "regulation_lookup",
    "clause_comparison",
    "related_documents",
    "reserve_estimation_basis",
    "definition_explanation",
    "cross_document_audit",
    "technical_requirement_sufficiency",
    "technical_test_conformity_verification",
    "technical_stage_requirement",
]

SearchMode = Literal["default", "scoped", "comparison", "exhaustive", "catalog"]

@dataclass(frozen=True)
class QueryVariant:
    """One independently verifiable evidence objective for a user question."""

    target: str
    query: str
    document_types: tuple[str, ...] = ()
    alternative_terms: tuple[str, ...] = ()
    required: bool = True


@dataclass(frozen=True)
class PlannerResult:
    plan: QueryPlan
    used: bool
    elapsed_ms: float
    error: str | None = None
    query_variants: tuple[QueryVariant, ...] = ()
    evidence_targets: tuple[QueryVariant, ...] = ()


class RetrievalPlanner:
    def __init__(self, settings: Settings, llm: LLMClient | None = None):
        self.settings = settings
        self.llm = llm or LLMClient(settings)

    async def aclose(self) -> None:
        await self.llm.aclose()

    async def plan(self, question: str, base_plan: QueryPlan) -> PlannerResult:
        started = perf_counter()
        if not self.settings.query_planner_enabled or not self.llm.enabled:
            plan = apply_semantic_plan(base_plan, None)
            variants = self._govern_query_variants(
                question,
                plan,
                self._deterministic_query_variants(plan),
            )
            return PlannerResult(
                plan=plan,
                used=False,
                elapsed_ms=(perf_counter() - started) * 1000,
                query_variants=variants,
                evidence_targets=variants or self._primary_evidence_target(plan),
            )

        base_system = (
                    "你是 geowiki 的地质矿产知识库检索规划器，只理解问题和制定检索计划，不回答问题。"
                    "你的输出将用于搜索本地权威标准、政策、办事指南和条款，不是互联网搜索。"
                    "必须保留矿种、标准号、文号、条款号、数值、比例、勘查阶段、业务事项、责任主体和限定条件。"
                    "将口语表达转换为专业检索概念，例如转采应理解为探矿权转采矿权，"
                    "但不得把模型记忆中的结论当作证据。"
                    "比较问题必须明确比较对象、比较维度，以及每条证据必须同时出现的事实组。"
                    "如果用户问哪些制度允许详查报告用于探矿权转采矿权，应识别为 exploration_to_mining_eligibility，"
                    "检索政策中的转采条件、勘查程度要求和技术标准中的报告类型限制；"
                    "必须区分行政上的探矿权转采矿权与技术上的矿山设计开采依据。"
                    "凡是一个结论同时依赖行政准入条件、技术要求、阶段要求、材料要求或例外条件中的两项以上，"
                    "必须生成对应数量的 subqueries，每一条只服务一个独立证据目标；"
                    "这些目标不是可选同义词扩展，任何一个没有直接条款时都不能宣称已形成完整结论。"
                    "用户已经列举多个条件分支时，应保留为条件矩阵，不得改写为跨文件比较或先要求补充其中一个分支。"
                    "行政申请语境中的‘要件、必备资料、所需资料’应理解为申请材料；"
                    "‘估算储量、提交储量、资源量转换为储量’中的储量是技术动作对象；出现预可研、"
                    "可研、技术经济评价、开发利用方案、初步设计或排产计划时，必须保持"
                    "reserve_estimation_basis，分别检索最低转换要求和各阶段技术依据，不能改成"
                    "评审备案报件清单。只有明确询问评审备案申请材料、报件或材料清单才是"
                    "service_materials，且不能用‘采矿权申请需要备案文件’反向证明备案申请材料。"
                    "政策正文引用附件清单时，document_types 必须包含 policy_attachment，不能只检索父政策正文。"
                    "required_evidence_groups 是 AND 关系，每个子数组内部是 OR 关系。"
                    "普通工程间距表不能作为矿体外推规则证据；仅出现同一个词但没有目标关系的内容应放入 negative_terms。"
                    "candidate_titles 和 standard_numbers 只有在问题明确给出或你高度确信时填写。"
                    "必须区分业务背景与回答目标。‘转采审查中、办理转采时’可以只是背景；"
                    "若用户实际询问勘查阶段选冶试验级别、规模或条件分支，intent 必须是"
                    "technical_stage_requirement，不能改成 exploration_to_mining_eligibility。"
                    "‘不要回答转采总体要求、不得用转采条款替代’是排除范围，不是正向转采锚点。"
                    "权限问题必须严格区分许可证颁发机关与矿业权出让机关。"
                    "license_issuer_level 只表示用户现有勘查许可证或采矿许可证由哪一级机关颁发；"
                    "mining_right_granting_level 只表示矿业权出让、配置或登记权限层级。"
                    "两者不能相互替代；问题未明确时必须返回 unknown，不能根据矿种或规模猜测。"
                    "对于复杂比较或多证据槽位问题，可以给出最多3条 subqueries；"
                    "每条必须对应不同证据目标，不能只改写同义词。简单问题返回空数组。"
                    "每条 subquery 都必须提供 2 至 4 个 alternative_terms，覆盖用户口语、事实描述与"
                    "法规或标准中的正式表述；不能只重复用户原词或父问题的文件锚点。"
                    "alternative_terms 的每一项必须是可单独全文匹配的短语，不得把多个关系词拼接成一句。"
                    "复合行政办理问题必须拆分为彼此独立的法律关系和办理环节。例如用户同时问"
                    "‘同一主体、相邻矿业权、夹缝区域、扩大矿区范围’，至少应分别检索："
                    "夹缝资源是否符合协议出让或其他配置条件，以及既有采矿权如何办理矿区范围变更登记。"
                    "对于上述配置条件，alternative_terms 应覆盖‘相邻矿业权、夹缝区域、协议方式出让’等"
                    "正式关系表达，不能只写‘夹缝资源、协议出让’。"
                    "不得因为问题中出现‘扩大矿区范围’就只检索变更登记材料；也不得将模型记忆中的"
                    "文号作为硬过滤条件。应使用规范化关系短语在本地知识库中核验来源。"
                    "document_types 只能从 standard、national_standard、industry_standard、policy_document、"
                    "policy_attachment、law、regulation、department_rule、guidance、service_guide、"
                    "administrative_service_guide、amendment 中选择。"
                    "只返回符合给定结构的 JSON。"
                    "\n"
        )
        dynamic_payload = {
                        "question": question,
                        "deterministic_plan": {
                            "normalized_query": base_plan.normalized_query,
                            "intent": base_plan.intent,
                            "target_exploration_type": base_plan.target_exploration_type,
                            "candidate_titles": base_plan.candidate_title_terms,
                            "standard_numbers": base_plan.standard_numbers,
                        },
        }
        output_schema = {
                            "canonical_query": "更专业且完整的检索问题",
                            "intent": "允许的意图标签",
                            "search_mode": "default|scoped|comparison|exhaustive|catalog",
                            "subject_terms": ["核心对象"],
                            "required_terms": ["必须优先检索的短语"],
                            "alternative_terms": ["同义或相关专业术语"],
                            "negative_terms": ["语义相近但不回答问题的内容"],
                            "candidate_titles": [],
                            "standard_numbers": [],
                            "document_types": [],
                            "output_mode": "default|table",
                            "required_evidence_groups": [["每组至少命中一个术语"]],
                            "comparison_dimensions": [],
                            "license_issuer_level": "unknown|ministry|province",
                            "mining_right_granting_level": "unknown|ministry|province",
                            "subqueries": [
                                {
                                    "target": "独立证据槽位或比较维度",
                                    "query": "只用于本地知识库检索的子查询",
                                    "document_types": ["该证据目标应优先检索的文件类型"],
                                    "alternative_terms": ["法规或标准中的正式替代表述"],
                                }
                            ],
                            "confidence": 0.0,
        }
        registry_instruction = prompt_text(
            self.settings,
            "retrieval_planner",
            primary_intent=(
                base_plan.classification.primary_intent
                if base_plan.classification
                else None
            ),
        )
        messages = structured_prompt_messages(
            layout=self.settings.prompt_layout_variant,
            base_system=base_system,
            dynamic_system_suffix=registry_instruction,
            dynamic_payload=dynamic_payload,
            output_schema=output_schema,
        )
        try:
            with llm_call_context("retrieval_planner"):
                raw = await self.llm.complete_json(
                    messages,
                    max_tokens=self.settings.query_planner_max_tokens,
                )
            payload = unwrap_output_schema_envelope(json.loads(raw))
            if not isinstance(payload, dict):
                raise ValueError("planner response must be a JSON object")
            allowed_intents = set(get_args(PlannerIntent))
            allowed_search_modes = set(get_args(SearchMode))
            semantic_intent = str(payload.get("intent") or "")
            semantic_search_mode = str(payload.get("search_mode") or "")
            payload["canonical_query"] = str(
                payload.get("canonical_query") or base_plan.normalized_query
            )
            payload["intent"] = semantic_intent if semantic_intent in allowed_intents else base_plan.intent
            payload["search_mode"] = (
                semantic_search_mode if semantic_search_mode in allowed_search_modes else "default"
            )
            payload["document_types"] = list(
                controlled_document_types(payload.get("document_types"))
            )
            for role_field in ("license_issuer_level", "mining_right_granting_level"):
                role_value = str(payload.get(role_field) or "unknown").strip().lower()
                payload[role_field] = (
                    role_value if role_value in {"unknown", "ministry", "province"} else "unknown"
                )
            plan = apply_semantic_plan(base_plan, payload)
            plan = replace(
                plan,
                intent=base_plan.intent,
                classification=base_plan.classification,
            )
            variants = self._query_variants(payload.get("subqueries"), plan)
            variants = self._govern_query_variants(question, plan, variants)
            return PlannerResult(
                plan=plan,
                used=True,
                elapsed_ms=(perf_counter() - started) * 1000,
                query_variants=variants,
                # A multi-part question is answerable only when every model-
                # identified target has direct evidence. Simple questions use
                # one primary target and keep the low-latency retrieval path.
                evidence_targets=variants or self._primary_evidence_target(plan),
            )
        except (json.JSONDecodeError, TypeError, ValueError, OSError) as error:
            plan = apply_semantic_plan(base_plan, None)
            variants = self._govern_query_variants(
                question,
                plan,
                self._deterministic_query_variants(plan),
            )
            return PlannerResult(
                plan=plan,
                used=False,
                elapsed_ms=(perf_counter() - started) * 1000,
                error=type(error).__name__,
                query_variants=variants,
                evidence_targets=variants or self._primary_evidence_target(plan),
            )
        except Exception as error:
            plan = apply_semantic_plan(base_plan, None)
            variants = self._govern_query_variants(
                question,
                plan,
                self._deterministic_query_variants(plan),
            )
            return PlannerResult(
                plan=plan,
                used=False,
                elapsed_ms=(perf_counter() - started) * 1000,
                error=type(error).__name__,
                query_variants=variants,
                evidence_targets=variants or self._primary_evidence_target(plan),
            )

    @classmethod
    def result_from_payload(
        cls,
        question: str,
        base_plan: QueryPlan,
        payload: dict[str, object],
        *,
        elapsed_ms: float = 0.0,
    ) -> PlannerResult:
        """Compile a validated model payload without making another model call.

        The unified question-understanding experiment emits the same planning
        fields as the dedicated planner. Keeping compilation here gives both
        paths identical allow-lists, protected intents and query-variant
        governance.
        """

        if not isinstance(payload, dict):
            raise TypeError("planner response must be a JSON object")
        value = dict(payload)
        allowed_intents = set(get_args(PlannerIntent))
        allowed_search_modes = set(get_args(SearchMode))
        semantic_intent = str(value.get("intent") or "")
        semantic_search_mode = str(value.get("search_mode") or "")
        value["canonical_query"] = str(
            value.get("canonical_query") or base_plan.normalized_query
        )
        value["intent"] = (
            semantic_intent if semantic_intent in allowed_intents else base_plan.intent
        )
        value["search_mode"] = (
            semantic_search_mode
            if semantic_search_mode in allowed_search_modes
            else "default"
        )
        value["document_types"] = list(
            controlled_document_types(value.get("document_types"))
        )
        for role_field in ("license_issuer_level", "mining_right_granting_level"):
            role_value = str(value.get(role_field) or "unknown").strip().lower()
            value[role_field] = (
                role_value
                if role_value in {"unknown", "ministry", "province"}
                else "unknown"
            )
        plan = apply_semantic_plan(base_plan, value)
        plan = replace(
            plan,
            intent=base_plan.intent,
            classification=base_plan.classification,
        )
        variants = cls._query_variants(value.get("subqueries"), plan)
        variants = cls._govern_query_variants(question, plan, variants)
        return PlannerResult(
            plan=plan,
            used=True,
            elapsed_ms=elapsed_ms,
            query_variants=variants,
            evidence_targets=variants or cls._primary_evidence_target(plan),
        )

    @staticmethod
    def unified_result_is_complete(result: PlannerResult) -> bool:
        """Conservatively decide whether a combined resolver plan is usable.

        ``required_evidence_groups`` are AND groups.  When the combined model
        emits more than one such group but no independent query variant, it
        has violated the unified prompt's completeness contract.  A single
        retrieval route may then return a plausible answer while dropping the
        intended authoritative evidence.  In that narrow case the caller must
        run the dedicated planner; one group, or explicit variants, remains on
        the one-call path.
        """

        if not result.used or not result.evidence_targets:
            return False
        return not (
            len(result.plan.required_evidence_groups) > 1
            and not result.query_variants
        )

    @classmethod
    def rebase_result(
        cls,
        question: str,
        resolved_plan: QueryPlan,
        preliminary: PlannerResult,
    ) -> PlannerResult:
        """Apply a parallel planner result to the semantically resolved plan."""

        if not preliminary.used:
            raise ValueError("cannot rebase an unavailable planner result")
        source = preliminary.plan
        payload: dict[str, object] = {
            "canonical_query": source.normalized_query,
            "intent": source.intent,
            "search_mode": source.search_mode,
            "subject_terms": list(source.subject_terms),
            "required_terms": list(source.required_terms),
            "alternative_terms": list(source.alternative_terms),
            "negative_terms": list(source.negative_terms),
            "candidate_titles": list(source.candidate_title_terms),
            "standard_numbers": list(source.standard_numbers),
            "document_types": list(source.document_types),
            "output_mode": source.output_mode,
            "required_evidence_groups": [
                list(group) for group in source.required_evidence_groups
            ],
            "comparison_dimensions": list(source.comparison_dimensions),
            "license_issuer_level": source.license_issuer_level,
            "mining_right_granting_level": source.mining_right_granting_level,
            "confidence": source.planner_confidence,
            "subqueries": [
                {
                    "target": variant.target,
                    "query": variant.query,
                    "document_types": list(variant.document_types),
                    "alternative_terms": list(variant.alternative_terms),
                }
                for variant in preliminary.query_variants
            ],
        }
        return cls.result_from_payload(
            question,
            resolved_plan,
            payload,
            elapsed_ms=preliminary.elapsed_ms,
        )

    @staticmethod
    def _primary_evidence_target(plan: QueryPlan) -> tuple[QueryVariant, ...]:
        deterministic = RetrievalPlanner._deterministic_query_variants(plan)
        if deterministic:
            return deterministic
        query = normalize_user_query(plan.retrieval_query or plan.normalized_query)
        if not query:
            return ()
        return (
            QueryVariant(
                target="核心结论",
                query=query,
                document_types=plan.document_types,
                alternative_terms=plan.alternative_terms[:4],
            ),
        )

    @staticmethod
    def _deterministic_query_variants(plan: QueryPlan) -> tuple[QueryVariant, ...]:
        if (
            plan.intent == "service_materials"
            and is_reserve_filing_materials_query(plan.normalized_query)
        ):
            return (
                QueryVariant(
                    target="矿产资源储量评审备案自身的完整申请材料目录",
                    query=(
                        "矿产资源储量评审备案服务指南 申请材料 申请函 "
                        "矿产资源储量信息表 矿产资源储量报告 附图 附表 附件"
                    ),
                    document_types=("service_guide", "administrative_service_guide"),
                    alternative_terms=("申请函", "信息表", "储量报告", "申请材料目录"),
                ),
            )
        if plan.intent == "technical_stage_requirement":
            refs = stage_requirement_evidence_refs(plan.normalized_query)
            general_refs = tuple(
                clause
                for standard_no, clause in refs
                if standard_no == "DZ/T 0340-2020"
            )
            variants: list[QueryVariant] = []
            if "6.5.6" in general_refs:
                opening_refs = tuple(
                    clause
                    for clause in general_refs
                    if clause in {"6.1.1", "6.5.1", "6.5.2"}
                )
                closing_refs = tuple(
                    clause
                    for clause in general_refs
                    if clause in {"6.5.3", "6.5.4", "6.5.6"}
                )
                variants.extend(
                    [
                        QueryVariant(
                            target="选冶试验确定维度及易选分支",
                            query=(
                                f"DZ/T 0340-2020 {' '.join(opening_refs)} "
                                "试验研究程度要求取决于不同勘查阶段 "
                                "矿石加工选冶难易程度 资源量规模 附录A"
                            ).strip(),
                            document_types=("standard", "national_standard", "industry_standard"),
                            alternative_terms=("易选矿石", "实验室流程试验", "资源量规模"),
                        ),
                        QueryVariant(
                            target="较易选、难选分支及采样困难例外",
                            query=(
                                f"DZ/T 0340-2020 {' '.join(closing_refs)} "
                                f"{stage_requirement_label(plan.normalized_query)} "
                                "大型资源量规模 较易选 难选 实验室扩大连续试验 "
                                "样品采集困难 成果可靠性"
                            ).strip(),
                            document_types=("standard", "national_standard", "industry_standard"),
                            alternative_terms=("半工业试验", "工业试验", "成果应用的可靠性"),
                        ),
                    ]
                )
            else:
                variants.append(
                    QueryVariant(
                        target="通用选冶试验条件矩阵",
                        query=(
                            f"DZ/T 0340-2020 {' '.join(general_refs)} "
                            f"{stage_requirement_label(plan.normalized_query)} "
                            "资源量规模 易选 较易选 难选 试验研究程度"
                        ).strip(),
                        document_types=("standard", "national_standard", "industry_standard"),
                        alternative_terms=("矿石加工选冶难易程度", "试验研究程度"),
                    )
                )
            if any(standard_no == "DZ/T 0205-2020" for standard_no, _ in refs):
                variants.append(
                    QueryVariant(
                        target="岩金勘探阶段矿种专项要求",
                        query=(
                            "DZ/T 0205-2020 4.3.4 矿产地质勘查规范 岩金 勘探阶段 "
                            "易选 较易选 难选 新类型矿石 实验室流程试验 "
                            "实验室扩大连续试验 半工业试验 工业试验"
                        ),
                        document_types=("standard", "national_standard", "industry_standard"),
                        alternative_terms=("岩金", "矿石类型", "选冶技术性能试验"),
                    )
                )
            return tuple(variants)
        if plan.intent != "reserve_estimation_basis":
            return ()
        return (
            QueryVariant(
                target="资源量转换为储量的规范性最低要求",
                query=(
                    "资源量转换为储量 至少经过预可行性研究 "
                    "与之相当的技术经济评价 转换因素"
                ),
                document_types=("standard", "national_standard", "industry_standard"),
                alternative_terms=("储量", "预可行性研究", "技术经济评价", "转换因素"),
            ),
            QueryVariant(
                target="各阶段提交储量的技术经济依据",
                query=(
                    "勘查阶段拟提交储量 矿山建设阶段 矿山正常生产阶段 "
                    "停产超过3年 开发利用方案 初步设计 排产计划"
                ),
                document_types=("guidance", "industry_standard"),
                alternative_terms=("可行性研究", "开发利用方案", "矿山初步设计", "排产计划"),
            ),
        )

    @staticmethod
    def _govern_query_variants(
        question: str,
        plan: QueryPlan,
        variants: tuple[QueryVariant, ...],
    ) -> tuple[QueryVariant, ...]:
        """Reject a model-created report target that the user never asked for.

        A stage-only transfer question is directly answered by the current
        policy clause that states both the report prerequisite and the stage
        matrix.  DZ/T 0430-2023's report-type limitation remains optional
        supporting context and must not become a second required target unless
        the user explicitly asks about a report object.
        """

        if (
            plan.intent == "service_materials"
            and is_reserve_filing_materials_query(plan.normalized_query)
        ):
            return RetrievalPlanner._deterministic_query_variants(plan)
        if plan.intent == "reserve_estimation_basis":
            return RetrievalPlanner._deterministic_query_variants(plan)
        if plan.intent == "technical_stage_requirement":
            return RetrievalPlanner._deterministic_query_variants(plan)
        if plan.intent != "exploration_to_mining_eligibility":
            return variants
        normalized = normalize_user_query(question)
        if any(term in normalized for term in TRANSFER_REPORT_OBJECT_TERMS):
            return variants
        return ()

    @staticmethod
    def _query_variants(values: object, plan: QueryPlan) -> tuple[QueryVariant, ...]:
        if not isinstance(values, list):
            return ()
        variants: list[QueryVariant] = []
        seen = {plan.retrieval_query, plan.normalized_query}
        protected_suffix = ""
        if plan.scope_origin == "user":
            protected_suffix = " ".join(
                dict.fromkeys(
                    (
                        plan.normalized_query,
                        *plan.standard_numbers,
                        *plan.subject_terms,
                        *plan.required_terms,
                    )
                )
            )
        for value in values[:3]:
            if not isinstance(value, dict):
                continue
            target = normalize_user_query(str(value.get("target") or ""))[:120]
            query = normalize_user_query(str(value.get("query") or ""))[:500]
            if not target or not query:
                continue
            raw_document_types = value.get("document_types") or []
            if isinstance(raw_document_types, str):
                raw_document_types = [raw_document_types]
            document_types: list[str] = []
            if isinstance(raw_document_types, list):
                for document_type in raw_document_types:
                    value_type = str(document_type).strip()
                    if value_type == "standard":
                        document_types.extend(("standard", "national_standard", "industry_standard"))
                    elif value_type in ALLOWED_DOCUMENT_TYPES:
                        document_types.append(value_type)
            raw_alternative_terms = value.get("alternative_terms") or []
            if isinstance(raw_alternative_terms, str):
                raw_alternative_terms = [raw_alternative_terms]
            if not isinstance(raw_alternative_terms, list):
                raw_alternative_terms = []
            alternative_terms = tuple(
                term
                for term in (
                    normalize_user_query(str(item or ""))[:120]
                    for item in raw_alternative_terms
                )
                if term
            )[:4]
            if protected_suffix:
                query = " ".join(dict.fromkeys((query, protected_suffix)))[:700]
            if query in seen:
                continue
            seen.add(query)
            variants.append(
                QueryVariant(
                    target=target,
                    query=query,
                    document_types=tuple(dict.fromkeys(document_types)),
                    alternative_terms=tuple(dict.fromkeys(alternative_terms)),
                )
            )
        return tuple(variants)
