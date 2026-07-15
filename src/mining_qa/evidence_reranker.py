from __future__ import annotations

import json
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .config import Settings
from .llm_client import LLMClient
from .query_understanding import QueryPlan


COMPLEX_INTENTS = {
    "projection_comparison",
    "clause_comparison",
    "regulation_lookup",
    "related_documents",
    "technical_requirement_sufficiency",
}

DETERMINISTIC_EVIDENCE_INTENTS = {
    "exploration_to_mining_eligibility",
    "companion_resource_type",
    "exploration_type_factors",
    "basic_analysis_items",
}

STRUCTURAL_GUARD_INTENTS = {
    "projection_comparison",
    "exploration_to_mining_eligibility",
}


class EvidenceFact(BaseModel):
    source_index: int = Field(ge=1)
    statement: str = Field(min_length=1, max_length=500)
    dimension: str = Field(default="", max_length=100)
    condition: str = Field(default="", max_length=300)


class EvidenceDecision(BaseModel):
    selected_indices: list[int] = Field(default_factory=list, max_length=10)
    direct_evidence_indices: list[int] = Field(default_factory=list, max_length=10)
    sufficient: bool = False
    missing_evidence_groups: list[str] = Field(default_factory=list, max_length=8)
    refined_query: str = Field(default="", max_length=500)
    refined_terms: list[str] = Field(default_factory=list, max_length=16)
    facts: list[EvidenceFact] = Field(default_factory=list, max_length=12)
    grounded_answer: str = Field(default="", max_length=8000)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


@dataclass(frozen=True)
class RerankResult:
    hits: tuple[dict[str, Any], ...]
    sufficient: bool
    used: bool
    elapsed_ms: float
    direct_evidence_count: int
    refined_query: str = ""
    refined_terms: tuple[str, ...] = ()
    missing_evidence_groups: tuple[str, ...] = ()
    facts: tuple[dict[str, Any], ...] = ()
    grounded_answer: str = ""
    confidence: float = 0.0
    error: str | None = None


