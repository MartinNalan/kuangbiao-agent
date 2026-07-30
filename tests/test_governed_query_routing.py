from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from mining_qa.config import Settings
from mining_qa.domain_gate import DomainGate
from mining_qa.governed_query_routing import (
    SAND_GOLD_MAPPING_ID,
    route_governed_query,
)
from mining_qa.knowledge_store import KnowledgeStore
from mining_qa.query_analysis import compile_query_analysis, safe_log_record
from mining_qa.query_understanding import understand_query
from mining_qa.question_resolution import QuestionResolver


ROOT = Path(__file__).resolve().parents[1]
REVIEW = (
    ROOT
    / "data"
    / "knowledge_base_v4"
    / "evaluation"
    / "t072_alias_intent_boundary_review_v1"
    / "alias_intent_boundary_review_set_v1.json"
)


def model_payload(question: str) -> dict[str, object]:
    canonical = question.replace("沙金", "砂金")
    return {
        "rewritten_question": canonical,
        "question_type": ["standard_catalog_lookup"],
        "anchors": [],
        "ambiguities": [],
        "search_hypotheses": [],
        "source_preferences": {
            "primary": ["industry_standard"],
            "supplementary": [],
            "exclude_by_type": [],
        },
        "lexical_terms": ["砂金"],
        "semantic_subqueries": [],
        "evidence_requirements": [
            {
                "description": "适用标准及其范围条款",
                "required_elements": ["standard", "scope_clause"],
                "must_have_clause_level_evidence": True,
            }
        ],
        "self_check": {
            "preserved_original_facts": ["砂金"],
            "missing_original_facts": [],
            "added_assumptions": [],
            "answer_generated": False,
        },
    }


class GovernedQueryRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.review = json.loads(REVIEW.read_text(encoding="utf-8"))

    def test_all_reviewed_boundary_cases_follow_the_approved_route(self) -> None:
        counts = {
            "positive_standard_applicability": 0,
            "negative_technical_detail": 0,
            "ambiguous_requires_confirmation": 0,
        }
        for case in self.review["cases"]:
            route = route_governed_query(case["question"])
            counts[case["boundary_type"]] += 1
            self.assertEqual(route.canonical_question, case["canonical_question"])
            self.assertEqual(route.governed_intent, case["expected_intent"])
            self.assertEqual(
                route.mapping_applied,
                case["boundary_type"] == "positive_standard_applicability",
            )
            self.assertEqual(
                route.retrieval_allowed,
                case["boundary_type"] != "ambiguous_requires_confirmation",
            )
            if route.mapping_applied:
                self.assertEqual(route.lexical_query, case["expected_lexical_query"])
                self.assertEqual(route.mapping_id, SAND_GOLD_MAPPING_ID)
            else:
                self.assertEqual(route.lexical_query, case["canonical_question"])
            self.assertEqual(route.semantic_query, case["canonical_question"])
        self.assertEqual(
            counts,
            {
                "positive_standard_applicability": 6,
                "negative_technical_detail": 8,
                "ambiguous_requires_confirmation": 4,
            },
        )

    def test_technical_detail_guard_wins_even_with_standard_selector_words(self) -> None:
        detail_terms = (
            "工业指标",
            "品位",
            "取样",
            "分析",
            "缩分",
            "内检",
            "外检",
            "资源量估算",
        )
        for term in detail_terms:
            with self.subTest(term=term):
                route = route_governed_query(
                    f"沙金{term}应当使用哪个标准的具体要求？"
                )
                self.assertEqual(route.governed_intent, "technical_detail")
                self.assertFalse(route.mapping_applied)
                self.assertNotIn("金属砂矿类", route.lexical_query)
                self.assertIn("砂金", route.lexical_query)

    def test_observed_qa008_maps_lexically_without_hard_document_scope(self) -> None:
        plan = understand_query("沙金应该使用哪个标准？")
        self.assertEqual(plan.governed_intent, "standard_applicability")
        self.assertTrue(plan.governed_mapping_applied)
        self.assertIn("金属砂矿类", plan.lexical_query)
        self.assertIn("砂金", plan.semantic_query)
        self.assertNotIn("金属砂矿类", plan.semantic_query)
        self.assertNotIn("金属砂矿类", plan.candidate_title_terms)
        self.assertNotIn("DZ/T 0208-2020", plan.standard_numbers)
        self.assertFalse(plan.has_hard_candidate_scope)

    def test_query_analysis_keeps_a_complete_governed_audit_trace(self) -> None:
        question = "沙金应该使用哪个标准？"
        analysis = compile_query_analysis(
            question,
            model_payload(question),
            query_id="QA008-routing",
        )
        plan = analysis.retrieval_plan
        self.assertEqual(plan.governed_intent, "standard_applicability")
        self.assertEqual(plan.governed_mapping_id, SAND_GOLD_MAPPING_ID)
        self.assertTrue(plan.governed_mapping_applied)
        self.assertTrue(plan.retrieval_allowed)
        self.assertIn("金属砂矿类", plan.lexical_query)
        self.assertEqual(plan.semantic_queries[0], "砂金应该使用哪个标准？")
        self.assertEqual(
            [(item.source_text, item.target_text) for item in analysis.normalizations],
            [("沙金", "砂金")],
        )
        self.assertEqual(
            safe_log_record(analysis)["governed_routing"],
            {
                "intent": "standard_applicability",
                "mapping_id": SAND_GOLD_MAPPING_ID,
                "mapping_applied": True,
                "retrieval_allowed": True,
            },
        )

    def test_ambiguous_search_stops_before_all_retrieval_routes(self) -> None:
        store = object.__new__(KnowledgeStore)
        with (
            patch.object(
                KnowledgeStore,
                "_lexical_and_graph_candidates",
                side_effect=AssertionError("lexical route must not run"),
            ),
            patch.object(
                KnowledgeStore,
                "_vector_candidates",
                side_effect=AssertionError("embedding/vector route must not run"),
            ),
            patch("mining_qa.knowledge_store.connect") as connect_mock,
        ):
            result = store.search({"query": "沙金怎么勘查？"})
        connect_mock.assert_not_called()
        self.assertEqual(result["results"], [])
        self.assertEqual(result["retrieval"]["retrieval_round"], 0)
        self.assertEqual(result["retrieval"]["full_text_hits"], 0)
        self.assertEqual(result["retrieval"]["vector_hits"], 0)
        self.assertFalse(result["coverage"]["query_plan"]["retrieval_allowed"])


class GovernedQueryResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_question_has_confirmation_without_a_model(self) -> None:
        resolver = QuestionResolver(
            Settings(QUESTION_RESOLUTION_ENABLED=False)
        )
        try:
            result = await resolver.resolve("沙金怎么勘查？")
        finally:
            await resolver.aclose()
        self.assertTrue(result.requires_clarification)
        self.assertFalse(result.plan.retrieval_allowed)
        clarification = result.clarification
        assert clarification is not None
        self.assertEqual(
            [item.label for item in clarification.options],
            ["适用标准", "工业指标", "取样分析", "资源量估算"],
        )

    async def test_canonical_sand_gold_question_reaches_same_confirmation_boundary(self) -> None:
        resolver = QuestionResolver(
            Settings(QUESTION_RESOLUTION_ENABLED=False)
        )
        try:
            result = await resolver.resolve("砂金有什么要求？")
        finally:
            await resolver.aclose()

        self.assertTrue(DomainGate().check("砂金有什么要求？").in_scope)
        self.assertTrue(result.requires_clarification)
        self.assertFalse(result.plan.retrieval_allowed)
        clarification = result.clarification
        assert clarification is not None
        self.assertEqual(clarification.pending_slot, "sand_gold_requirement_scope")
        self.assertEqual(
            [item.label for item in clarification.options],
            ["适用标准", "工业指标", "取样分析", "资源量估算"],
        )


if __name__ == "__main__":
    unittest.main()
