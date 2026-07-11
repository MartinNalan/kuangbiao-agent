from typing import Any

import httpx

from .config import Settings
from .query_understanding import QueryPlan, understand_query
from .schemas import KnowledgeSearchResponse, StandardsResponse


class KnowledgeClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.knowledge_base_url.strip())

    async def search(
        self,
        question: str,
        filters: dict[str, Any],
        plan: QueryPlan | None = None,
        *,
        retrieval_round: int = 1,
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
        payload = {
            "query": question,
            "filters": filters,
            "retrieval_plan": effective_plan.to_payload(),
            "options": {
                "top_k": 30
                if effective_plan.search_mode in {"comparison", "exhaustive"}
                or effective_plan.exhaustive_search
                else 12,
                "include_full_text": False,
                "allow_web_supplement": True,
                "retrieval_round": retrieval_round,
            },
        }
        url = self.settings.knowledge_base_url.rstrip("/") + "/knowledge/search"
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, trust_env=False) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return KnowledgeSearchResponse.model_validate(response.json())

    async def standards(self, params: dict[str, Any]) -> StandardsResponse:
        if not self.enabled:
            return StandardsResponse()

        url = self.settings.knowledge_base_url.rstrip("/") + "/knowledge/standards"
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, trust_env=False) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return StandardsResponse.model_validate(response.json())

    async def create_candidates(self, question: str, sources: list[dict[str, Any]]) -> int:
        if not self.enabled or not sources:
            return 0

        url = self.settings.knowledge_base_url.rstrip("/") + "/knowledge/candidates"
        created = 0
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, trust_env=False) as client:
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
                    response = await client.post(url, json=payload)
                    response.raise_for_status()
                except httpx.HTTPError:
                    continue
                created += 1
        return created
