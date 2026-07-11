import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mining_qa.agent import MiningQAAgent
from mining_qa.config import Settings
from mining_qa.evidence_reranker import EvidenceReranker
from mining_qa.knowledge_store import KnowledgeStore, VectorCandidateResult, connect, table_quote, table_references
from mining_qa.query_understanding import (
    apply_semantic_plan,
    contextualize_follow_up,
    query_plan_from_payload,
    understand_query,
)
from mining_qa.schemas import Source
from mining_qa.retrieval_planner import RetrievalPlanner


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

    def test_infinite_projection_wording_uses_the_same_relation_profile(self) -> None:
        regular = apply_semantic_plan(
            understand_query("关于矿体外推所依据的间距，不同标准是否有不同规定？"),
            None,
        )
        infinite = apply_semantic_plan(
            understand_query("关于矿体无限外推所依据的间距，不同标准是否有不同规定？"),
            None,
        )

        self.assertEqual(regular.intent, "projection_comparison")
        self.assertEqual(infinite.intent, "projection_comparison")
        self.assertEqual(regular.required_evidence_groups, infinite.required_evidence_groups)

    def test_semantic_plan_can_identify_exploration_to_mining_eligibility(self) -> None:
        plan = apply_semantic_plan(
            understand_query("哪些标准、制度规定了详查报告就可以转采"),
            {
                "canonical_query": "详查阶段地质报告作为探矿权转采矿权依据的条件",
                "intent": "exploration_to_mining_eligibility",
                "search_mode": "exhaustive",
                "subject_terms": ["详查报告", "探矿权转采矿权"],
                "required_terms": ["勘查程度", "申请采矿权"],
                "confidence": 0.92,
            },
        )

        self.assertEqual(plan.intent, "exploration_to_mining_eligibility")
        self.assertTrue(plan.planner_used)
        self.assertFalse(plan.exhaustive_search)
        self.assertIn("探矿权转采矿权", plan.retrieval_query)

    def test_exploration_to_mining_intent_has_a_deterministic_fallback(self) -> None:
        plan = apply_semantic_plan(
            understand_query("哪些标准、制度规定了详查报告就可以转采？"),
            None,
        )

        self.assertEqual(plan.intent, "exploration_to_mining_eligibility")
        self.assertFalse(plan.exhaustive_search)
        self.assertIn("探矿权转采矿权", plan.retrieval_query)
        self.assertEqual(len(plan.required_evidence_groups), 3)
        self.assertEqual(plan.scope_origin, "deterministic")
        self.assertTrue(plan.has_hard_candidate_scope)

    def test_feedback_topics_use_protected_relation_intents(self) -> None:
        cases = {
            "伴生矿产资源量类型如何确定": ("companion_resource_type", "GB/T 25283-2023", "default"),
            "岩金矿勘查类型划分因素表格": ("exploration_type_factors", "DZ/T 0205-2020", "table"),
            "铁矿勘查基本分析项目有哪些": ("basic_analysis_items", "DZ/T 0200-2020", "default"),
        }
        for question, (intent, standard_no, output_mode) in cases.items():
            with self.subTest(question=question):
                plan = understand_query(question)
                self.assertEqual(plan.intent, intent)
                self.assertIn(standard_no, plan.standard_numbers)
                self.assertEqual(plan.scope_origin, "deterministic")
                self.assertEqual(plan.output_mode, output_mode)

    def test_model_suggested_title_is_a_soft_hint(self) -> None:
        base = understand_query("某固体矿产资源分类问题")
        plan = apply_semantic_plan(
            base,
            {
                "canonical_query": base.normalized_query,
                "intent": "general",
                "candidate_titles": ["模型猜测的标准"],
                "confidence": 0.7,
            },
        )

        self.assertEqual(plan.scope_origin, "llm")
        self.assertFalse(plan.has_hard_candidate_scope)

    def test_restored_protected_plan_rejects_external_candidate_scope(self) -> None:
        question = "哪些标准、制度规定了详查报告就可以转采"
        plan = query_plan_from_payload(
            question,
            {
                "intent": "general",
                "candidate_title_terms": ["无关标准"],
                "standard_numbers": ["DZ/T 9999-2099"],
                "exhaustive_search": True,
            },
        )

        self.assertEqual(plan.intent, "exploration_to_mining_eligibility")
        self.assertNotIn("无关标准", plan.candidate_title_terms)
        self.assertNotIn("DZ/T 9999-2099", plan.standard_numbers)
        self.assertFalse(plan.exhaustive_search)

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

    def test_table_output_mode_emits_gfm_markdown(self) -> None:
        plan = understand_query("岩金矿勘查类型划分因素表格")
        table = json.dumps(
            {
                "caption": "表 E.1 矿体规模",
                "matrix": [["规模等级", "走向/m"], ["大型", ">500"], ["中型", "200~500"]],
            },
            ensure_ascii=False,
        )

        quote = table_quote(table, "", plan.normalized_query, limit=1000, plan=plan)

        self.assertIn("| 规模等级 | 走向/m |", quote)
        self.assertIn("| --- | --- |", quote)
        self.assertIn("| 大型 | >500 |", quote)

    def test_table_reference_range_is_expanded(self) -> None:
        self.assertEqual(
            table_references("矿床勘查类型划分因素见表 E.1 至表 E.5。"),
            ("E.1", "E.2", "E.3", "E.4", "E.5"),
        )


class RetrievalStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = KnowledgeStore(Path(self.temp_dir.name) / "knowledge.sqlite")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_sufficient_scoped_fts_evidence_skips_vectors(self) -> None:
        row = engineering_row()
        candidates = {}
        self.store._add_candidate(candidates, row, "full_text", 1, 1.0)  # type: ignore[arg-type]
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

    def test_llm_title_hint_does_not_apply_sql_document_scope(self) -> None:
        base = understand_query("某固体矿产资源分类问题")
        plan = apply_semantic_plan(
            base,
            {
                "canonical_query": base.normalized_query,
                "intent": "general",
                "candidate_titles": ["模型猜测的标准"],
                "confidence": 0.8,
            },
        )
        base_where = ["d.visibility in ('internal', 'public')"]
        with connect(self.store.db_path) as connection:
            where, params, document_ids = self.store._candidate_scope(connection, plan, base_where, [])

        self.assertEqual(where, base_where)
        self.assertEqual(params, [])
        self.assertEqual(document_ids, [])

    def test_dense_success_does_not_run_local_hash(self) -> None:
        dense_row = engineering_row()
        plan = understand_query(QUESTIONS[0])
        with (
            patch.object(
                self.store,
                "_dense_embedding_candidates",
                return_value=VectorCandidateResult(candidates=((dense_row, 0.7),), route="ann"),
            ),
            patch.object(self.store, "_local_hash_vector_candidates", return_value=[]) as local_hash,
        ):
            result = self.store._vector_candidates(  # type: ignore[arg-type]
                None,
                "query",
                plan,
                ["d.document_id in (?)"],
                ["doc-gold"],
                10,
            )

        local_hash.assert_not_called()
        self.assertEqual(result.route, "ann")
        self.assertEqual(result.candidates[0][0]["chunk_id"], "chunk-f1")
        self.assertAlmostEqual(result.candidates[0][1], 0.78)

    def test_dense_failure_falls_back_to_local_hash(self) -> None:
        local_row = engineering_row()
        plan = understand_query(QUESTIONS[0])
        with (
            patch.object(
                self.store,
                "_dense_embedding_candidates",
                return_value=VectorCandidateResult(error="ann_unavailable"),
            ),
            patch.object(
                self.store,
                "_local_hash_vector_candidates",
                return_value=[(local_row, 0.4)],
            ) as local_hash,
        ):
            result = self.store._vector_candidates(  # type: ignore[arg-type]
                None,
                "query",
                plan,
                ["d.document_id in (?)"],
                ["doc-gold"],
                10,
            )

        local_hash.assert_called_once()
        self.assertEqual(result.route, "local_hash")
        self.assertEqual(result.candidates[0][0]["chunk_id"], "chunk-f1")


