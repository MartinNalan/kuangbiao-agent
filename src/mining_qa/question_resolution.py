from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .config import Settings
from .domain_gate import DomainGate
from .llm_client import LLMClient
from .prompt_registry import prompt_text
from .query_classification import (
    PRIMARY_INTENTS,
    build_classification,
    classification_from_payload,
    legacy_intent_for_primary,
    legacy_primary_intent,
    merge_slot_updates,
    strategy_for,
)
from .query_understanding import (
    PROTECTED_QUERY_INTENTS,
    QueryPlan,
    default_document_types,
    default_evidence_groups,
    is_post_filing_license_steps_query,
    normalize_user_query,
    understand_query,
)
from .schemas import Clarification, ClarificationOption


BROAD_ACTION_MARKERS = (
    "怎么处理",
    "如何处理",
    "怎么办",
    "如何解决",
    "怎么解决",
    "处理方法",
    "处理措施",
    "治理方法",
    "治理措施",
)
GOAF_SPECIFIC_GOALS = (
    "稳定性",
    "积水",
    "水害",
    "塌陷",
    "沉陷",
    "监测",
    "调查",
    "评价",
    "充填",
    "封闭",
    "支护",
    "复垦",
)
MINING_RIGHT_APPLICATION_TERMS = (
    "新立",
    "首次登记",
    "延续",
    "续期",
    "变更",
    "注销",
)
MINING_RIGHT_MATERIAL_TERMS = (
    "要件",
    "材料",
    "资料",
    "清单",
    "提交什么",
    "提交哪些",
)
MINING_RIGHT_LICENSE_TERMS = (
    "采矿证",
    "采矿许可证",
    "采矿权",
)


