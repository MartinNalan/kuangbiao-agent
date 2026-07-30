from dataclasses import replace
from typing import Any

import httpx

from .config import Settings
from .query_understanding import (
    QueryPlan,
    broad_classification_structural_query,
    understand_query,
)
from .schemas import KnowledgeSearchResponse, StandardsResponse


def is_broad_classification_question(question: str, plan: QueryPlan) -> bool:
    return bool(broad_classification_structural_query(question, plan))


def primary_retrieval_question(
    original_question: str,
    canonical_question: str,
    plan: QueryPlan,
) -> str:
    """Keep the primary v4 retrieval route bound to the user's question.

    The accepted v4 fixed-20 baseline was evaluated from the original question.
    Question resolution may still provide intent, classification and structural
    metadata, but substituting its rewrite as the lexical/dense query creates a
    different candidate pool from the locally accepted runtime.  Canonical text
    therefore cannot replace the primary retrieval text.
    """

    del canonical_question, plan
    return original_question


def structural_evidence_plan(question: str, plan: QueryPlan) -> QueryPlan:
    """Expose broad-classification identity only to the structural selector.

    The canonical question remains unchanged for lexical and dense retrieval.
    This marker lets the already-accepted same-section completion rule survive
    harmless model paraphrases such as “分类划分标准是什么”.
    """

    structural = broad_classification_structural_query(question, plan)
    if not structural or plan.scope_origin == "semantic_target":
        return plan
    return replace(plan, structural_query=structural)


def evidence_window_top_k(question: str, plan: QueryPlan) -> int:
    """Return the complete candidate window accepted by the v4 baseline.

    The v4 runtime already builds a fixed pool of twenty candidates. Asking the
    Knowledge API for only ten rows does not save retrieval work; it only clips
    evidence before the Agent can validate it and breaks local/API parity.
    """

    del question, plan
    return 20


class KnowledgeClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.knowledge_base_url.strip())

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.settings.knowledge_request_timeout_seconds,
                trust_env=False,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def search(
        self,
        question: str,
        filters: dict[str, Any],
        plan: QueryPlan | None = None,
        *,
        retrieval_round: int = 1,
        top_k: int | None = None,
        allow_web_supplement: bool = True,
    ) -> KnowledgeSearchResponse:
        if not self.enabled:
            return KnowledgeSearchResponse(
                coverage={
                    "has_clause_level_evidence": False,
                    "needs_web_supplement": True,
                    "notes": ["知识库服务尚未配置，当前不能提供条款级证据。"],
                }
            )

        effective_plan = plan or understand_query(question)
        kb_plan = structural_evidence_plan(question, effective_plan)
        payload = {
            "query": question,
            "filters": filters,
            "retrieval_plan": kb_plan.to_payload(),
            "options": {
                "top_k": top_k
                if top_k is not None
                else evidence_window_top_k(question, effective_plan),
                "include_full_text": False,
                "allow_web_supplement": allow_web_supplement,
                "retrieval_round": retrieval_round,
            },
        }
        url = self.settings.knowledge_base_url.rstrip("/") + "/knowledge/search"
        response = await self._http_client().post(url, json=payload)
        response.raise_for_status()
        return KnowledgeSearchResponse.model_validate(response.json())

    async def standards(self, params: dict[str, Any]) -> StandardsResponse:
        if not self.enabled:
            return StandardsResponse()

        url = self.settings.knowledge_base_url.rstrip("/") + "/knowledge/standards"
        response = await self._http_client().get(url, params=params)
        response.raise_for_status()
        return StandardsResponse.model_validate(response.json())

    async def research_corpus(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            return {
                "items": [],
                "total": 0,
                "returned": 0,
                "truncated": False,
                "knowledge_snapshot": None,
            }
        url = self.settings.knowledge_base_url.rstrip("/") + "/knowledge/research/corpus"
        response = await self._http_client().post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def create_candidates(self, question: str, sources: list[dict[str, Any]]) -> int:
        if not self.enabled or not sources:
            return 0

        url = self.settings.knowledge_base_url.rstrip("/") + "/knowledge/candidates"
        created = 0
        for source in sources:
            payload = {
                "triggering_question": question,
                "standard_no": source.get("standard_no"),
                "title": source.get("title"),
                "source_url": source.get("url"),
                "source_type": source.get("source_type"),
                "text_access": source.get("text_access"),
                "extracted_text": source.get("quote"),
                "review_status": "candidate_found",
                "copyright_note": "Candidate discovered from official source lookup; admin approval required before public KB ingestion.",
            }
            try:
                response = await self._http_client().post(url, json=payload)
                response.raise_for_status()
            except httpx.HTTPError:
                continue
            created += 1
        return created
