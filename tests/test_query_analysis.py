import json
import unittest
from pathlib import Path

from mining_qa.query_analysis import (
    AnchorState,
    QueryAnalysis,
    RetrievalAction,
    compile_query_analysis,
    query_analysis_json_schema,
    safe_log_record,
)


def base_payload(question: str) -> dict:
    return {
        "query_id": "test",
        "rewritten_question": question if question.endswith("？") else question + "？",
        "question_type": ["procedure"],
        "anchors": [],
        "ambiguities": [],
        "search_hypotheses": [],
        "source_preferences": {
            "primary": ["law", "policy_document"],
            "supplementary": ["service_guide"],
            "exclude_by_type": [],
        },
        "lexical_terms": [],
        "semantic_subqueries": [],
        "evidence_requirements": [
            {
                "description": "办理事项和适用条件",
                "required_elements": ["action", "condition"],
                "must_have_clause_level_evidence": True,
            }
        ],
        "self_check": {
            "preserved_original_facts": [],
            "missing_original_facts": [],
            "added_assumptions": [],
            "answer_generated": False,
        },
    }


class QueryAnalysisTests(unittest.TestCase):
    def test_original_question_and_route_are_immutable(self) -> None:
        question = "采矿权延续需要提交哪些材料？"
        payload = base_payload(question)
        payload["original_question"] = "模型改掉的问题"
        analysis = compile_query_analysis(question, payload, query_id="Q1")

        self.assertEqual(analysis.original_question, question)
        self.assertEqual(analysis.retrieval_plan.original_query, question)
        self.assertTrue(analysis.retrieval_plan.original_route_independent)
        self.assertIn("model_original_question_mismatch", analysis.validation.errors)

    def test_explicit_standard_and_clause_get_exact_route_only_filters(self) -> None:
        question = "根据DZ/T 0205-2020第8.2.3条，岩金矿详查阶段的要求是什么？"
        payload = base_payload(question)
        payload["anchors"] = [
            {
                "type": "standard_no",
                "source_text": "DZ/T 0205-2020",
                "normalized_value": "DZ/T 0205-2020",
                "interpretation": "explicit",
                "needs_confirmation": False,
            },
            {
                "type": "clause_no",
                "source_text": "第8.2.3条",
                "normalized_value": "第8.2.3条",
                "interpretation": "explicit",
                "needs_confirmation": False,
            },
        ]
        payload["lexical_terms"] = ["DZ/T 0205-2020", "第8.2.3条"]
        analysis = compile_query_analysis(question, payload, query_id="Q2")

        self.assertEqual({item.field for item in analysis.retrieval_plan.hard_filters}, {"standard_no", "clause_no"})
        self.assertTrue(all(item.scope == "exact_route_only" for item in analysis.retrieval_plan.hard_filters))
        self.assertTrue(
            all(
                RetrievalAction.HARD_FILTER_EXACT_ROUTE in anchor.retrieval_actions
                for anchor in analysis.anchors
                if anchor.type in {"standard_no", "clause_no"}
            )
        )

    def test_inferred_procedure_is_parallel_only_and_requires_confirmation(self) -> None:
        question = "采矿权已经超过有效期，下一步怎么办？"
        payload = base_payload(question)
        payload["anchors"] = [
            {
                "type": "administrative_matter",
                "source_text": "",
                "normalized_value": "采矿权延续登记",
                "interpretation": "inferred",
                "needs_confirmation": True,
            }
        ]
        payload["search_hypotheses"] = [
            {
                "query": "采矿权届满后矿业权是否消灭",
                "purpose": "检查其他法律后果",
                "needs_confirmation": False,
            }
        ]
        analysis = compile_query_analysis(question, payload, query_id="Q3")
        candidate = next(anchor for anchor in analysis.anchors if anchor.canonical_text == "采矿权延续登记")

        self.assertEqual(candidate.state, AnchorState.CANDIDATE)
        self.assertEqual(candidate.retrieval_actions, [RetrievalAction.PARALLEL_SEARCH_ONLY])
        self.assertFalse(analysis.retrieval_plan.hard_filters)
        self.assertTrue(analysis.confirmation.required)

    def test_model_proposed_standard_stays_parallel_not_primary_semantic_query(self) -> None:
        question = "岩金矿详查阶段的实验室流程试验是否满足试验程度要求？"
        payload = base_payload(question)
        payload["search_hypotheses"] = [
            {
                "query": "DZ/T 0340 矿石加工选冶试验研究程度要求",
                "purpose": "并行核对通用选冶试验规范",
                "needs_confirmation": False,
            }
        ]
        payload["semantic_subqueries"] = [
            {
                "query": "DZ/T 0340中实验室流程试验的层级是什么？",
                "purpose": "核对试验层级",
            }
        ]

        analysis = compile_query_analysis(question, payload, query_id="Q3B")

        self.assertEqual(analysis.validation.status, "pass")
        self.assertNotIn("DZ/T 0340中实验室流程试验的层级是什么？", analysis.retrieval_plan.semantic_queries)
        self.assertTrue(
            any(
                query.query == "DZ/T 0340中实验室流程试验的层级是什么？"
                for query in analysis.retrieval_plan.parallel_queries
            )
        )
        self.assertIn(
            "semantic_subquery_demoted_to_parallel:DZ/T 0340",
            analysis.validation.warnings,
        )

    def test_unsafe_normalization_is_demoted_but_raw_fact_remains_locked(self) -> None:
        question = "部里发的采矿权超过了有效期，怎么办？"
        payload = base_payload(question)
        payload["anchors"] = [
            {
                "type": "issuing_authority",
                "source_text": "部里发的",
                "normalized_value": "自然资源部颁发",
                "interpretation": "explicit",
                "needs_confirmation": True,
            }
        ]
        analysis = compile_query_analysis(question, payload, query_id="Q4")

        locked = next(anchor for anchor in analysis.anchors if anchor.raw_text == "部里发的")
        self.assertEqual(locked.state, AnchorState.LOCKED)
        self.assertEqual(locked.canonical_text, "部里发的")
        self.assertTrue(any(item.query == "自然资源部颁发" for item in analysis.search_hypotheses))
        self.assertFalse(any(item.value == "自然资源部颁发" for item in analysis.retrieval_plan.hard_filters))

    def test_ungrounded_lexical_term_becomes_search_hypothesis(self) -> None:
        question = "采矿权超过有效期后怎么办？"
        payload = base_payload(question)
        payload["lexical_terms"] = ["采矿权", "超过有效期", "延续登记"]
        analysis = compile_query_analysis(question, payload, query_id="Q5")

        self.assertIn("采矿权", analysis.retrieval_plan.lexical_terms)
        self.assertNotIn("延续登记", analysis.retrieval_plan.lexical_terms)
        self.assertTrue(any(item.query == "延续登记" for item in analysis.search_hypotheses))

    def test_sensitive_values_never_appear_in_safe_log(self) -> None:
        question = "我的手机号是13812345678，采矿许可证编号ABC-123456怎么办理延续？"
        payload = base_payload(question)
        analysis = compile_query_analysis(question, payload, query_id="Q6")
        safe = safe_log_record(analysis)
        serialized = json.dumps(safe, ensure_ascii=False)

        self.assertTrue(analysis.privacy.sensitive_data_detected)
        self.assertNotIn("13812345678", serialized)
        self.assertNotIn("ABC-123456", serialized)
        self.assertNotIn(question, serialized)

    def test_source_preferences_can_never_exclude_material_types(self) -> None:
        question = "采矿权延续怎么办？"
        payload = base_payload(question)
        payload["source_preferences"]["exclude_by_type"] = ["technical_standard"]
        analysis = compile_query_analysis(question, payload, query_id="Q7")

        self.assertEqual(analysis.source_preferences.exclude_by_type, [])
        self.assertFalse(analysis.retrieval_plan.source_preferences_are_filters)

    def test_schema_is_versioned_and_round_trips_compiled_record(self) -> None:
        question = "采矿权延续怎么办？"
        analysis = compile_query_analysis(question, base_payload(question), query_id="Q8")
        restored = QueryAnalysis.model_validate(analysis.model_dump(mode="json"))
        schema = query_analysis_json_schema()

        self.assertEqual(restored.query_id, "Q8")
        self.assertEqual(schema["$id"], "https://geowiki.local/schemas/query_analysis.v1.schema.json")
        self.assertIn("$defs", schema)

    def test_exported_schema_matches_runtime_schema(self) -> None:
        path = Path(__file__).resolve().parents[1] / "schemas" / "query_analysis.v1.schema.json"
        exported = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(exported, query_analysis_json_schema())

    def test_model_answer_field_fails_validation(self) -> None:
        question = "虚假报送储量报告有什么责任？"
        payload = base_payload(question)
        payload["answer"] = "处以罚款"
        analysis = compile_query_analysis(question, payload, query_id="Q9")

        self.assertEqual(analysis.validation.status, "fail")
        self.assertIn("model_returned_answer_field", analysis.validation.errors)
        self.assertIn("model_generated_answer", analysis.validation.errors)

    def test_broad_liability_question_searches_all_types_without_confirmation(self) -> None:
        question = "矿业权人故意报送虚假矿产资源储量报告，应当承担什么责任？"
        payload = base_payload(question)
        payload["anchors"] = [
            {
                "type": "liability_type",
                "source_text": "",
                "normalized_value": "行政责任、刑事责任和民事责任",
                "interpretation": "inferred",
                "needs_confirmation": True,
            }
        ]
        payload["ambiguities"] = [
            {
                "field": "责任类型",
                "description": "可能涉及行政责任、刑事责任或民事责任",
                "material": True,
                "confirmation_question": "您想了解的是行政责任、刑事责任还是所有责任？",
            }
        ]
        payload["search_hypotheses"] = [
            {
                "query": "虚假储量报告的行政责任和刑事责任",
                "purpose": "覆盖全部可能适用的责任类型",
                "needs_confirmation": True,
            }
        ]

        analysis = compile_query_analysis(question, payload, query_id="Q9A")

        candidate = next(anchor for anchor in analysis.anchors if anchor.type == "liability_type")
        self.assertFalse(candidate.needs_confirmation)
        self.assertFalse(analysis.ambiguities[0].material)
        self.assertFalse(analysis.search_hypotheses[0].needs_confirmation)
        self.assertFalse(analysis.confirmation.required)
        self.assertEqual(analysis.retrieval_plan.confirmation_pending, [])
        self.assertEqual(analysis.validation.status, "pass")

    def test_mining_right_cannot_be_rewritten_as_license(self) -> None:
        question = "采矿权已经超过有效期，下一步怎么办？"
        payload = base_payload(question)
        payload["rewritten_question"] = "采矿许可证已经过期，下一步怎么办？"
        payload["anchors"] = [
            {
                "type": "right_type",
                "source_text": "采矿权",
                "normalized_value": "采矿许可证",
                "interpretation": "explicit",
                "needs_confirmation": False,
            }
        ]
        payload["lexical_terms"] = ["采矿许可证"]
        analysis = compile_query_analysis(question, payload, query_id="Q10")

        self.assertEqual(analysis.rewritten_question, question)
        self.assertNotIn("采矿许可证", analysis.retrieval_plan.lexical_terms)
        self.assertFalse(any(item.query == "采矿许可证" for item in analysis.search_hypotheses))
        self.assertIn("semantic_rewrite_fell_back_to_original", analysis.validation.warnings)
        self.assertEqual(analysis.validation.status, "pass")

    def test_non_verbatim_explicit_anchor_is_demoted_not_locked(self) -> None:
        question = "采矿权新立和延续分别需要提交哪些申请材料？"
        payload = base_payload(question)
        payload["anchors"] = [
            {
                "type": "administrative_matter",
                "source_text": "采矿权延续",
                "normalized_value": "采矿权延续",
                "interpretation": "explicit",
                "needs_confirmation": False,
            }
        ]
        analysis = compile_query_analysis(question, payload, query_id="Q11")

        candidate = next(anchor for anchor in analysis.anchors if anchor.canonical_text == "采矿权延续")
        self.assertEqual(candidate.state, AnchorState.CANDIDATE)
        self.assertEqual(candidate.retrieval_actions, [RetrievalAction.PARALLEL_SEARCH_ONLY])
        self.assertIn("explicit_anchor_demoted_not_verbatim:采矿权延续", analysis.validation.warnings)
        self.assertEqual(analysis.validation.status, "pass")

    def test_program_detection_upgrades_model_candidate_for_safe_type_normalization(self) -> None:
        question = "金矿勘查1类型的推荐工程间距是多少？"
        payload = base_payload(question)
        payload["rewritten_question"] = "金矿勘查类型Ⅰ的推荐工程间距是多少？"
        payload["anchors"] = [
            {
                "type": "exploration_type",
                "source_text": "1类型",
                "normalized_value": "类型Ⅰ",
                "interpretation": "inferred",
                "needs_confirmation": False,
            }
        ]
        analysis = compile_query_analysis(question, payload, query_id="Q12")
        anchor = next(anchor for anchor in analysis.anchors if anchor.type == "exploration_type")

        self.assertEqual(anchor.state, AnchorState.LOCKED)
        self.assertEqual(anchor.canonical_text, "类型Ⅰ")
        self.assertFalse(anchor.needs_confirmation)
        self.assertIn("类型Ⅰ", analysis.retrieval_plan.lexical_terms)
        self.assertEqual(analysis.validation.status, "pass")


if __name__ == "__main__":
    unittest.main()
