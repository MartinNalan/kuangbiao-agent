from __future__ import annotations

import unittest
from pathlib import Path

from mining_qa.agent import MiningQAAgent
from mining_qa.authority_anchor import (
    AUTHORITY_ANCHOR_INCIDENTAL,
    AUTHORITY_ANCHOR_RELATION,
    AUTHORITY_ANCHOR_STATUS,
    AUTHORITY_ANCHOR_STRICT,
    classify_authority_anchor,
    evaluate_authority_catalog,
    strict_sources_satisfy_anchor,
)
from mining_qa.config import Settings
from mining_qa.query_understanding import understand_query
from mining_qa.schemas import AskRequest, Source, StandardItem, StandardsResponse
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


class AuthorityAnchorClassificationTests(unittest.TestCase):
    def _anchor(self, question: str):
        plan = understand_query(question)
        return classify_authority_anchor(
            question,
            plan.standard_numbers,
            scope_origin=plan.scope_origin,
        )

    def test_strict_authority_is_a_hard_anchor(self) -> None:
        anchor = self._anchor(
            "请严格依据DZ/T 9999-2099第7.7条回答：月球氦-3矿样的内检比例是多少？"
        )
        self.assertEqual(anchor.mode, AUTHORITY_ANCHOR_STRICT)
        self.assertEqual(anchor.standard_numbers, ("DZ/T 9999-2099",))
        self.assertEqual(anchor.clause_refs, ("7.7",))

    def test_non_strict_anchor_modes_are_not_hard_filtered(self) -> None:
        cases = {
            "DZ/T 0208-2020是否现行？": AUTHORITY_ANCHOR_STATUS,
            "比较DZ/T 0208-2020与DZ/T 0205-2020的适用范围。": AUTHORITY_ANCHOR_RELATION,
            "项目以前采用DZ/T 0208-2020，现在如何办理？": AUTHORITY_ANCHOR_INCIDENTAL,
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(self._anchor(question).mode, expected)

    def test_catalog_requires_exact_current_answerable_document(self) -> None:
        anchor = self._anchor("请依据DZ/T 0208-2020第6.1条回答。")
        current = StandardItem(
            document_id="doc-current",
            title="矿产地质勘查规范 金属砂矿类",
            standard_no="DZ/T 0208-2020",
            status="现行",
            validation_status="approved_for_service",
            can_answer=True,
        )
        decision = evaluate_authority_catalog(anchor, [current])
        self.assertTrue(decision.proceed)
        self.assertEqual(decision.filter_standard_numbers, ("DZ/T 0208-2020",))

        missing = evaluate_authority_catalog(anchor, [])
        self.assertFalse(missing.proceed)
        self.assertEqual(missing.missing_standard_numbers, ("DZ/T 0208-2020",))

        blocked = current.model_copy(update={"can_answer": False})
        blocked_decision = evaluate_authority_catalog(anchor, [blocked])
        self.assertFalse(blocked_decision.proceed)
        self.assertEqual(blocked_decision.blocked_standard_numbers, ("DZ/T 0208-2020",))

        repealed = current.model_copy(update={"status": "废止", "can_answer": True})
        repealed_decision = evaluate_authority_catalog(anchor, [repealed])
        self.assertFalse(repealed_decision.proceed)

    def test_final_sources_must_match_standard_and_clause(self) -> None:
        anchor = self._anchor("请严格依据DZ/T 0208-2020第7.7条回答。")
        correct = Source(
            title="矿产地质勘查规范 金属砂矿类",
            standard_no="DZ/T 0208-2020",
            chapter="7.7.5.3",
            quote="内检要求。",
            source_type="local_kb",
            text_access="ocr_text",
        )
        wrong_standard = correct.model_copy(update={"standard_no": "DZ/T 0202-2020"})
        wrong_clause = correct.model_copy(update={"chapter": "6.6.3.2"})
        self.assertTrue(strict_sources_satisfy_anchor(anchor, [correct]))
        self.assertFalse(strict_sources_satisfy_anchor(anchor, [wrong_standard]))
        self.assertFalse(strict_sources_satisfy_anchor(anchor, [wrong_clause]))


class _MissingCatalogKnowledge:
    def __init__(self) -> None:
        self.standard_calls = 0
        self.search_calls = 0

    async def standards(self, params):
        self.standard_calls += 1
        return StandardsResponse()

    async def search(self, *args, **kwargs):
        self.search_calls += 1
        raise AssertionError("missing strict authority must stop before retrieval")


class _FailIfPlanned:
    async def plan(self, *args, **kwargs):
        raise AssertionError("missing strict authority must stop before model planning")


class _TraceSink:
    def __init__(self) -> None:
        self.rows = []

    def write(self, payload) -> None:
        self.rows.append(payload)


class _UnusedEmbedder:
    def embed_query(self, text):
        raise AssertionError("authority catalog guard must stop before embedding")

    def close(self) -> None:
        return None


class _InProcessV4Knowledge:
    def __init__(self, store: V4KnowledgeStore) -> None:
        self.store = store
        self.search_calls = 0

    async def standards(self, params):
        return StandardsResponse.model_validate(self.store.standards(params))

    async def search(self, *args, **kwargs):
        self.search_calls += 1
        raise AssertionError("missing strict authority must stop before v4 body retrieval")


class AuthorityAnchorAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_authority_stops_before_planner_and_retrieval(self) -> None:
        settings = Settings(
            KNOWLEDGE_BASE_URL="http://unused.test",
            OPENAI_API_KEY="",
            RETRIEVAL_TRACE_ENABLED=False,
        )
        agent = MiningQAAgent(settings)
        knowledge = _MissingCatalogKnowledge()
        trace = _TraceSink()
        agent.knowledge = knowledge
        agent.planner = _FailIfPlanned()
        agent.trace = trace

        response = await agent.ask(
            AskRequest(
                question=(
                    "请严格依据DZ/T 9999-2099第7.7条回答："
                    "月球氦-3矿样的内检比例是多少？"
                )
            )
        )

        self.assertEqual(response.status, "insufficient_evidence")
        self.assertEqual(response.sources, [])
        self.assertEqual(response.retrieval.query_count, 0)
        self.assertEqual(knowledge.standard_calls, 1)
        self.assertEqual(knowledge.search_calls, 0)
        self.assertIn("无法核验", response.answer)
        self.assertIn("DZ/T 9999-2099", response.answer)
        self.assertEqual(trace.rows[0]["authority_anchor"]["anchor"]["mode"], AUTHORITY_ANCHOR_STRICT)

    async def test_real_v4_catalog_stops_the_t077_missing_authority_case(self) -> None:
        store = ResilientV4KnowledgeStore(MANIFEST, query_embedder=_UnusedEmbedder())
        try:
            settings = Settings(
                KNOWLEDGE_BASE_URL="http://in-process-v4.test",
                OPENAI_API_KEY="",
                RETRIEVAL_TRACE_ENABLED=False,
            )
            agent = MiningQAAgent(settings)
            knowledge = _InProcessV4Knowledge(store)
            agent.knowledge = knowledge
            agent.planner = _FailIfPlanned()
            agent.trace = _TraceSink()

            response = await agent.ask(
                AskRequest(
                    question=(
                        "请严格依据DZ/T 9999-2099第7.7条回答："
                        "月球氦-3矿样的内检比例是多少？"
                    )
                )
            )
        finally:
            store.close()

        self.assertEqual(response.status, "insufficient_evidence")
        self.assertEqual(response.sources, [])
        self.assertEqual(response.retrieval.query_count, 0)
        self.assertEqual(knowledge.search_calls, 0)


if __name__ == "__main__":
    unittest.main()
