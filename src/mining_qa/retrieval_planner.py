from __future__ import annotations

import json
from dataclasses import dataclass
from time import perf_counter
from typing import Literal, get_args

from .config import Settings
from .llm_client import LLMClient
from .query_understanding import PROTECTED_QUERY_INTENTS, QueryPlan, apply_semantic_plan


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
]

SearchMode = Literal["default", "scoped", "comparison", "exhaustive", "catalog"]

ALLOWED_DOCUMENT_TYPES = {
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

MODEL_PLANNING_INTENTS = {
    "general",
    "projection_rule",
    "projection_comparison",
    "related_documents",
}

PLANNER_BYPASS_INTENTS = PROTECTED_QUERY_INTENTS | {"projection_comparison"}


@dataclass(frozen=True)
class PlannerResult:
    plan: QueryPlan
    used: bool
    elapsed_ms: float
    error: str | None = None


class RetrievalPlanner:
    def __init__(self, settings: Settings, llm: LLMClient | None = None):
        self.settings = settings
        self.llm = llm or LLMClient(settings)

    async def plan(self, question: str, base_plan: QueryPlan) -> PlannerResult:
        started = perf_counter()
        if (
            not self.settings.query_planner_enabled
            or not self.llm.enabled
            or base_plan.intent in PLANNER_BYPASS_INTENTS
            or (base_plan.intent not in MODEL_PLANNING_INTENTS and not base_plan.exhaustive_search)
        ):
            return PlannerResult(
                plan=apply_semantic_plan(base_plan, None),
                used=False,
                elapsed_ms=(perf_counter() - started) * 1000,
            )

        messages = [
            {
                "role": "system",
                "content": (
                    "你是 geowiki 的地质矿产知识库检索规划器，只理解问题和制定检索计划，不回答问题。"
                    "你的输出将用于搜索本地权威标准、政策、办事指南和条款，不是互联网搜索。"
                    "必须保留矿种、标准号、文号、条款号、数值、比例、勘查阶段、业务事项、责任主体和限定条件。"
                    "将口语表达转换为专业检索概念，例如转采应理解为探矿权转采矿权，"
                    "但不得把模型记忆中的结论当作证据。"
                    "比较问题必须明确比较对象、比较维度，以及每条证据必须同时出现的事实组。"
                    "如果用户问哪些制度允许详查报告用于探矿权转采矿权，应识别为 exploration_to_mining_eligibility，"
                    "检索政策中的转采条件、勘查程度要求和技术标准中的报告类型限制；"
                    "必须区分行政上的探矿权转采矿权与技术上的矿山设计开采依据。"
                    "行政申请语境中的‘要件、必备资料、所需资料’应理解为申请材料；"
                    "政策正文引用附件清单时，document_types 必须包含 policy_attachment，不能只检索父政策正文。"
                    "required_evidence_groups 是 AND 关系，每个子数组内部是 OR 关系。"
                    "普通工程间距表不能作为矿体外推规则证据；仅出现同一个词但没有目标关系的内容应放入 negative_terms。"
                    "candidate_titles 和 standard_numbers 只有在问题明确给出或你高度确信时填写。"
                    "document_types 只能从 standard、national_standard、industry_standard、policy_document、"
                    "policy_attachment、law、regulation、department_rule、guidance、service_guide、"
                    "administrative_service_guide、amendment 中选择。"
                    "只返回符合给定结构的 JSON。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question,
                        "deterministic_plan": {
                            "normalized_query": base_plan.normalized_query,
                            "intent": base_plan.intent,
                            "target_exploration_type": base_plan.target_exploration_type,
                            "candidate_titles": base_plan.candidate_title_terms,
                            "standard_numbers": base_plan.standard_numbers,
                        },
                        "output_schema": {
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
                            "confidence": 0.0,
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            raw = await self.llm.complete_json(
                messages,
                max_tokens=self.settings.query_planner_max_tokens,
            )
            payload = json.loads(raw)
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
            raw_document_types = payload.get("document_types")
            if not isinstance(raw_document_types, list):
                raw_document_types = []
            payload["document_types"] = [
                str(value) for value in raw_document_types if str(value) in ALLOWED_DOCUMENT_TYPES
            ]
            plan = apply_semantic_plan(base_plan, payload)
            return PlannerResult(
                plan=plan,
                used=True,
                elapsed_ms=(perf_counter() - started) * 1000,
            )
        except (json.JSONDecodeError, TypeError, ValueError, OSError) as error:
            return PlannerResult(
                plan=apply_semantic_plan(base_plan, None),
                used=False,
                elapsed_ms=(perf_counter() - started) * 1000,
                error=type(error).__name__,
            )
        except Exception as error:
            return PlannerResult(
                plan=apply_semantic_plan(base_plan, None),
                used=False,
                elapsed_ms=(perf_counter() - started) * 1000,
                error=type(error).__name__,
            )