class EvidenceRerankerTests(unittest.IsolatedAsyncioTestCase):
    async def test_projection_evidence_requires_the_relation_not_just_spacing_words(self) -> None:
        question = "关于矿体无限外推所依据的间距，不同标准是否有不同规定？"
        plan = apply_semantic_plan(understand_query(question), None)
        hits = [
            {
                "document_id": "ordinary-table",
                "title": "某矿产地质勘查规范",
                "standard_no": "DZ/T 0000-2020",
                "clause_no": "表1",
                "quote": "推荐基本工程间距为走向100 m、倾向100 m，局部可按1/2加密。",
            },
            {
                "document_id": "geometry",
                "title": "固体矿产资源量估算规程 第2部分：几何法",
                "standard_no": "DZ/T 0338.2-2020",
                "clause_no": "5.4.2",
                "quote": "有限外推时，若实际工程间距大于推断资源量工程间距，按推断资源量工程间距的1/2尖推。",
            },
            {
                "document_id": "rock-gold",
                "title": "矿产地质勘查规范 岩金",
                "standard_no": "DZ/T 0205-2020",
                "clause_no": "8.3.4.5.2",
                "quote": "有限外推按理论工程间距的1/2尖推、1/4平推；实际间距较小时按实际工程间距计算。",
            },
        ]
        settings = Settings(OPENAI_API_KEY="", EVIDENCE_RERANKER_ENABLED=True)
        result = await EvidenceReranker(settings).judge(question, plan, hits)

        self.assertTrue(result.sufficient)
        self.assertEqual(result.direct_evidence_count, 2)
        self.assertEqual({hit["document_id"] for hit in result.hits}, {"geometry", "rock-gold"})


class PlannerFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_planner_failure_preserves_a_safe_deterministic_plan(self) -> None:
        class BrokenLLM:
            enabled = True

            async def complete_json(self, messages, **kwargs):  # noqa: ANN001
                raise RuntimeError("provider unavailable")

        question = "关于矿体无限外推所依据的间距，不同标准是否有不同规定？"
        base = understand_query(question)
        settings = Settings(OPENAI_API_KEY="configured", QUERY_PLANNER_ENABLED=True)
        result = await RetrievalPlanner(settings, BrokenLLM()).plan(question, base)  # type: ignore[arg-type]

        self.assertFalse(result.used)
        self.assertEqual(result.plan.intent, "projection_comparison")
        self.assertTrue(result.plan.required_evidence_groups)
        self.assertEqual(result.error, "RuntimeError")

    async def test_protected_transfer_intent_skips_model_planning(self) -> None:
        class CountingLLM:
            enabled = True

            def __init__(self):
                self.calls = 0

            async def complete_json(self, messages, **kwargs):  # noqa: ANN001
                self.calls += 1
                raise AssertionError("protected intent must not call planner")

        question = "哪些标准、制度规定了详查报告就可以转采"
        llm = CountingLLM()
        settings = Settings(OPENAI_API_KEY="configured", QUERY_PLANNER_ENABLED=True)
        result = await RetrievalPlanner(settings, llm).plan(question, understand_query(question))  # type: ignore[arg-type]

        self.assertFalse(result.used)
        self.assertEqual(llm.calls, 0)
        self.assertEqual(result.plan.intent, "exploration_to_mining_eligibility")

    async def test_protected_transfer_intent_skips_model_reranking(self) -> None:
        plan = understand_query("哪些标准、制度规定了详查报告就可以转采")

        self.assertFalse(EvidenceReranker.needs_model(plan))


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

    def test_exploration_to_mining_answer_distinguishes_degree_and_report_type(self) -> None:
        agent = object.__new__(MiningQAAgent)
        plan = apply_semantic_plan(
            understand_query("哪些标准、制度规定了详查报告就可以转采？"),
            None,
        )
        sources = [
            Source(
                title="自然资源部关于进一步完善矿产资源勘查开采登记管理的通知",
                standard_no="自然资规〔2023〕4号",
                chapter="二、#1",
                quote=(
                    "探矿权转采矿权，应当依据经评审备案的矿产资源储量报告。"
                    "资源储量规模为大型的非煤矿山、大中型煤矿应当达到勘探程度，"
                    "其他矿山应当达到详查（含）以上程度。"
                ),
                source_type="official_fulltext",
                text_access="html_text",
            ),
            Source(
                title="固体矿产资源储量核实报告编写规范",
                standard_no="DZ/T 0430-2023",
                chapter="A.9.5",
                quote="矿产资源储量核实报告不能替代探矿权转采矿权时应提交的地质勘查报告。",
                source_type="local_kb",
                text_access="ocr_text",
            ),
        ]

        answer = agent._fast_answer("哪些标准、制度规定了详查报告就可以转采？", sources, plan) or ""

        self.assertIn("不能简单理解", answer)
        self.assertIn("经评审备案的矿产资源储量报告", answer)
        self.assertIn("详查（含）以上程度", answer)
        self.assertIn("不能替代", answer)

    def test_transfer_paraphrases_produce_the_same_answer(self) -> None:
        agent = object.__new__(MiningQAAgent)
        sources = [
            Source(
                title="自然资源部关于进一步完善矿产资源勘查开采登记管理的通知",
                standard_no="自然资规〔2023〕4号",
                chapter="二、#1",
                quote=(
                    "探矿权转采矿权，应当依据经评审备案的矿产资源储量报告。"
                    "资源储量规模为大型的非煤矿山、大中型煤矿应当达到勘探程度，"
                    "其他矿山应当达到详查（含）以上程度。"
                ),
                source_type="official_fulltext",
                text_access="html_text",
            ),
            Source(
                title="固体矿产资源储量核实报告编写规范",
                standard_no="DZ/T 0430-2023",
                chapter="A.9.5",
                quote="矿产资源储量核实报告不能替代探矿权转采矿权时应提交的地质勘查报告。",
                source_type="local_kb",
                text_access="ocr_text",
            ),
        ]
        questions = (
            "哪些标准、制度规定了详查报告就可以转采",
            "哪个标准或文件规定了，详查报告就可以转采",
            "达到详查程度后能不能申请探矿权转采矿权",
        )

        answers = [agent._fast_answer(question, sources, understand_query(question)) for question in questions]

        self.assertEqual(len(set(answers)), 1)

    def test_companion_resource_type_answer_uses_three_clause_slots(self) -> None:
        agent = object.__new__(MiningQAAgent)
        sources = [
            Source(title="矿产资源综合勘查评价规范", standard_no="GB/T 25283-2023", chapter="9.2", quote="9.2 基本分析且研究工作达到要求时，资源储量类型可与主要矿产相同。", source_type="local_kb", text_access="ocr_text"),
            Source(title="矿产资源综合勘查评价规范", standard_no="GB/T 25283-2023", chapter="9.3", quote="9.3 基本分析但未满足其他条件时，应降低资源储量类型。", source_type="local_kb", text_access="ocr_text"),
            Source(title="矿产资源综合勘查评价规范", standard_no="GB/T 25283-2023", chapter="9.4", quote="9.4 只进行组合分析而未做基本分析时，划为推断资源量。", source_type="local_kb", text_access="ocr_text"),
        ]

        answer = agent._fast_answer("伴生矿产资源量类型如何确定", sources) or ""

        self.assertIn("GB/T 25283-2023", answer)
        self.assertIn("降低资源储量类型", answer)
        self.assertIn("推断资源量", answer)

    def test_factor_table_answer_keeps_all_five_tables(self) -> None:
        agent = object.__new__(MiningQAAgent)
        sources = [
            Source(
                title="矿产地质勘查规范 岩金",
                standard_no="DZ/T 0205-2020",
                chapter=f"表 E.{number} 测试表",
                quote=f"**表 E.{number} 测试表**\n\n| 项目 | 值 |\n| --- | --- |\n| A | {number} |",
                source_type="local_kb",
                text_access="ocr_text",
            )
            for number in range(1, 6)
        ]

        answer = agent._fast_answer("岩金矿勘查类型划分因素表格", sources) or ""

        for number in range(1, 6):
            self.assertIn(f"表 E.{number}", answer)

    def test_basic_analysis_selection_drops_unrelated_standards(self) -> None:
        agent = object.__new__(MiningQAAgent)
        question = "铁矿勘查基本分析项目有哪些"
        plan = understand_query(question)
        hits = [
            {
                "document_id": "iron",
                "standard_no": "DZ/T 0200-2020",
                "title": "矿产地质勘查规范 铁、锰、铬",
                "clause_no": "6.7.2.3",
                "quote": "铁矿石基本分析项目，磁性铁矿石分析TFe、mFe，赤铁矿石分析TFe。",
                "source_type": "local_kb",
                "text_access": "ocr_text",
            },
            {
                "document_id": "bauxite",
                "standard_no": "DZ/T 0202-2020",
                "title": "矿产地质勘查规范 铝土矿",
                "clause_no": "7.7.4.3",
                "quote": "铝土矿基本分析项目。",
                "source_type": "local_kb",
                "text_access": "ocr_text",
            },
        ]

        selected = agent._select_evidence_hits(hits, question, plan)

        self.assertEqual([item["standard_no"] for item in selected], ["DZ/T 0200-2020"])


if __name__ == "__main__":
    unittest.main()