class EvidenceReranker:
    def __init__(self, settings: Settings, llm: LLMClient | None = None):
        self.settings = settings
        self.llm = llm or LLMClient(settings)

    @staticmethod
    def needs_model(plan: QueryPlan) -> bool:
        if plan.intent in DETERMINISTIC_EVIDENCE_INTENTS:
            return False
        return (
            plan.intent in COMPLEX_INTENTS
            or plan.search_mode in {"comparison", "exhaustive"}
            or plan.exhaustive_search
            or bool(plan.comparison_dimensions)
        )

    async def judge(
        self,
        question: str,
        plan: QueryPlan,
        hits: list[dict[str, Any]],
    ) -> RerankResult:
        started = perf_counter()
        candidates = hits[:10]
        fallback = self._deterministic_result(plan, candidates, started)
        if (
            not candidates
            or not self.settings.evidence_reranker_enabled
            or not self.llm.enabled
            or not self.needs_model(plan)
        ):
            return fallback

        compact_candidates = []
        for index, hit in enumerate(candidates, start=1):
            compact_candidates.append(
                {
                    "index": index,
                    "document_id": hit.get("document_id"),
                    "title": hit.get("title"),
                    "standard_no": hit.get("standard_no"),
                    "clause": hit.get("clause_no") or hit.get("section_path"),
                    "document_type": hit.get("document_type"),
                    "retrieval_routes": hit.get("hit_type") or [],
                    "text": str(hit.get("evidence_text") or hit.get("quote") or "")[:450],
                }
            )

        messages = [
            {
                "role": "system",
                "content": (
                    "你是 geowiki 的证据审查器，不回答用户问题，只审查本地知识库候选条款。"
                    "判断候选是否直接表达用户询问的关系，不能因为矿种、工程间距或外推等单个词相同就判为相关。"
                    "比较问题必须至少选出两个不同文件的直接证据，并提取各文件在同一比较维度上的具体规定。"
                    "普通勘查工程间距表不是矿体外推依据的证据，除非条款明确说明外推及其距离基准或比例。"
                    "探矿权转采矿权问题中，‘可作为矿山设计开采依据’、‘供矿山设计开采’等受控表述，"
                    "在本项目业务语义中属于满足条款所列条件时可以转采的正向等价证据；"
                    "必须同时保留矿种、规模、可行性研究、工业价值等适用条件。"
                    "应区分一般转采政策、分矿种特殊规定和限制可提交报告类型的技术标准。"
                    "若候选中同时存在转采条件和‘某类报告不能替代转采应提交报告’的限制证据，必须同时选中。"
                    "‘达到详查程度’是勘查程度条件，不得自动改写为任何名为‘详查报告’的文件都可转采；"
                    "应准确保留‘经评审备案的矿产资源储量报告’这一前提。"
                    "对于‘做了某项较高研究或试验，是否满足阶段最低要求’的问题，必须分别寻找："
                    "阶段最低要求条款，以及研究层级、前置基础或包含关系条款。"
                    "不得把用户没有说过的‘未开展较低级工作’补成事实；‘必要时’不等于机械的先后顺序。"
                    "只有原文明确说明包含、以前一研究为基础或不能替代时，才能判断满足或不能满足。"
                    "模型常识不能作为证据；所有结论必须能由候选原文直接推出。"
                    "若证据不足，给出一条更适合搜索本地标准库的 refined_query 和短语列表。"
                    "只负责筛选证据，不生成最终回答。最多选择4份代表性文件。"
                    "只返回 JSON。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question,
                        "retrieval_plan": plan.to_llm_payload(),
                        "candidates": compact_candidates,
                        "output_schema": {
                            "selected_indices": [1],
                            "direct_evidence_indices": [1],
                            "sufficient": False,
                            "missing_evidence_groups": ["缺失的关系或条件"],
                            "refined_query": "证据不足时使用的检索问题",
                            "refined_terms": ["检索短语"],
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
                max_tokens=self.settings.evidence_reranker_max_tokens,
            )
            decision = EvidenceDecision.model_validate_json(raw)
        except (ValidationError, json.JSONDecodeError, TypeError, ValueError, OSError) as error:
            return self._with_error(fallback, started, type(error).__name__)
        except Exception as error:
            return self._with_error(fallback, started, type(error).__name__)

        selected_indices = self._valid_indices(decision.selected_indices, len(candidates))
        direct_indices = self._valid_indices(decision.direct_evidence_indices, len(candidates))
        if plan.intent in STRUCTURAL_GUARD_INTENTS:
            structurally_direct = {
                index
                for index, hit in enumerate(candidates, start=1)
                if self._matches_all_groups(hit, plan.required_evidence_groups)
            }
            direct_indices = [index for index in direct_indices if index in structurally_direct]
            if plan.intent == "exploration_to_mining_eligibility":
                limiting_indices = [
                    index
                    for index in sorted(structurally_direct)
                    if any(
                        marker in self._candidate_text(candidates[index - 1])
                        for marker in ("不能替代", "不得替代", "不应替代")
                    )
                ]
                if limiting_indices and limiting_indices[0] not in direct_indices:
                    direct_indices.append(limiting_indices[0])
                    selected_indices.append(limiting_indices[0])
        if direct_indices:
            selected_indices = self._valid_indices(selected_indices, len(candidates))
            selected_indices = [index for index in selected_indices if index in direct_indices] or direct_indices

        selected_hits = tuple(candidates[index - 1] for index in selected_indices)
        distinct_documents = {
            str(hit.get("document_id") or f"row-{index}")
            for index, hit in enumerate(selected_hits)
        }
        comparison = plan.search_mode in {"comparison", "exhaustive"} or plan.intent in {
            "projection_comparison",
            "clause_comparison",
        }
        sufficient = bool(decision.sufficient and direct_indices and selected_hits)
        if comparison and len(distinct_documents) < 2:
            sufficient = False

        facts = tuple(
            fact.model_dump()
            for fact in decision.facts
            if fact.source_index in selected_indices
        )
        return RerankResult(
            hits=selected_hits,
            sufficient=sufficient,
            used=True,
            elapsed_ms=(perf_counter() - started) * 1000,
            direct_evidence_count=len(direct_indices),
            refined_query=decision.refined_query.strip(),
            refined_terms=tuple(self._clean_terms(decision.refined_terms)),
            missing_evidence_groups=tuple(self._clean_terms(decision.missing_evidence_groups)),
            facts=facts,
            grounded_answer="",
            confidence=decision.confidence,
        )

    def _deterministic_result(
        self,
        plan: QueryPlan,
        hits: list[dict[str, Any]],
        started: float,
    ) -> RerankResult:
        direct = [hit for hit in hits if self._matches_all_groups(hit, plan.required_evidence_groups)]
        if not plan.required_evidence_groups:
            direct = list(hits)
        selected: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        comparison = plan.search_mode in {"comparison", "exhaustive"} or plan.intent in {
            "projection_comparison",
            "clause_comparison",
        }
        for hit in direct:
            document_id = str(hit.get("document_id") or "")
            key = (
                document_id,
                "" if comparison else str(
                    hit.get("clause_no") or hit.get("section_path") or hit.get("chunk_id") or ""
                ),
            )
            if key in seen:
                continue
            seen.add(key)
            selected.append(hit)
            if len(selected) >= 4:
                break
        distinct_documents = {str(hit.get("document_id") or "") for hit in selected}
        sufficient = bool(selected) and (not comparison or len(distinct_documents) >= 2)
        return RerankResult(
            hits=tuple(selected),
            sufficient=sufficient,
            used=False,
            elapsed_ms=(perf_counter() - started) * 1000,
            direct_evidence_count=len(selected),
            refined_query=self._fallback_refined_query(plan) if not sufficient else "",
        )

    @staticmethod
    def _candidate_text(hit: dict[str, Any]) -> str:
        return " ".join(
            str(hit.get(key) or "")
            for key in (
                "title",
                "standard_no",
                "section_path",
                "clause_no",
                "evidence_text",
                "quote",
            )
        )

    @classmethod
    def _matches_all_groups(
        cls,
        hit: dict[str, Any],
        groups: tuple[tuple[str, ...], ...],
    ) -> bool:
        if not groups:
            return True
        text = cls._candidate_text(hit)
        return all(any(term and term in text for term in group) for group in groups)

    @staticmethod
    def _valid_indices(values: list[int], count: int) -> list[int]:
        return list(dict.fromkeys(value for value in values if 1 <= value <= count))[:10]

    @staticmethod
    def _clean_terms(values: list[str]) -> list[str]:
        return list(dict.fromkeys(str(value).strip()[:160] for value in values if str(value).strip()))[:16]

    @staticmethod
    def _fallback_refined_query(plan: QueryPlan) -> str:
        parts = [
            *plan.subject_terms,
            *plan.required_terms,
            *(term for group in plan.required_evidence_groups for term in group[:2]),
            *plan.standard_numbers,
        ]
        return " ".join(dict.fromkeys(part for part in parts if part))[:500]

    @staticmethod
    def _with_error(fallback: RerankResult, started: float, error: str) -> RerankResult:
        return RerankResult(
            hits=fallback.hits,
            sufficient=fallback.sufficient,
            used=False,
            elapsed_ms=(perf_counter() - started) * 1000,
            direct_evidence_count=fallback.direct_evidence_count,
            refined_query=fallback.refined_query,
            refined_terms=fallback.refined_terms,
            missing_evidence_groups=fallback.missing_evidence_groups,
            facts=fallback.facts,
            grounded_answer=fallback.grounded_answer,
            confidence=fallback.confidence,
            error=error,
        )
