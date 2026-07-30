from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import httpx
import numpy as np

from mining_qa.agent import MiningQAAgent
from mining_qa.config import Settings
from mining_qa.knowledge_client import KnowledgeClient
from mining_qa.query_understanding import understand_query
from mining_qa.retrieval_planner import PlannerResult, QueryVariant
from mining_qa.schemas import AskRequest, KnowledgeSearchResponse
from mining_qa.v4_retrieval_store_v2 import (
    ResilientQwenQueryEmbedder,
    ResilientV4KnowledgeStore,
)


QUESTION = "SD法资源量估算中，当同一矿体产状变化大时，计算单元应如何划分？"


class _CountingEmbeddingClient:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    def embed(self, texts, **kwargs):  # noqa: ANN001
        self.calls += 1
        time.sleep(0.05)
        return np.ones((len(texts), 1024), dtype=np.float32), 1, 50.0

    def close(self) -> None:
        self.closed = True


class _SyntheticSingleFlightStore(ResilientV4KnowledgeStore):
    def __init__(self) -> None:
        from concurrent.futures import Future
        from threading import Lock, local

        self.calls = 0
        self._search_state_lock = Lock()
        self._search_inflight: dict[str, Future] = {}
        self._search_call_state = local()

    def _search_key(self, payload):  # noqa: ANN001
        return "same-effective-query"

    def _run_uncached_search(self, payload):  # noqa: ANN001
        self.calls += 1
        time.sleep(0.05)
        return {
            "query": payload["query"],
            "results": [{"chunk_id": "runit-test"}],
            "retrieval": {
                "search_singleflight": {"coalesced": False, "wait_ms": 0.0}
            },
            "coverage": {},
        }


class _TraceSink:
    def __init__(self) -> None:
        self.rows = []

    def write(self, payload):  # noqa: ANN001
        self.rows.append(payload)


class _GapSink:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.calls += 1
        return None


class _FailingKnowledge:
    def __init__(self) -> None:
        self.calls = 0

    async def search(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.calls += 1
        raise httpx.ReadTimeout("synthetic KB timeout")


class _PrimaryFailureThenEmptySuccess:
    def __init__(self) -> None:
        self.calls = 0

    async def search(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.calls += 1
        if self.calls == 1:
            raise httpx.ReadTimeout("synthetic primary timeout")
        return KnowledgeSearchResponse(
            coverage={
                "has_clause_level_evidence": False,
                "needs_web_supplement": True,
                "notes": ["synthetic empty supplement"],
            }
        )


class _TwoRoutePlanner:
    async def plan(self, question, base_plan):  # noqa: ANN001
        variant = QueryVariant(
            target="计算单元划分",
            query="SD法 计算单元 产状变化",
        )
        return PlannerResult(
            plan=base_plan,
            used=False,
            elapsed_ms=0.1,
            query_variants=(variant,),
            evidence_targets=(variant,),
        )


class V4EmbeddingResilienceTests(unittest.TestCase):
    def test_p1_fix_runtime_uses_the_resilient_v2_store_contract(self) -> None:
        from mining_qa import knowledge_service

        manifest = json.dumps({"runtime_id": "v4-hybrid-fixed20-p1fix-v4"})
        sentinel = object()
        with (
            patch.dict(os.environ, {"V4_RUNTIME_MANIFEST": "/tmp/p1-runtime.json"}),
            patch.object(Path, "read_text", return_value=manifest),
            patch.object(knowledge_service, "KnowledgeStore", return_value=object()),
            patch.object(
                knowledge_service,
                "ResilientV4KnowledgeStore",
                return_value=sentinel,
            ) as resilient_store,
        ):
            result = knowledge_service.build_store("v4", query_embedder=object())

        self.assertIs(result, sentinel)
        resilient_store.assert_called_once()

    def test_embedding_timeout_budget_is_independent_from_outer_request_budget(self) -> None:
        settings = Settings(
            REQUEST_TIMEOUT_SECONDS=60,
            KNOWLEDGE_REQUEST_TIMEOUT_SECONDS=20,
            V4_EMBEDDING_TIMEOUT_SECONDS=3,
            V4_EMBEDDING_MAX_RETRIES=1,
        )
        client = KnowledgeClient(settings)._http_client()
        try:
            self.assertEqual(client.timeout.read, 20)
            self.assertEqual(settings.v4_embedding_timeout_seconds, 3)
            self.assertEqual(settings.v4_embedding_max_retries, 1)
        finally:
            import asyncio

            asyncio.run(client.aclose())

    def test_same_query_embedding_is_single_flight_and_cached(self) -> None:
        client = _CountingEmbeddingClient()
        embedder = ResilientQwenQueryEmbedder(
            settings=Settings(V4_QUERY_EMBEDDING_CACHE_SIZE=8),
            client=client,
        )
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                vectors = list(pool.map(embedder.embed_query, [QUESTION] * 3))
            cached = embedder.embed_query(QUESTION)
        finally:
            embedder.close()
        self.assertEqual(client.calls, 1)
        self.assertTrue(client.closed)
        self.assertTrue(all(vector.shape == (1024,) for vector in vectors))
        self.assertEqual(cached.shape, (1024,))
        self.assertTrue(embedder.last_call_metrics()["cache_hit"])

    def test_same_effective_search_is_computed_once(self) -> None:
        store = _SyntheticSingleFlightStore()
        with ThreadPoolExecutor(max_workers=3) as pool:
            responses = list(
                pool.map(store.search, [{"query": QUESTION}] * 3)
            )
        self.assertEqual(store.calls, 1)
        self.assertEqual(
            sum(
                int(item["retrieval"]["search_singleflight"]["coalesced"])
                for item in responses
            ),
            2,
        )


class AgentKnowledgeFailureTests(unittest.IsolatedAsyncioTestCase):
    def _agent(self) -> MiningQAAgent:
        settings = Settings(
            KNOWLEDGE_BASE_URL="http://unused.test",
            OPENAI_API_KEY="",
            QUERY_PLANNER_ENABLED=False,
            EVIDENCE_RERANKER_ENABLED=False,
            RETRIEVAL_TRACE_ENABLED=False,
        )
        agent = MiningQAAgent(settings)
        agent.trace = _TraceSink()
        agent.gap_tasks = _GapSink()
        return agent

    async def test_all_kb_timeouts_return_safe_insufficient_response(self) -> None:
        agent = self._agent()
        knowledge = _FailingKnowledge()
        agent.knowledge = knowledge

        response = await agent.ask(AskRequest(question=QUESTION))

        self.assertEqual(response.status, "insufficient_evidence")
        self.assertEqual(response.sources, [])
        self.assertEqual(response.retrieval.query_count, 0)
        self.assertIn("停止作答", response.answer)
        self.assertEqual(agent.gap_tasks.calls, 0)
        self.assertEqual(agent.trace.rows[0]["knowledge_errors"][0]["error_type"], "ReadTimeout")

    async def test_primary_timeout_uses_successful_supplement_instead_of_raising(self) -> None:
        agent = self._agent()
        knowledge = _PrimaryFailureThenEmptySuccess()
        agent.knowledge = knowledge
        agent.planner = _TwoRoutePlanner()

        response = await agent.ask(AskRequest(question=QUESTION))

        self.assertEqual(knowledge.calls, 2)
        self.assertEqual(response.status, "queued_for_enrichment")
        self.assertEqual(response.retrieval.query_count, 1)
        self.assertIn("部分检索查询执行失败", " ".join(response.limitations.notes))
        self.assertEqual(agent.trace.rows[0]["knowledge_errors"][0]["primary"], True)


if __name__ == "__main__":
    unittest.main()
