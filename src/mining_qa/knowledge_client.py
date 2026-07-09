from typing import Any

import httpx

from .config import Settings
from .schemas import KnowledgeSearchResponse, StandardsResponse


class KnowledgeClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.knowledge_base_url.strip())

    async def search(self, question: str, filters: dict[str, Any]) -> KnowledgeSearchResponse:
        if not self.enabled:
            return KnowledgeSearchResponse(
                coverage={
                    "has_clause_level_evidence": False,
                    "needs_web_supplement": True,
                    "notes": ["知识库服务尚未配置，当前不能提供条款级证据。"],
                }
            )

        payload = {
            "query": question,
            "filters": filters,
            "options": {
                "top_k": 10,
                "include_full_text": False,
                "allow_web_supplement": True,
            },
        }
        url = self.settings.knowledge_base_url.rstrip("/") + "/knowledge/search"
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return KnowledgeSearchResponse.model_validate(response.json())

    async def standards(self, params: dict[str, Any]) -> StandardsResponse:
        if not self.enabled:
            return StandardsResponse()

        url = self.settings.knowledge_base_url.rstrip("/") + "/knowledge/standards"
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return StandardsResponse.model_validate(response.json())
