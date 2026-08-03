from __future__ import annotations

import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from mining_qa import knowledge_service
from mining_qa.agent import MiningQAAgent
from mining_qa.config import Settings
from mining_qa.knowledge_client import structural_evidence_plan
from mining_qa.query_understanding import understand_query
from mining_qa.schemas import AskRequest, KnowledgeSearchResponse, Source, StandardsResponse
from mining_qa.v4_retrieval_store import FrozenQueryEmbedder
from mining_qa.v4_retrieval_store_v2 import ResilientV4KnowledgeStore


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = (
    ROOT
    / "data"
    / "knowledge_base_v4"
    / "runtime_private"
    / "hybrid_fixed20_v4"
    / "runtime_manifest.json"
)
T074 = (
    ROOT
    / "data"
    / "knowledge_base_v4"
    / "evaluation"
    / "t074_governed_routing_activation_replay_v1"
    / "governed_routing_activation_replay_results_v1.json"
)
T073_VECTORS = (
    ROOT
    / "data"
    / "knowledge_base_v4"
    / "embedding_artifacts_private"
    / "qwen37_text_embedding_1024_t027_v1"
    / "query_sets"
    / "t073_alias_intent_boundary_v1"
    / "query_embeddings.npy"
)
T068_QUERY_ROOT = (
    ROOT
    / "data"
    / "knowledge_base_v4"
    / "embedding_artifacts_private"
    / "qwen37_text_embedding_1024_t027_v1"
    / "query_sets"
    / "t068_generic_shadow_replay_v1"
)


class FailingEmbedder:
    def embed_query(self, text: str) -> np.ndarray:
        raise RuntimeError("synthetic embedding outage")

    def close(self) -> None:
        return None


class InProcessKnowledgeClient:
    def __init__(self, store: ResilientV4KnowledgeStore) -> None:
        self.store = store

    async def search(
        self,
        question,
        filters,
        plan,
        *,
        retrieval_round=1,
        top_k=None,
        allow_web_supplement=True,
    ):
        del retrieval_round, allow_web_supplement
        return KnowledgeSearchResponse.model_validate(
            self.store.search(
                {
                    "query": question,
                    "filters": filters,
                    "retrieval_plan": plan.to_payload(),
                    "options": {"top_k": top_k or 20},
                }
            )
        )

    async def standards(self, params):
        return StandardsResponse.model_validate(self.store.standards(params))

    async def create_candidates(self, question, sources):
        del question, sources
        return 0

    async def aclose(self) -> None:
        return None


