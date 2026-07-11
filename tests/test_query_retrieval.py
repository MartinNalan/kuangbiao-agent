import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mining_qa.agent import MiningQAAgent
from mining_qa.knowledge_store import KnowledgeStore, table_quote
from mining_qa.query_understanding import contextualize_follow_up, understand_query
from mining_qa.schemas import Source


QUESTIONS = (
    "金矿勘查1类型的推荐工程间距是多少？",
    "金矿勘查Ⅰ类型的推荐工程间距是多少？",
    "金矿勘查一类型的推荐工程间距是多少？",
)

TABLE_JSON = json.dumps(
    {
        "caption": "表 F.1 参考基本勘查工程间距",
        "matrix": [
            ["勘查类型", "控制资源量勘查工程间距/m", "控制资源量勘查工程间距/m", "控制资源量勘查工程间距/m", "控制资源量勘查工程间距/m"],
            ["勘查类型", "坑探", "坑探", "钻探", "钻探"],
            ["勘查类型", "穿脉", "沿脉", "走向", "倾斜"],
            ["工", "80~160", "80~160", "80~160", "80~160"],
            ["Ⅱ", "40~80", "40~80", "40~80", "40~80"],
            ["Ⅲ", "20~40", "20~40", "20~40", "20~40"],
        ],
    },
    ensure_ascii=False,
)


def engineering_row() -> dict:
    return {
        "chunk_id": "chunk-f1",
        "document_id": "doc-gold",
        "chunk_type": "table",
        "title": "矿产地质勘查规范 岩金",
        "standard_no": "DZ/T 0205-2020",
        "section_path": "表 F.1 参考基本勘查工程间距",
        "clause_no": None,
        "page_start": 26,
        "page_end": 26,
        "text": "表 F.1 参考基本勘查工程间距\n工\t80~160\t80~160\t80~160\t80~160",
        "table_json": TABLE_JSON,
        "source_type": "local_kb",
        "text_access": "ocr_text",
        "validation_status": "approved",
        "document_type": "industry_standard",
        "status": "active",
        "official_url": "http://www.nrsis.org.cn/portal/stdDetail/240754",
        "source_platform": "自然资源标准化信息服务平台",
        "rank": 0.0,
    }


class QueryUnderstandingTests(unittest.TestCase):
    def test_equivalent_exploration_type_forms_share_one_plan(self) -> None:
        plans = [understand_query(question) for question in QUESTIONS]

        self.assertEqual({plan.normalized_query for plan in plans}, {"金矿勘查Ⅰ类型的推荐工程间距是多少?"})
        self.assertEqual({plan.target_exploration_type for plan in plans}, {"Ⅰ"})
        self.assertEqual({plan.intent for plan in plans}, {"engineering_distance_lookup"})
        self.assertEqual({plan.candidate_title_terms for plan in plans}, {("岩金",)})

    def test_projection_comparison_uses_exhaustive_search(self) -> None:
        plan = understand_query("关于矿体外推所依据的距离，不同标准规定是否不一致？")

        self.assertEqual(plan.intent, "projection_comparison")
        self.assertTrue(plan.exhaustive_search)
        self.assertFalse(plan.has_candidate_scope)

    def test_high_value_policy_and_numeric_intents_are_separate(self) -> None:
        cases = {
            "采矿证延续需要提交什么材料？": ("service_materials", "自然资规〔2023〕4号"),
            "采矿证办理应该依据哪个文件": ("service_procedure_basis", "自然资规〔2023〕4号"),
            "资源量估算中，无限外推是推1/2还是1/4": ("projection_numeric_rule", "DZ/T 0338.1-2020"),
            "根据矿产资源法实施条例，资源储量报告的真实性由谁负责": ("legal_responsibility", "国令第839号"),
            "我的储量报告评审应该去哪个机构": ("authority_responsibility", "自然资规〔2023〕6号"),
        }
        for question, (intent, document_no) in cases.items():
            with self.subTest(question=question):
                plan = understand_query(question)
                self.assertEqual(plan.intent, intent)
                self.assertIn(document_no, plan.standard_numbers)

    def test_context_dependent_follow_up_is_rewritten_with_previous_question(self) -> None:
        rewritten = contextualize_follow_up(
            "是否还有其他文件规定了相关内容？",
            "勘查实施方案的评审或审查是怎么规定的？",
        )

        self.assertIn("勘查实施方案的评审或审查", rewritten)
        self.assertIn("其他文件", rewritten)
        self.assertEqual(understand_query(rewritten).intent, "related_documents")
        self.assertTrue(understand_query(rewritten).exhaustive_search)


class TableExtractionTests(unittest.TestCase):
    def test_target_row_keeps_all_four_direction_values(self) -> None:
        quotes = [table_quote(TABLE_JSON, "", question) for question in QUESTIONS]

        self.assertEqual(len(set(quotes)), 1)
        quote = quotes[0]
        self.assertIn("Ⅰ类型", quote)
        for label in ("坑探-穿脉", "坑探-沿脉", "钻探-走向", "钻探-倾斜"):
            self.assertIn(f"{label} 80～160 m", quote)

    def test_later_type_rows_do_not_treat_prior_values_as_headers(self) -> None:
        quote = table_quote(TABLE_JSON, "", "金矿勘查Ⅱ类型的推荐工程间距是多少？")

        for label in ("坑探-穿脉", "坑探-沿脉", "钻探-走向", "钻探-倾斜"):
            self.assertIn(f"{label} 40～80 m", quote)
        self.assertNotIn("穿脉-80", quote)


class RetrievalStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = KnowledgeStore(Path(self.temp_dir.name) / "knowledge.sqlite")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_sufficient_scoped_fts_evidence_skips_vectors(self) -> None:
        row = engineering_row()
        candidates = {
            row["chunk_id"]: {"row": row, "hit_types": {"full_text"}, "boost": 2.0, "order": 0}
        }
        with (
            patch.object(
                self.store,
                "_candidate_scope",
                return_value=(["d.visibility in ('internal', 'public')"], [], [row["document_id"]]),
            ),
            patch.object(self.store, "_lexical_and_graph_candidates", return_value=candidates),
            patch.object(self.store, "_vector_candidates", return_value=[]) as vector_candidates,
        ):
            result = self.store.search({"query": QUESTIONS[0], "options": {"top_k": 10}})

        vector_candidates.assert_not_called()
        self.assertEqual(result["retrieval"]["scoped_search"], 1)
        self.assertEqual(result["retrieval"]["vector_skipped"], 1)
        self.assertEqual(result["results"][0]["standard_no"], "DZ/T 0205-2020")

    def test_dense_success_does_not_run_local_hash(self) -> None:
        dense_row = engineering_row()
        with (
            patch.object(self.store, "_dense_embedding_candidates", return_value=(True, [(dense_row, 0.7)])),
            patch.object(self.store, "_local_hash_vector_candidates", return_value=[]) as local_hash,
        ):
            result = self.store._vector_candidates(None, "query", ["1=1"], [], 10)  # type: ignore[arg-type]

        local_hash.assert_not_called()
        self.assertEqual(result[0][0]["chunk_id"], "chunk-f1")
        self.assertAlmostEqual(result[0][1], 0.85)

    def test_dense_failure_falls_back_to_local_hash(self) -> None:
        local_row = engineering_row()
        with (
            patch.object(self.store, "_dense_embedding_candidates", return_value=(False, [])),
            patch.object(
                self.store,
                "_local_hash_vector_candidates",
                return_value=[(local_row, 0.4)],
            ) as local_hash,
        ):
            result = self.store._vector_candidates(None, "query", ["1=1"], [], 10)  # type: ignore[arg-type]

        local_hash.assert_called_once()
        self.assertEqual(result[0][0]["chunk_id"], "chunk-f1")


class FastAnswerTests(unittest.TestCase):
    def test_equivalent_questions_produce_identical_structured_answer(self) -> None:
        agent = object.__new__(MiningQAAgent)
        source = Source(
            title="矿产地质勘查规范 岩金",
            standard_no="DZ/T 0205-2020",
            chapter="表 F.1 参考基本勘查工程间距",
            quote=table_quote(TABLE_JSON, "", QUESTIONS[0]),
            source_type="local_kb",
            text_access="ocr_text",
        )

        answers = [agent._fast_answer(question, [source]) for question in QUESTIONS]

        self.assertEqual(len(set(answers)), 1)
        answer = answers[0] or ""
        self.assertIn("坑探**：穿脉 80～160 m；沿脉 80～160 m", answer)
        self.assertIn("钻探**：走向 80～160 m；倾斜 80～160 m", answer)

    def test_numeric_projection_answer_distinguishes_half_from_quarter(self) -> None:
        agent = object.__new__(MiningQAAgent)
        source = Source(
            title="固体矿产资源量估算规程 第1部分：通则",
            standard_no="DZ/T 0338.1-2020",
            chapter="6.2.2.1",
            quote=(
                "普查阶段矿体的圈连可用实际工程间距的1/4平推处理。"
                "b)无限外推：见矿工程向外再没有工程控制时，允许以矿体产出特征结合拟推的资源量类型的经验工程间距1/2尖推。"
            ),
            source_type="local_kb",
            text_access="ocr_text",
        )

        answer = agent._fast_answer("资源量估算中，无限外推是推1/2还是1/4", [source]) or ""

        self.assertIn("1/2 尖推", answer)
        self.assertIn("不是 1/4 平推", answer)
        self.assertIn("6.2.2.1", answer)

    def test_authenticity_answer_names_mining_right_holder(self) -> None:
        agent = object.__new__(MiningQAAgent)
        source = Source(
            title="中华人民共和国矿产资源法实施条例",
            standard_no="国令第839号",
            chapter="第四十三条",
            quote="矿业权人应当对其报送的储量报告的真实性负责，不得弄虚作假。",
            source_type="official_fulltext",
            text_access="html_text",
        )

        answer = agent._fast_answer("资源储量报告的真实性由谁负责", [source]) or ""

        self.assertIn("矿业权人负责", answer)
        self.assertNotIn("许可证颁发层级", answer)


if __name__ == "__main__":
    unittest.main()