class ResolutionOptionPayload(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    question: str = Field(min_length=2, max_length=300)
    description: str = Field(default="", max_length=100)
    slot_updates: dict[str, str] = Field(default_factory=dict)


class ResolutionPayload(BaseModel):
    canonical_question: str = Field(min_length=1, max_length=600)
    intent: str = Field(default="general", max_length=80)
    is_ambiguous: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    missing_slots: list[str] = Field(default_factory=list, max_length=8)
    reason: str = Field(default="", max_length=300)
    interpretations: list[ResolutionOptionPayload] = Field(default_factory=list, max_length=4)
    primary_intent: str = Field(default="", max_length=80)
    secondary_intents: list[str] = Field(default_factory=list, max_length=4)
    target_entity: str | None = Field(default=None, max_length=160)
    mineral: str | None = Field(default=None, max_length=40)
    business_action: str | None = Field(default=None, max_length=160)
    completed_stage: str | None = Field(default=None, max_length=160)
    target_outcome: str | None = Field(default=None, max_length=160)
    document_types: list[str] = Field(default_factory=list, max_length=12)
    evidence_slots: list[str] = Field(default_factory=list, max_length=12)
    output_shape: str = Field(default="", max_length=80)
    ambiguities: list[str] = Field(default_factory=list, max_length=8)
    application_type: str | None = Field(default=None, max_length=40)
    change_subtype: str | None = Field(default=None, max_length=60)
    authority_relation: str | None = Field(default=None, max_length=60)
    license_issuer_level: str = Field(default="unknown", max_length=20)
    comparison_topic: str | None = Field(default=None, max_length=160)
    comparison_scope: str | None = Field(default=None, max_length=80)


@dataclass(frozen=True)
class QuestionResolution:
    canonical_question: str
    plan: QueryPlan
    model_used: bool = False
    clarification: Clarification | None = None
    error: str | None = None

    @property
    def requires_clarification(self) -> bool:
        return self.clarification is not None


class QuestionResolver:
    def __init__(self, settings: Settings, llm: LLMClient | None = None):
        self.settings = settings
        self.llm = llm or LLMClient(settings)
        self.domain_gate = DomainGate()

    async def aclose(self) -> None:
        await self.llm.aclose()

    async def resolve(
        self,
        question: str,
        *,
        mode: str = "basic",
        conversation_context: tuple[str, ...] | list[str] = (),
        inherited_plan: QueryPlan | None = None,
        resolved_slots: dict[str, str] | None = None,
    ) -> QuestionResolution:
        normalized = normalize_user_query(question)
        mechanical = self._minimal_domain_corrections(normalized)
        recent_questions = tuple(
            value
            for value in (
                normalize_user_query(item)
                for item in list(conversation_context)[-4:]
            )
            if value and value != normalized
        )
        contextual_fallback = self._apply_conversation_guard(
            mechanical,
            mechanical,
            recent_questions,
        )
        base_plan = self._inherit_plan(
            inherited_plan,
            understand_query(contextual_fallback),
            resolved_slots,
        )
        fallback = QuestionResolution(canonical_question=contextual_fallback, plan=base_plan)
        if not normalized:
            return fallback
        schema_fallback = self._schema_clarification(
            contextual_fallback,
            base_plan,
            contextual_fallback,
            [],
        )
        if not self.settings.question_resolution_enabled:
            if schema_fallback is not None:
                return replace(fallback, clarification=schema_fallback)
            return fallback
        if not self.llm.enabled or not self._needs_model(contextual_fallback, base_plan):
            if schema_fallback is not None:
                return QuestionResolution(
                    canonical_question=contextual_fallback,
                    plan=base_plan,
                    clarification=schema_fallback,
                )
            return fallback

        registry_instruction = prompt_text(
            self.settings,
            "question_resolution",
            primary_intent=(
                base_plan.classification.primary_intent
                if base_plan.classification
                else None
            ),
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 geowiki 的问题理解与歧义判断器，只理解问题，不回答问题，不检索标准，"
                    "服务领域是矿产资源、地质勘查、矿业权登记、储量管理、标准规范和办事指南。"
                    "先纠正常见输入错误、同音字和形近字，例如‘采矿正’应理解为‘采矿证’，"
                    "再识别用户真正办理的事项；不得改写标准号、文号、数值或用户明确限定的对象。"
                    "最近用户问题仅用于消解省略、回答追问或恢复被误解的原始目标，当前用户的纠正优先。"
                    "出现‘与X无关、不是X、不要讨论X’时，canonical_question 必须排除X，不能继续检索X。"
                    "不得使用模型记忆生成专业结论。判断歧义时关注：不同解释是否会改变目标标准、"
                    "条款范围、业务事项或最终结论。只有存在两个以上实质不同且合理的专业方向，"
                    "或缺少决定结论的关键条件时，才要求确认；不要对表达清楚的问题过度追问。"
                    "矿产资源储量评审备案机构取决于许可证颁发机关；用户只给矿种或矿山规模而未说明"
                    "许可证由自然资源部还是省级自然资源主管部门颁发时，必须判为歧义并给出对应候选。"
                    "上述发证机关规则只适用于用户明确询问矿产资源储量评审备案机构的情形，"
                    "不得用于采矿许可证办理材料、申请要件或办理流程。"
                    "采矿许可证办理材料首先按新立、延续、变更、注销区分，不按发证机关生成候选。"
                    "当用户询问‘评审备案后、领取采矿许可证前还需办理什么’时，目标是采矿权登记的"
                    "申请材料、缴费或有偿处置等待办事项，intent 应为 service_materials，不能归为"
                    "license_reference、authority_responsibility 或跨文件比较。"
                    "intent 优先使用 authority_responsibility、service_materials、service_procedure_basis、"
                    "service_time_limit、standard_selection、definition_explanation、engineering_distance_lookup、"
                    "projection_numeric_rule、projection_comparison、legal_responsibility、general 之一。"
                    "同时按统一分类给出 primary_intent：standard_catalog_lookup、status_verification、"
                    "clause_lookup、definition_lookup、numeric_table_lookup、service_materials、service_workflow、"
                    "authority_jurisdiction、eligibility_condition、cross_document_comparison、document_inventory、"
                    "technical_method 或 out_of_scope。采矿权材料问题必须抽取 application_type；"
                    "application_type=change 时还必须抽取 change_subtype。用户已明确‘扩大范围’等子事项时"
                    "不得再次要求确认。领域词典不负责决定 primary_intent。"
                    "候选解释必须完整、互斥、仍属于地质矿产领域，并可直接作为后续知识库检索问题。"
                    "最多给出4个候选，不得在候选中预设答案。转采、权限、材料等已确认业务规则不能被改写。"
                    "只给出专业主题并询问‘怎么处理、怎么办、处理方法’而未说明目标、阶段或事项时，"
                    "应判为歧义，并按实质不同的专业任务给出候选方向。"
                    "只返回 JSON。\n"
                    f"{registry_instruction}"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": mechanical,
                        "contextual_fallback": contextual_fallback,
                        "original_input": normalized,
                        "recent_user_questions": recent_questions,
                        "mode": mode,
                        "deterministic_plan": base_plan.to_llm_payload(),
                        "output_schema": {
                            "canonical_question": "问题明确时的专业化完整表达",
                            "intent": "主要业务意图",
                            "primary_intent": "统一问题分类",
                            "secondary_intents": ["兼有的次要分类"],
                            "target_entity": "目标业务对象",
                            "mineral": "矿种或null",
                            "business_action": "具体业务动作",
                            "completed_stage": "已经完成的阶段或null",
                            "target_outcome": "希望取得的结果或null",
                            "document_types": ["优先文件类型"],
                            "evidence_slots": ["回答必须覆盖的证据槽位"],
                            "output_shape": "输出结构",
                            "application_type": "new|renewal|change|cancellation|null",
                            "change_subtype": "expand_area|shrink_area|mineral_or_mining_method|holder_name|transfer|null",
                            "authority_relation": "权限关系或null",
                            "license_issuer_level": "unknown|ministry|province",
                            "comparison_topic": "比较主题或null",
                            "comparison_scope": "representative|exhaustive|null",
                            "is_ambiguous": False,
                            "confidence": 0.0,
                            "missing_slots": ["缺少且会改变结论的条件"],
                            "reason": "需要或不需要确认的简短原因",
                            "interpretations": [
                                {
                                    "label": "候选方向短名称",
                                    "question": "可直接检索的完整问题",
                                    "description": "该方向关注的内容",
                                    "slot_updates": {"待确认槽位": "选择后的值"},
                                }
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            raw = await self.llm.complete_json(
                messages,
                max_tokens=self.settings.question_resolution_max_tokens,
            )
        except (json.JSONDecodeError, TypeError, ValueError, OSError) as error:
            return QuestionResolution(
                canonical_question=contextual_fallback,
                plan=base_plan,
                model_used=True,
                clarification=schema_fallback,
                error=type(error).__name__,
            )
        except Exception as error:
            return QuestionResolution(
                canonical_question=contextual_fallback,
                plan=base_plan,
                model_used=True,
                clarification=schema_fallback,
                error=type(error).__name__,
            )
        try:
            payload = ResolutionPayload.model_validate_json(raw)
        except ValidationError as error:
            payload = self._recover_resolution_payload(raw, contextual_fallback)
            if payload is None:
                return QuestionResolution(
                    canonical_question=contextual_fallback,
                    plan=base_plan,
                    model_used=True,
                    clarification=schema_fallback,
                    error=type(error).__name__,
                )

        canonical = self._validated_question(
            mechanical,
            base_plan,
            payload.canonical_question,
        )
        canonical = self._apply_conversation_guard(
            mechanical,
            canonical,
            recent_questions,
        )
        canonical_plan = self._apply_model_intent(
            base_plan,
            understand_query(canonical),
            payload,
        )
        canonical_plan = self._apply_model_classification(canonical_plan, payload)
        options = self._options(payload.interpretations, canonical_plan)
        schema_clarification = self._schema_clarification(
            mechanical,
            canonical_plan,
            canonical,
            options,
        )
        if schema_clarification is not None:
            return QuestionResolution(
                canonical_question=canonical,
                plan=canonical_plan,
                model_used=True,
                clarification=schema_clarification,
            )
        complete_material_classification = bool(
            canonical_plan.classification
            and canonical_plan.classification.primary_intent == "service_materials"
            and not canonical_plan.classification.missing_slots
        )
        if (
            payload.is_ambiguous
            and payload.confidence >= self.settings.question_resolution_min_confidence
            and len(options) >= 2
            and not complete_material_classification
        ):
            interpreted = canonical if canonical != normalized else normalized
            reason = normalize_user_query(payload.reason)[:300] or "当前问题存在多个会影响检索范围的专业方向。"
            clarification = Clarification(
                pending_slot=(payload.missing_slots[0] if payload.missing_slots else None),
                resolved_slots=(
                    canonical_plan.classification.resolved_slots
                    if canonical_plan.classification
                    else {}
                ),
                interpreted_question=interpreted,
                reason=reason,
                options=options,
                allow_free_text=True,
            )
            return QuestionResolution(
                canonical_question=canonical,
                plan=canonical_plan,
                model_used=True,
                clarification=clarification,
            )
        return QuestionResolution(
            canonical_question=canonical,
            plan=canonical_plan,
            model_used=True,
        )

    @staticmethod
    def _recover_resolution_payload(
        raw: str,
        fallback_question: str,
    ) -> ResolutionPayload | None:
        """Keep a usable intent/slot decision when optional LLM fields are malformed."""
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        interpretations: list[dict[str, object]] = []
        for item in value.get("interpretations") or []:
            if not isinstance(item, dict):
                continue
            question = normalize_user_query(str(item.get("question") or ""))[:300]
            label = re.sub(r"\s+", " ", str(item.get("label") or "")).strip()[:40]
            if len(question) < 2 or not label:
                continue
            interpretations.append(
                {
                    "label": label,
                    "question": question,
                    "description": str(item.get("description") or "")[:100],
                    "slot_updates": item.get("slot_updates")
                    if isinstance(item.get("slot_updates"), dict)
                    else {},
                }
            )
            if len(interpretations) >= 4:
                break
        sanitized = {
            "canonical_question": normalize_user_query(
                str(value.get("canonical_question") or fallback_question)
            )[:600]
            or fallback_question,
            "intent": str(value.get("intent") or "general")[:80],
            "is_ambiguous": bool(value.get("is_ambiguous")),
            "confidence": value.get("confidence") if isinstance(value.get("confidence"), (int, float)) else 0.0,
            "missing_slots": [
                str(item)[:80]
                for item in (value.get("missing_slots") or [])
                if str(item).strip()
            ][:8],
            "reason": str(value.get("reason") or "")[:300],
            "interpretations": interpretations,
            "primary_intent": str(value.get("primary_intent") or "")[:80],
            "secondary_intents": [
                str(item)[:80]
                for item in (value.get("secondary_intents") or [])
                if str(item).strip()
            ][:4],
            "target_entity": str(value.get("target_entity") or "")[:160] or None,
            "mineral": str(value.get("mineral") or "")[:40] or None,
            "business_action": str(value.get("business_action") or "")[:160] or None,
            "completed_stage": str(value.get("completed_stage") or "")[:160] or None,
            "target_outcome": str(value.get("target_outcome") or "")[:160] or None,
            "document_types": [str(item)[:80] for item in (value.get("document_types") or [])][:12],
            "evidence_slots": [str(item)[:120] for item in (value.get("evidence_slots") or [])][:12],
            "output_shape": str(value.get("output_shape") or "")[:80],
            "ambiguities": [str(item)[:120] for item in (value.get("ambiguities") or [])][:8],
            "application_type": str(value.get("application_type") or "")[:40] or None,
            "change_subtype": str(value.get("change_subtype") or "")[:60] or None,
            "authority_relation": str(value.get("authority_relation") or "")[:60] or None,
            "license_issuer_level": str(value.get("license_issuer_level") or "unknown")[:20],
            "comparison_topic": str(value.get("comparison_topic") or "")[:160] or None,
            "comparison_scope": str(value.get("comparison_scope") or "")[:80] or None,
        }
        try:
            return ResolutionPayload.model_validate(sanitized)
        except ValidationError:
            return None

    @staticmethod
    def _inherit_plan(
        inherited: QueryPlan | None,
        current: QueryPlan,
        slot_updates: dict[str, str] | None,
    ) -> QueryPlan:
        if inherited is None:
            if slot_updates and current.classification:
                return replace(
                    current,
                    classification=merge_slot_updates(current.classification, slot_updates),
                )
            return current

        parent_classification = inherited.classification or build_classification(
            inherited.normalized_query,
            inherited.intent,
            document_types=inherited.document_types,
            license_issuer_level=inherited.license_issuer_level,
        )
        merged = merge_slot_updates(
            parent_classification,
            current.classification.resolved_slots if current.classification else {},
        )
        merged = merge_slot_updates(merged, slot_updates)
        retrieval_query = " ".join(
            dict.fromkeys(
                part
                for part in (
                    current.retrieval_query,
                    inherited.normalized_query,
                    merged.target_entity,
                    merged.business_action,
                )
                if part
            )
        )
        return replace(
            current,
            intent=legacy_intent_for_primary(merged.primary_intent, inherited.intent),
            retrieval_query=retrieval_query,
            candidate_title_terms=tuple(
                dict.fromkeys((*inherited.candidate_title_terms, *current.candidate_title_terms))
            ),
            standard_numbers=tuple(
                dict.fromkeys((*inherited.standard_numbers, *current.standard_numbers))
            ),
            document_types=merged.document_types or current.document_types,
            target_exploration_type=(
                inherited.target_exploration_type or current.target_exploration_type
            ),
            license_issuer_level=(
                merged.license_issuer_level
                if merged.license_issuer_level in {"ministry", "province"}
                else current.license_issuer_level
            ),
            classification=merged,
        )

    @staticmethod
    def _needs_model(question: str, plan: QueryPlan) -> bool:
        if plan.intent == "general":
            return True
        if plan.intent == "engineering_distance_lookup":
            return not plan.target_exploration_type or not plan.candidate_title_terms
        if plan.intent == "service_materials":
            return True
        if plan.intent == "authority_responsibility":
            return plan.authority_role_ambiguous or plan.license_issuer_level == "unknown"
        return plan.intent in {
            "license_reference",
            "service_procedure_basis",
            "projection_rule",
            "related_documents",
            "regulation_lookup",
        }

    def _apply_model_intent(
        self,
        base_plan: QueryPlan,
        canonical_plan: QueryPlan,
        payload: ResolutionPayload,
    ) -> QueryPlan:
        suggested = payload.intent.strip()
        if (
            payload.is_ambiguous
            or payload.confidence < self.settings.question_resolution_min_confidence
            or suggested not in PROTECTED_QUERY_INTENTS
        ):
            return canonical_plan
        if base_plan.intent in PROTECTED_QUERY_INTENTS and suggested != base_plan.intent:
            return canonical_plan
        if canonical_plan.intent in PROTECTED_QUERY_INTENTS:
            return canonical_plan
        return replace(
            canonical_plan,
            intent=suggested,
            document_types=default_document_types(suggested),
            required_evidence_groups=default_evidence_groups(suggested),
            planner_confidence=payload.confidence,
        )

    def _apply_model_classification(
        self,
        plan: QueryPlan,
        payload: ResolutionPayload,
    ) -> QueryPlan:
        fallback = plan.classification or build_classification(
            plan.normalized_query,
            plan.intent,
            document_types=plan.document_types,
            license_issuer_level=plan.license_issuer_level,
        )
        model_primary = payload.primary_intent.strip()
        if model_primary not in PRIMARY_INTENTS:
            model_primary = legacy_primary_intent(payload.intent)
        if (
            fallback.primary_intent == "service_workflow"
            and model_primary == "service_materials"
            and fallback.completed_stage
            and fallback.target_outcome
        ):
            model_primary = "service_workflow"
        if (
            payload.confidence < self.settings.question_resolution_min_confidence
            or (
                plan.intent in PROTECTED_QUERY_INTENTS
                and model_primary not in {fallback.primary_intent, *fallback.secondary_intents}
            )
        ):
            model_primary = fallback.primary_intent

        model_payload = payload.model_dump(mode="json")
        model_payload["primary_intent"] = model_primary
        classification = classification_from_payload(model_payload, fallback)
        classification = replace(
            classification,
            application_type=(
                fallback.application_type
                if fallback.application_type is not None
                else None
            ),
            change_subtype=(
                fallback.change_subtype
                if fallback.change_subtype is not None
                else None
            ),
            license_issuer_level=(
                fallback.license_issuer_level
                if fallback.license_issuer_level in {"ministry", "province"}
                else "unknown"
            ),
        )
        classification = merge_slot_updates(classification, fallback.resolved_slots)
        strategy = strategy_for(classification.primary_intent)
        retrieval_query = " ".join(
            dict.fromkeys(
                part
                for part in (
                    plan.retrieval_query,
                    classification.target_entity,
                    classification.business_action,
                    classification.completed_stage,
                    classification.target_outcome,
                )
                if part
            )
        )
        legacy_intent = legacy_intent_for_primary(classification.primary_intent, plan.intent)
        if (
            plan.intent == "service_materials"
            and classification.primary_intent == "service_workflow"
        ):
            # The legacy service-material renderer contains the controlled
            # post-filing action conversion; classification still declares the
            # user-facing output as a workflow.
            legacy_intent = "service_materials"
        return replace(
            plan,
            intent=legacy_intent,
            retrieval_query=retrieval_query,
            document_types=classification.document_types or plan.document_types,
            search_mode=(
                plan.search_mode
                if plan.search_mode in {"comparison", "exhaustive"}
                else strategy.search_mode
            ),
            output_mode=(
                "table"
                if classification.output_shape in {"structured_table", "comparison_matrix"}
                else plan.output_mode
            ),
            license_issuer_level=(
                classification.license_issuer_level
                if classification.license_issuer_level in {"ministry", "province"}
                else plan.license_issuer_level
            ),
            classification=classification,
            planner_confidence=max(plan.planner_confidence, payload.confidence),
        )

    def _options(
        self,
        values: list[ResolutionOptionPayload],
        base_plan: QueryPlan,
    ) -> list[ClarificationOption]:
        options: list[ClarificationOption] = []
        seen_questions: set[str] = set()
        for value in values[:4]:
            question = normalize_user_query(value.question)[:300]
            label = re.sub(r"\s+", " ", value.label).strip()[:40]
            if (
                not question
                or not label
                or question in seen_questions
                or not self.domain_gate.check(question).in_scope
                or not self._preserves_protected_constraints(
                    base_plan,
                    question,
                    require_missing_slot_resolution=True,
                )
            ):
                continue
            seen_questions.add(question)
            options.append(
                ClarificationOption(
                    option_id=f"option_{len(options) + 1}",
                    label=label,
                    question=question,
                    description=re.sub(r"\s+", " ", value.description).strip()[:100] or None,
                    slot_updates={
                        str(key)[:60]: str(item)[:120]
                        for key, item in value.slot_updates.items()
                        if str(key).strip() and str(item).strip()
                    },
                )
            )
        return options

    @staticmethod
    def _schema_clarification(
        original: str,
        base_plan: QueryPlan,
        canonical: str,
        model_options: list[ClarificationOption],
    ) -> Clarification | None:
        classification = base_plan.classification
        if (
            classification
            and classification.primary_intent == "service_materials"
            and "application_type" in classification.missing_slots
        ):
            return Clarification(
                pending_slot="application_type",
                resolved_slots=classification.resolved_slots,
                interpreted_question=canonical or original,
                reason=(
                    "采矿许可证申请资料按办理类型分别规定；新立、延续、变更和注销的材料清单不同。"
                ),
                options=[
                    ClarificationOption(
                        option_id="option_1",
                        label="新立申请",
                        question="采矿权新立申请需要提交哪些材料和要件？",
                        description="首次申请或探矿权转采矿权等新立情形。",
                        slot_updates={"application_type": "new"},
                    ),
                    ClarificationOption(
                        option_id="option_2",
                        label="延续申请",
                        question="采矿权延续申请需要提交哪些材料和要件？",
                        description="现有采矿许可证到期前申请延续。",
                        slot_updates={"application_type": "renewal"},
                    ),
                    ClarificationOption(
                        option_id="option_3",
                        label="变更申请",
                        question="采矿权变更申请需要提交哪些材料和要件？",
                        description="包括矿区范围、矿种或开采方式、名称及转让等变更。",
                        slot_updates={"application_type": "change"},
                    ),
                    ClarificationOption(
                        option_id="option_4",
                        label="注销申请",
                        question="采矿权注销申请需要提交哪些材料和要件？",
                        description="申请注销现有采矿许可证。",
                        slot_updates={"application_type": "cancellation"},
                    ),
                ],
                allow_free_text=True,
            )
        if (
            classification
            and classification.primary_intent == "service_materials"
            and classification.application_type == "change"
            and "change_subtype" in classification.missing_slots
        ):
            return Clarification(
                pending_slot="change_subtype",
                resolved_slots=classification.resolved_slots,
                interpreted_question=canonical or original,
                reason="采矿权变更包含五类材料清单不同的具体事项，需要先确定本次变更类型。",
                options=[
                    ClarificationOption(
                        option_id="change_expand_area",
                        label="扩大矿区范围",
                        question="采矿权变更申请中，扩大矿区范围需要提交哪些材料和要件？",
                        description="增加采矿许可证载明的矿区范围。",
                        slot_updates={"application_type": "change", "change_subtype": "expand_area"},
                    ),
                    ClarificationOption(
                        option_id="change_shrink_area",
                        label="缩小矿区范围",
                        question="采矿权变更申请中，缩小矿区范围需要提交哪些材料和要件？",
                        description="减少采矿许可证载明的矿区范围。",
                        slot_updates={"application_type": "change", "change_subtype": "shrink_area"},
                    ),
                    ClarificationOption(
                        option_id="change_mineral_method",
                        label="矿种或开采方式",
                        question="采矿权变更申请中，变更开采主矿种或开采方式需要提交哪些材料和要件？",
                        description="变更开采主矿种、开采方式或二者之一。",
                        slot_updates={"application_type": "change", "change_subtype": "mineral_or_mining_method"},
                    ),
                    ClarificationOption(
                        option_id="change_holder_name",
                        label="采矿权人名称",
                        question="采矿权变更申请中，变更采矿权人名称需要提交哪些材料和要件？",
                        description="主体未发生转让，仅变更采矿权人名称。",
                        slot_updates={"application_type": "change", "change_subtype": "holder_name"},
                    ),
                    ClarificationOption(
                        option_id="change_transfer",
                        label="采矿权转让",
                        question="采矿权转让变更需要提交哪些材料和要件？",
                        description="采矿权转让并办理权利人变更。",
                        slot_updates={"application_type": "change", "change_subtype": "transfer"},
                    ),
                ],
                allow_free_text=True,
            )
        if (
            base_plan.intent == "authority_responsibility"
            and base_plan.license_issuer_level == "unknown"
        ):
            options = model_options if len(model_options) >= 2 else [
                ClarificationOption(
                    option_id="option_1",
                    label="自然资源部颁发",
                    question=(
                        "我的勘查许可证或采矿许可证由自然资源部颁发，"
                        "矿产资源储量评审备案应向哪个机构申请？"
                    ),
                    description="按自然资源部本级颁发许可证的情形核验。",
                    slot_updates={"license_issuer_level": "ministry"},
                ),
                ClarificationOption(
                    option_id="option_2",
                    label="省级部门颁发",
                    question=(
                        "我的勘查许可证或采矿许可证由省级自然资源主管部门颁发，"
                        "矿产资源储量评审备案应向哪个机构申请？"
                    ),
                    description="按省级自然资源主管部门颁发许可证的情形核验。",
                    slot_updates={"license_issuer_level": "province"},
                ),
            ]
            return Clarification(
                pending_slot="license_issuer_level",
                resolved_slots=(classification.resolved_slots if classification else {}),
                interpreted_question=canonical or original,
                reason="当前缺少许可证颁发机关；该条件会直接改变评审备案权限结论。",
                options=options[:4],
                allow_free_text=True,
            )
        if QuestionResolver._is_broad_goaf_question(original):
            options = model_options if len(model_options) >= 2 else [
                ClarificationOption(
                    option_id="option_1",
                    label="稳定性评价",
                    question="采空区稳定性评价应依据哪些标准和条款？",
                    description="关注顶板、矿柱、岩体结构和邻近工程影响。",
                ),
                ClarificationOption(
                    option_id="option_2",
                    label="积水与水害",
                    question="采空区积水调查、评价与水害防治应依据哪些标准和条款？",
                    description="关注积水分布、水量、水质和突水风险。",
                ),
                ClarificationOption(
                    option_id="option_3",
                    label="塌陷监测",
                    question="采空区塌陷监测与防治应依据哪些标准和条款？",
                    description="关注地表沉陷、变形监测和防治要求。",
                ),
                ClarificationOption(
                    option_id="option_4",
                    label="工程治理",
                    question="采空区充填、封闭或其他工程治理应依据哪些标准和条款？",
                    description="关注具体工程处置方案及其适用条件。",
                ),
            ]
            return Clarification(
                pending_slot="technical_goal",
                resolved_slots=(classification.resolved_slots if classification else {}),
                interpreted_question=canonical or original,
                reason="“处理”可能指稳定性、积水水害、塌陷监测或工程治理，不同目标对应不同标准和条款。",
                options=options[:4],
                allow_free_text=True,
            )
        return None

    @staticmethod
    def _minimal_domain_corrections(question: str) -> str:
        corrected = question
        for source, target in (
            ("采矿正", "采矿证"),
            ("采矿症", "采矿证"),
            ("采矿政", "采矿证"),
        ):
            corrected = corrected.replace(source, target)
        return corrected

    @staticmethod
    def _is_generic_mining_right_material_question(
        question: str,
        plan: QueryPlan,
    ) -> bool:
        return bool(
            plan.intent == "service_materials"
            and any(term in question for term in MINING_RIGHT_LICENSE_TERMS)
            and not any(term in question for term in MINING_RIGHT_APPLICATION_TERMS)
            and not is_post_filing_license_steps_query(question)
        )

    @classmethod
    def _apply_conversation_guard(
        cls,
        current: str,
        canonical: str,
        recent_questions: tuple[str, ...],
    ) -> str:
        current_compact = re.sub(r"\s+", "", current)
        correction = (
            any(term in current_compact for term in ("无关", "不是", "不要讨论", "说的不是"))
            and any(term in current_compact for term in MINING_RIGHT_LICENSE_TERMS)
            and "评审备案" in current_compact
        )
        issuer_reply = (
            any(term in current_compact for term in ("不知道哪个机关发证", "不知道谁发证", "不清楚发证机关"))
            and any("采矿" in item for item in recent_questions)
        )
        if not correction and not issuer_reply:
            return canonical
        for previous in reversed(recent_questions):
            candidate = cls._minimal_domain_corrections(previous)
            candidate_plan = understand_query(candidate)
            if candidate_plan.intent == "service_materials" or (
                any(term in candidate for term in MINING_RIGHT_LICENSE_TERMS)
                and any(term in candidate for term in MINING_RIGHT_MATERIAL_TERMS)
            ):
                return candidate
        if correction:
            return "采矿许可证办理需要什么要件"
        return canonical

    @staticmethod
    def _is_broad_goaf_question(question: str) -> bool:
        compact = re.sub(r"\s+", "", question or "")
        return bool(
            any(term in compact for term in ("采空区", "采空场", "老空区", "老窑采空区"))
            and any(marker in compact for marker in BROAD_ACTION_MARKERS)
            and not any(goal in compact for goal in GOAF_SPECIFIC_GOALS)
        )

    def _validated_question(
        self,
        original: str,
        base_plan: QueryPlan,
        candidate: str,
    ) -> str:
        canonical = normalize_user_query(candidate)[:600] or original
        if not self.domain_gate.check(canonical).in_scope:
            return original
        if not self._preserves_protected_constraints(base_plan, canonical):
            return original
        explicit_titles = tuple(
            title.strip()
            for title in re.findall(r"《([^》]{2,80})》", original)
            if title.strip()
        )
        if any(title not in canonical for title in explicit_titles):
            return original
        return canonical

    @staticmethod
    def _preserves_protected_constraints(
        base_plan: QueryPlan,
        candidate: str,
        *,
        require_missing_slot_resolution: bool = False,
    ) -> bool:
        candidate_plan = understand_query(candidate)
        if (
            base_plan.scope_origin == "user"
            and base_plan.standard_numbers
            and not set(base_plan.standard_numbers).issubset(candidate_plan.standard_numbers)
        ):
            return False
        if base_plan.target_exploration_type and (
            candidate_plan.target_exploration_type != base_plan.target_exploration_type
        ):
            return False
        for field in (
            "license_issuer_level",
            "mining_right_granting_level",
            "filing_authority",
        ):
            expected = getattr(base_plan, field)
            actual = getattr(candidate_plan, field)
            if expected != "unknown" and actual != expected:
                return False
        if (
            base_plan.intent in PROTECTED_QUERY_INTENTS
            and candidate_plan.intent != base_plan.intent
        ):
            return False
        if require_missing_slot_resolution:
            if (
                base_plan.intent == "authority_responsibility"
                and base_plan.license_issuer_level == "unknown"
                and candidate_plan.license_issuer_level == "unknown"
            ):
                return False
            if (
                base_plan.intent == "engineering_distance_lookup"
                and not base_plan.target_exploration_type
                and not candidate_plan.target_exploration_type
            ):
                return False
        return True


def clarification_answer(clarification: Clarification) -> str:
    return "\n".join(
        [
            f"我目前理解为：{clarification.interpreted_question}",
            "",
            clarification.reason,
            "",
            "请选择更接近你实际需求的方向，或直接补充说明。确认后再进入知识库检索。",
        ]
    )