class V4LocalProductionRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.t074 = json.loads(T074.read_text(encoding="utf-8"))
        vectors = np.load(T073_VECTORS, mmap_mode="r", allow_pickle=False)
        cls.vectors_by_text = {
            case["production_plan"]["semantic_query"]: vectors[index]
            for index, case in enumerate(cls.t074["cases"])
        }
        cls.embedder = FrozenQueryEmbedder(cls.vectors_by_text)
        cls.store = ResilientV4KnowledgeStore(MANIFEST, query_embedder=cls.embedder)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.store.close()

    def _case(self, case_id: str) -> dict:
        return next(case for case in self.t074["cases"] if case["id"] == case_id)

    def test_runtime_health_and_authorization_boundary(self) -> None:
        health = self.store.health()
        authorization = self.store.manifest["authorization"]
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["runtime_version"], "v4")
        self.assertEqual(health["document_count"], 156)
        self.assertEqual(health["retrieval_leaf_count"], 23250)
        self.assertEqual(health["vector_count"], 23250)
        self.assertEqual(health["kg_relation_count"], 0)
        self.assertFalse(health["ann_available"])
        self.assertTrue(authorization["v3_remains_default"])
        self.assertFalse(authorization["cloud_activation_authorized"])
        self.assertFalse(authorization["deployment_authorized"])
        self.assertFalse(authorization["service_restart_authorized"])
        self.assertFalse(authorization["cloud_sync_required"])

    def test_standard_applicability_reproduces_t074_final_pool(self) -> None:
        case = self._case("AIP001")
        response = self.store.search(
            {"query": case["question"], "options": {"top_k": 20}}
        )
        self.assertEqual(
            [item["chunk_id"] for item in response["results"]],
            case["replay"]["final_pool"],
        )
        plan = response["coverage"]["query_plan"]
        self.assertTrue(plan["governed_mapping_applied"])
        self.assertIn("金属砂矿类", plan["lexical_query"])
        self.assertIn("砂金", plan["semantic_query"])

    def test_technical_detail_does_not_expand_and_reproduces_t074(self) -> None:
        case = self._case("AIN001")
        response = self.store.search(
            {"query": case["question"], "options": {"top_k": 20}}
        )
        self.assertEqual(
            [item["chunk_id"] for item in response["results"]],
            case["replay"]["final_pool"],
        )
        plan = response["coverage"]["query_plan"]
        self.assertFalse(plan["governed_mapping_applied"])
        self.assertNotIn("金属砂矿类", plan["lexical_query"])

    def test_rewritten_oil_gas_classification_preserves_complete_section(self) -> None:
        manifest = json.loads(
            (T068_QUERY_ROOT / "query_embedding_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        vectors = np.load(
            T068_QUERY_ROOT / "query_embeddings.npy",
            mmap_mode="r",
            allow_pickle=False,
        )
        question = "油气资源储量的分类划分标准是什么？"
        vector = vectors[manifest["case_ids"].index("QA030")]
        store = ResilientV4KnowledgeStore(
            MANIFEST,
            query_embedder=FrozenQueryEmbedder({question: vector}),
        )
        try:
            plan = structural_evidence_plan(question, understand_query(question))
            response = store.search(
                {
                    "query": question,
                    "retrieval_plan": plan.to_payload(),
                    "options": {"top_k": 20},
                }
            )
        finally:
            store.close()

        oil_gas_clauses = {
            item.get("clause_no")
            for item in response["results"]
            if item.get("standard_no") == "GB/T 19492-2020"
        }
        self.assertTrue({"4.1", "4.2", "4.3", "4.7", "4.8"} <= oil_gas_clauses)
        query_plan = response["coverage"]["query_plan"]
        self.assertEqual(query_plan["lexical_query"], question)
        self.assertEqual(query_plan["semantic_query"], question)
        self.assertIn("分类体系", query_plan["structural_query"])
        classification_trace = query_plan["structure_traces"]["classification_section"]
        self.assertEqual(
            set(classification_trace["selected_ids"]),
            {"runit-07073b1ecad0f6b8", "runit-117b5dcb7486b6ad"},
        )

    def test_ambiguous_question_stops_before_embedding_and_retrieval(self) -> None:
        case = self.t074["ambiguous_cases"][0]
        calls_before = len(self.embedder.calls)
        response = self.store.search(
            {"query": case["question"], "options": {"top_k": 20}}
        )
        self.assertEqual(response["results"], [])
        self.assertFalse(response["coverage"]["query_plan"]["retrieval_allowed"])
        self.assertEqual(len(self.embedder.calls), calls_before)
        self.assertEqual(response["retrieval"]["vector_skipped"], 1)

    def test_existing_http_models_accept_v4_results(self) -> None:
        case = self._case("AIP002")
        response = self.store.search(
            {"query": case["question"], "options": {"top_k": 10}}
        )
        KnowledgeSearchResponse.model_validate(response)
        for item in response["results"]:
            Source.model_validate(
                {
                    "title": item["title"],
                    "standard_no": item.get("standard_no"),
                    "chapter": item.get("clause_no") or item.get("section_path"),
                    "page": item.get("page"),
                    "quote": item.get("quote"),
                    "score": item.get("score"),
                    "source_type": item.get("source_type"),
                    "text_access": item.get("text_access"),
                    "url": item.get("url"),
                    "source_platform": item.get("source_platform"),
                }
            )

    def test_fastapi_search_endpoint_can_run_with_v4_store(self) -> None:
        case = self._case("AIP003")
        with patch.object(knowledge_service, "store", self.store):
            with TestClient(knowledge_service.app) as client:
                health = client.get("/knowledge/health")
                response = client.post(
                    "/knowledge/search",
                    json={"query": case["question"], "options": {"top_k": 20}},
                )
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["runtime_version"], "v4")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["chunk_id"] for item in response.json()["results"]],
            case["replay"]["final_pool"],
        )

    def test_shared_evidence_layer_returns_v4_sources_without_answer_model(self) -> None:
        case = self._case("AIP001")
        settings = Settings(
            KNOWLEDGE_BASE_URL="http://in-process-v4.test",
            OPENAI_API_KEY="",
            QUERY_PLANNER_ENABLED=False,
            EVIDENCE_RERANKER_ENABLED=False,
            RETRIEVAL_TRACE_ENABLED=False,
        )
        agent = MiningQAAgent(settings)
        agent.knowledge = InProcessKnowledgeClient(self.store)
        try:
            response = asyncio.run(
                agent.generate_evidence(AskRequest(question=case["question"]))
            )
        finally:
            asyncio.run(agent.aclose())

        self.assertEqual(response.status, "ready")
        self.assertTrue(response.answerable)
        self.assertNotIn("answer", response.model_dump())
        self.assertTrue(response.sources)
        self.assertTrue(response.sources[0].document_id)
        self.assertTrue(response.sources[0].chunk_id)

    def test_explicit_document_scope_returns_only_that_document(self) -> None:
        case = self._case("AIP001")
        expected_document = self.store.row_by_id[
            case["required_groups"][0][0]
        ]["document_id"]
        response = self.store.search(
            {
                "query": case["question"],
                "filters": {"document_id": expected_document},
                "options": {"top_k": 20},
            }
        )
        self.assertTrue(response["results"])
        self.assertTrue(
            all(item["document_id"] == expected_document for item in response["results"])
        )
        self.assertEqual(response["retrieval"]["scoped_search"], 1)

    def test_explicit_standard_scope_returns_only_that_standard(self) -> None:
        case = self._case("AIP001")
        expected_standard = self.store.row_by_id[
            case["required_groups"][0][0]
        ]["standard_no"]
        response = self.store.search(
            {
                "query": case["question"],
                "filters": {"standard_no": expected_standard},
                "options": {"top_k": 20},
            }
        )
        self.assertTrue(response["results"])
        self.assertTrue(
            all(
                item["standard_no"] == expected_standard
                for item in response["results"]
            )
        )
        self.assertEqual(response["retrieval"]["scoped_search"], 1)

    def test_nonexistent_explicit_standard_scope_never_falls_back_to_full_corpus(self) -> None:
        case = self._case("AIP001")
        response = self.store.search(
            {
                "query": case["question"],
                "filters": {"standard_no": "DZ/T 9999-2099"},
                "options": {"top_k": 20},
            }
        )
        self.assertEqual(response["results"], [])
        self.assertEqual(response["retrieval"]["scoped_search"], 1)

    def test_embedding_failure_uses_keyword_fallback(self) -> None:
        case = self._case("AIP001")
        store = ResilientV4KnowledgeStore(
            MANIFEST,
            query_embedder=FailingEmbedder(),
            validate_hashes=False,
        )
        try:
            response = store.search(
                {"query": case["question"], "options": {"top_k": 20}}
            )
        finally:
            store.close()
        self.assertEqual(len(response["results"]), 20)
        self.assertEqual(response["retrieval"]["vector_route"], "none")
        self.assertIn("关键词安全回退", " ".join(response["coverage"]["notes"]))

    def test_status_catalog_document_and_chunk_contracts(self) -> None:
        status = self.store.search(
            {"query": "DZ/T 0208-2020是否现行？", "options": {"top_k": 10}}
        )
        self.assertTrue(status["results"])
        self.assertEqual(status["results"][0]["effective_status"], "current")
        catalog = self.store.standards(
            {"standard_no": "DZ/T 0208-2020", "page": 1, "page_size": 20}
        )
        StandardsResponse.model_validate(catalog)
        self.assertEqual(catalog["pagination"]["total"], 1)
        document_id = catalog["items"][0]["document_id"]
        self.assertEqual(self.store.document(document_id)["standard_no"], "DZ/T 0208-2020")
        unit_id = self._case("AIP001")["required_groups"][0][0]
        chunk = self.store.chunk(unit_id)
        self.assertEqual(chunk["chunk_id"], unit_id)
        self.assertTrue(chunk["quote"])


if __name__ == "__main__":
    unittest.main()
