from __future__ import annotations

import json
import unittest

from mining_qa.config import Settings
from mining_qa.question_resolution import QuestionResolver
from mining_qa.query_understanding import understand_query
from mining_qa.retrieval_planner import PlannerResult, QueryVariant, RetrievalPlanner
from mining_qa.schemas import AskRequest


class FakeUnifiedLLM:
    enabled = True

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0
        self.messages = []

    async def complete_json(self, messages, *, max_tokens=None):  # noqa: ANN001
        self.calls += 1
        self.messages = messages
        return json.dumps(self.payload, ensure_ascii=False)

    async def aclose(self) -> None:
        return None


class UnifiedQueryPlanningTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def settings() -> Settings:
        return Settings(
            OPENAI_API_KEY="configured",
            QUESTION_RESOLUTION_ENABLED=True,
            QUERY_PLANNER_ENABLED=True,
            UNIFIED_QUERY_PLANNING_ENABLED=True,
        )

    async def test_one_response_compiles_independent_evidence_targets(self) -> None:
        llm = FakeUnifiedLLM(
            {
                "canonical_question": "样品制备中样品损失率和缩分误差有何要求？",
                "intent": "general",
                "primary_intent": "technical_method",
                "target_entity": "样品制备",
                "target_outcome": "核验损失率和缩分误差要求",
                "document_types": ["standard", "industry_standard"],
                "evidence_slots": ["样品损失率", "缩分误差"],
                "output_shape": "requirements_and_advice",
                "is_ambiguous": False,
                "confidence": 0.94,
                "missing_slots": [],
                "reason": "两个并列数值要求需要分别核验。",
                "interpretations": [],
                "search_mode": "default",
                "subject_terms": ["样品制备"],
                "required_terms": ["样品损失率", "缩分误差"],
                "alternative_terms": ["制样损失率", "缩分允许误差"],
                "subqueries": [
                    {
                        "target": "样品损失率要求",
                        "query": "样品制备 样品损失率 要求",
                        "document_types": ["standard", "industry_standard"],
                        "alternative_terms": ["样品损失率", "制样损失率"],
                    },
                    {
                        "target": "缩分误差要求",
                        "query": "样品制备 缩分误差 要求",
                        "document_types": ["standard", "industry_standard"],
                        "alternative_terms": ["缩分误差", "缩分允许误差"],
                    },
                ],
            }
        )
        result = await QuestionResolver(self.settings(), llm=llm).resolve(
            "样品制备过程中对样品损失率和缩分误差有何要求？"
        )

        self.assertEqual(llm.calls, 1)
        self.assertIsNotNone(result.prepared_planner_result)
        prepared = result.prepared_planner_result
        self.assertTrue(prepared.used)  # type: ignore[union-attr]
        self.assertEqual(len(prepared.query_variants), 2)  # type: ignore[union-attr]
        self.assertEqual(len(prepared.evidence_targets), 2)  # type: ignore[union-attr]
        self.assertIn("subqueries", llm.messages[0]["content"])
        self.assertNotIn("subqueries", llm.messages[1]["content"])
        self.assertTrue(result.plan.planner_used)

    async def test_default_path_does_not_prepare_a_plan(self) -> None:
        llm = FakeUnifiedLLM(
            {
                "canonical_question": "金矿勘查工程间距是多少？",
                "intent": "engineering_distance_lookup",
                "is_ambiguous": False,
                "confidence": 0.9,
                "missing_slots": [],
                "reason": "问题明确。",
                "interpretations": [],
            }
        )
        settings = self.settings().model_copy(
            update={"unified_query_planning_enabled": False}
        )
        result = await QuestionResolver(settings, llm=llm).resolve(
            "金矿勘查工程间距是多少？"
        )

        self.assertIsNone(result.prepared_planner_result)
        self.assertNotIn("subqueries", llm.messages[1]["content"])

    async def test_multi_group_unified_plan_without_variants_falls_back(self) -> None:
        llm = FakeUnifiedLLM(
            {
                "canonical_question": "样品制备中样品损失率和缩分误差有何要求？",
                "intent": "general",
                "primary_intent": "technical_method",
                "target_entity": "样品制备",
                "document_types": ["standard"],
                "is_ambiguous": False,
                "confidence": 0.9,
                "required_evidence_groups": [["样品损失率"], ["缩分误差"]],
                "subqueries": [],
            }
        )

        result = await QuestionResolver(self.settings(), llm=llm).resolve(
            "样品制备过程中对样品损失率和缩分误差有何要求？"
        )

        self.assertEqual(llm.calls, 1)
        self.assertIsNone(result.prepared_planner_result)
        self.assertEqual(len(result.plan.required_evidence_groups), 2)

    def test_single_group_unified_plan_can_keep_the_one_call_path(self) -> None:
        question = "剩余控制经济可采储量的定义是什么？"
        result = RetrievalPlanner.result_from_payload(
            question,
            understand_query(question),
            {
                "canonical_query": question,
                "intent": "definition_explanation",
                "required_evidence_groups": [["剩余控制经济可采储量"]],
                "confidence": 0.9,
            },
        )

        self.assertTrue(RetrievalPlanner.unified_result_is_complete(result))

    def test_internal_request_transports_prepared_result(self) -> None:
        request = AskRequest(question="金矿勘查间距")
        marker = object()
        request._prepared_planner_result = marker

        self.assertIs(request.prepared_planner_result, marker)

    def test_payload_compiler_preserves_parallel_targets(self) -> None:
        question = "卤水矿及深层固体盐类矿床详查报告的适用条件"
        base_plan = understand_query(question)
        compiled = RetrievalPlanner.result_from_payload(
            question,
            base_plan,
            {
                "canonical_query": question,
                "intent": base_plan.intent,
                "search_mode": "default",
                "document_types": ["standard"],
                "confidence": 0.9,
                "subqueries": [
                    {"target": "卤水矿分支", "query": "卤水矿 详查报告 条件"},
                    {"target": "深层固体盐类分支", "query": "深层固体盐类 详查报告 条件"},
                ],
            },
        )

        self.assertEqual([item.target for item in compiled.query_variants], ["卤水矿分支", "深层固体盐类分支"])

    def test_parallel_result_rebases_on_resolved_classification(self) -> None:
        question = "省里发的金矿采矿许可证，储量评审备案找谁？"
        preliminary_plan = understand_query(question)
        preliminary = PlannerResult(
            plan=preliminary_plan,
            used=True,
            elapsed_ms=1234.0,
            query_variants=(
                QueryVariant(
                    target="许可证颁发机关与备案权限关系",
                    query="省级颁发采矿许可证 储量评审备案 负责",
                    document_types=("policy_document",),
                    alternative_terms=("本级已颁发采矿许可证", "评审备案"),
                ),
            ),
        )
        resolved_plan = understand_query(
            "省级自然资源主管部门颁发采矿许可证的金矿，矿产资源储量评审备案由谁负责？"
        )

        rebased = RetrievalPlanner.rebase_result(
            question,
            resolved_plan,
            preliminary,
        )

        self.assertEqual(rebased.plan.classification, resolved_plan.classification)
        self.assertEqual(rebased.plan.intent, resolved_plan.intent)
        self.assertEqual(rebased.query_variants[0].target, "许可证颁发机关与备案权限关系")
        self.assertEqual(rebased.elapsed_ms, 1234.0)


if __name__ == "__main__":
    unittest.main()
