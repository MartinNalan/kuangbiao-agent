from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .config import Settings
from .domain_gate import DomainGate
from .llm_client import LLMClient
from .query_understanding import (
    PROTECTED_QUERY_INTENTS,
    QueryPlan,
    normalize_user_query,
    understand_query,
)
from .schemas import Clarification, ClarificationOption


SPECIFIC_APPLICATION_TERMS = (
    "新立",
    "延续",
    "变更",
    "注销",
    "转让",
    "扩大矿区范围",
    "缩小矿区范围",
)


class ResolutionOptionPayload(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    question: str = Field(min_length=2, max_length=300)
    description: str = Field(default="", max_length=100)


class ResolutionPayload(BaseModel):
    canonical_question: str = Field(min_length=1, max_length=600)
    intent: str = Field(default="general", max_length=80)
    is_ambiguous: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    missing_slots: list[str] = Field(default_factory=list, max_length=8)
    reason: str = Field(default="", max_length=300)
    interpretations: list[ResolutionOptionPayload] = Field(default_factory=list, max_length=4)


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

    async def resolve(self, question: str, *, mode: str = "basic") -> QuestionResolution:
        normalized = normalize_user_query(question)
        base_plan = understand_query(normalized)
        fallback = QuestionResolution(canonical_question=normalized, plan=base_plan)
        if not normalized or not self.settings.question_resolution_enabled:
            return fallback
        schema_fallback = self._schema_clarification(
            normalized,
            base_plan,
            normalized,
            [],
        )
        if not self.llm.enabled or not self._needs_model(normalized, base_plan):
            if schema_fallback is not None:
                return QuestionResolution(
                    canonical_question=normalized,
                    plan=base_plan,
                    clarification=schema_fallback,
                )
            return fallback

        messages = [
            {
                "role": "system",
                "content": (
                    "你是 geowiki 的问题理解与歧义判断器，只理解问题，不回答问题，不检索标准，"
                    "不得使用模型记忆生成专业结论。判断歧义时关注：不同解释是否会改变目标标准、"
                    "条款范围、业务事项或最终结论。只有存在两个以上实质不同且合理的专业方向，"
                    "或缺少决定结论的关键条件时，才要求确认；不要对表达清楚的问题过度追问。"
                    "矿产资源储量评审备案机构取决于许可证颁发机关；用户只给矿种或矿山规模而未说明"
                    "许可证由自然资源部还是省级自然资源主管部门颁发时，必须判为歧义并给出对应候选。"
                    "候选解释必须完整、互斥、仍属于地质矿产领域，并可直接作为后续知识库检索问题。"
                    "最多给出4个候选，不得在候选中预设答案。转采、权限、材料等已确认业务规则不能被改写。"
                    "只返回 JSON。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": normalized,
                        "mode": mode,
                        "deterministic_plan": base_plan.to_llm_payload(),
                        "output_schema": {
                            "canonical_question": "问题明确时的专业化完整表达",
                            "intent": "主要业务意图",
                            "is_ambiguous": False,
                            "confidence": 0.0,
                            "missing_slots": ["缺少且会改变结论的条件"],
                            "reason": "需要或不需要确认的简短原因",
                            "interpretations": [
                                {
                                    "label": "候选方向短名称",
                                    "question": "可直接检索的完整问题",
                                    "description": "该方向关注的内容",
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
            payload = ResolutionPayload.model_validate_json(raw)
        except (ValidationError, json.JSONDecodeError, TypeError, ValueError, OSError) as error:
            return QuestionResolution(
                canonical_question=normalized,
                plan=base_plan,
                model_used=True,
                clarification=schema_fallback,
                error=type(error).__name__,
            )
        except Exception as error:
            return QuestionResolution(
                canonical_question=normalized,
                plan=base_plan,
                model_used=True,
                clarification=schema_fallback,
                error=type(error).__name__,
            )

        canonical = self._validated_question(
            normalized,
            base_plan,
            payload.canonical_question,
        )
        options = self._options(payload.interpretations, base_plan)
        if (
            payload.is_ambiguous
            and payload.confidence >= self.settings.question_resolution_min_confidence
            and len(options) >= 2
        ):
            interpreted = canonical if canonical != normalized else normalized
            reason = normalize_user_query(payload.reason)[:300] or "当前问题存在多个会影响检索范围的专业方向。"
            clarification = Clarification(
                interpreted_question=interpreted,
                reason=reason,
                options=options,
                allow_free_text=True,
            )
            return QuestionResolution(
                canonical_question=canonical,
                plan=base_plan,
                model_used=True,
                clarification=clarification,
            )
        schema_clarification = self._schema_clarification(
            normalized,
            base_plan,
            canonical,
            options,
        )
        if schema_clarification is not None:
            return QuestionResolution(
                canonical_question=canonical,
                plan=base_plan,
                model_used=True,
                clarification=schema_clarification,
            )
        return QuestionResolution(
            canonical_question=canonical,
            plan=understand_query(canonical),
            model_used=True,
        )

    @staticmethod
    def _needs_model(question: str, plan: QueryPlan) -> bool:
        if plan.intent == "general":
            return True
        if plan.intent == "engineering_distance_lookup":
            return not plan.target_exploration_type or not plan.candidate_title_terms
        if plan.intent == "service_materials":
            return not any(term in question for term in SPECIFIC_APPLICATION_TERMS)
        if plan.intent == "authority_responsibility":
            return plan.authority_role_ambiguous or plan.license_issuer_level == "unknown"
        return plan.intent in {"projection_rule", "related_documents", "regulation_lookup"}

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
        if (
            base_plan.intent != "authority_responsibility"
            or base_plan.license_issuer_level != "unknown"
        ):
            return None
        options = model_options if len(model_options) >= 2 else [
            ClarificationOption(
                option_id="option_1",
                label="自然资源部颁发",
                question=(
                    "我的勘查许可证或采矿许可证由自然资源部颁发，"
                    "矿产资源储量评审备案应向哪个机构申请？"
                ),
                description="按自然资源部本级颁发许可证的情形核验。",
            ),
            ClarificationOption(
                option_id="option_2",
                label="省级部门颁发",
                question=(
                    "我的勘查许可证或采矿许可证由省级自然资源主管部门颁发，"
                    "矿产资源储量评审备案应向哪个机构申请？"
                ),
                description="按省级自然资源主管部门颁发许可证的情形核验。",
            ),
        ]
        return Clarification(
            interpreted_question=canonical or original,
            reason="当前缺少许可证颁发机关；该条件会直接改变评审备案权限结论。",
            options=options[:4],
            allow_free_text=True,
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
