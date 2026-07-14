import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mining_qa.agent import MiningQAAgent
from mining_qa.config import Settings
from mining_qa.evidence_reranker import EvidenceReranker, RerankResult
from mining_qa.knowledge_store import (
    KnowledgeStore,
    VectorCandidateResult,
    authority_evidence_quote,
    connect,
    table_quote,
    table_references,
    transfer_evidence_quote,
)
from mining_qa.query_understanding import (
    PROTECTED_QUERY_INTENTS,
    apply_semantic_plan,
    contextualize_follow_up,
    query_plan_from_payload,
    understand_query,
)
from mining_qa.schemas import Source
from mining_qa.retrieval_planner import QueryVariant, RetrievalPlanner


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
        self.assertEqual(plan.search_mode, "comparison")
        self.assertEqual(plan.scope_origin, "none")
        self.assertFalse(plan.has_hard_candidate_scope)
        self.assertIn("自然资规〔2023〕4号", plan.retrieval_query)
        self.assertIn("DZ/T 0430-2023", plan.retrieval_query)

    def test_transfer_evidence_accepts_governed_design_basis_and_rejects_stage_definition(self) -> None:
        direct, _ = transfer_evidence_quote(
            "4.2.3 卤水矿及深层固体盐类矿床详查报告，经可行性研究具有工业价值，"
            "可作为矿山设计开采依据。"
        )
        ordinary, _ = transfer_evidence_quote(
            "4.2.3 详查阶段应基本查明矿床地质特征，并做出是否有必要转入勘探的评价。"
        )

        self.assertIn("可作为矿山设计开采依据", direct or "")
        self.assertIsNone(ordinary)

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
            "采矿权申请的前置条件及要件有哪些": ("service_materials", "自然资规〔2023〕4号"),
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

    def test_post_filing_license_steps_route_to_registration_guide(self) -> None:
        plan = understand_query("资源储量评审备案后，在领取采矿证之前还需要办什么手续")

        self.assertEqual(plan.intent, "service_materials")
        self.assertIn("采矿权变更（续期）登记临时服务指南", plan.candidate_title_terms)
        self.assertNotIn("矿产资源储量评审备案", plan.candidate_title_terms)
        self.assertIn("矿业权出让收益（价款）缴纳或有偿处置证明材料", plan.retrieval_query)

    def test_generic_mining_right_requirements_include_policy_attachment(self) -> None:
        plan = apply_semantic_plan(
            understand_query("采矿权申请的前置条件及要件有哪些"),
            None,
        )

        self.assertEqual(plan.intent, "service_materials")
        self.assertIn("采矿权申请资料清单及要求", plan.candidate_title_terms)
        self.assertIn("policy_attachment", plan.document_types)
        self.assertNotIn("采矿权延续", plan.candidate_title_terms)

    def test_context_dependent_follow_up_is_rewritten_with_previous_question(self) -> None:
        rewritten = contextualize_follow_up(
            "是否还有其他文件规定了相关内容？",
            "勘查实施方案的评审或审查是怎么规定的？",
        )

        self.assertIn("勘查实施方案的评审或审查", rewritten)
        self.assertIn("其他文件", rewritten)
        self.assertEqual(understand_query(rewritten).intent, "related_documents")
        self.assertTrue(understand_query(rewritten).exhaustive_search)

    def test_authority_roles_separate_license_issuer_from_granting_authority(self) -> None:
        plan = understand_query(
            "我现在持有的是省里发的钼矿采矿证，按照权限应该是自然资源部出让，"
            "我这种情况，应该去哪里申请资源储量评审备案？"
        )

        self.assertEqual(plan.intent, "authority_responsibility")
        self.assertEqual(plan.license_issuer_level, "province")
        self.assertEqual(plan.mining_right_granting_level, "ministry")
        self.assertEqual(plan.filing_authority, "province")
        self.assertFalse(plan.authority_role_ambiguous)

    def test_my_situation_follow_up_inherits_authority_roles(self) -> None:
        previous = (
            "我现在持有的是省里发的钼矿采矿证，按照权限应该是自然资源部出让，"
            "我这种情况，应该去哪里申请资源储量评审备案？"
        )
        current = (
            "“自然资源部负责本级已颁发勘查许可证或采矿许可证的矿产资源储量评审备案工作，"
            "其他由省级自然资源主管部门负责。”这句话是否可以理解为，我的情况需要在省里申请？"
        )
        plan = understand_query(contextualize_follow_up(current, previous))

        self.assertEqual(plan.intent, "authority_responsibility")
        self.assertEqual(plan.license_issuer_level, "province")
        self.assertEqual(plan.mining_right_granting_level, "ministry")
        self.assertEqual(plan.filing_authority, "province")

    def test_quoted_generic_authority_clause_does_not_fake_a_user_issuer(self) -> None:
        base = understand_query(
            "自然资源部负责本级已颁发勘查许可证或采矿许可证的矿产资源储量评审备案工作，"
            "其他由省级自然资源主管部门负责。这句话是否可以理解为我的情况需要在省里申请？"
        )
        plan = apply_semantic_plan(
            base,
            {
                "canonical_query": base.normalized_query,
                "intent": "authority_responsibility",
                "license_issuer_level": "province",
                "confidence": 0.99,
            },
        )

        self.assertEqual(plan.intent, "authority_responsibility")
        self.assertEqual(plan.license_issuer_level, "unknown")
        self.assertTrue(plan.authority_role_ambiguous)

    def test_definition_questions_create_protected_term_slots(self) -> None:
        compound = apply_semantic_plan(understand_query("资源储量的定义"), None)
        exact = apply_semantic_plan(understand_query("什么是证实储量？"), None)

        self.assertEqual(compound.intent, "definition_explanation")
        self.assertEqual(compound.target_terms, ("资源储量",))
        self.assertEqual(compound.definition_mode, "compound")
        self.assertEqual(compound.definition_slots, ("资源量", "储量"))
        self.assertIn("GB/T 17766-2020", compound.standard_numbers)
        self.assertEqual(exact.definition_slots, ("证实储量",))
        self.assertIn("definition_explanation", PROTECTED_QUERY_INTENTS)


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

    def test_service_material_rows_format_keeps_every_item_and_requirement(self) -> None:
        table = json.dumps(
            {
                "caption": "申请材料目录",
                "headers": ["序号", "材料名称", "要求"],
                "rows": [
                    {"序号": "1", "材料名称": "采矿权登记申请书", "要求": ""},
                    {"序号": "2", "材料名称": "企业法人营业执照副本", "要求": "登记机关在线核查。"},
                    {"序号": "3", "材料名称": "不动产权证书（采矿权）或采矿许可证", "要求": "按适用情形提交。"},
                    {"序号": "4", "材料名称": "矿产资源储量评审备案文件", "要求": "重大变化时提交。"},
                    {"序号": "5", "材料名称": "矿业权出让收益（价款）缴纳或有偿处置证明材料", "要求": "提供缴纳凭证。"},
                ],
            },
            ensure_ascii=False,
        )
        plan = understand_query("资源储量评审备案后，在领取采矿证之前还需要办什么手续")

        quote = table_quote(table, "", plan.normalized_query, limit=1400, plan=plan)

        for sequence in range(1, 6):
            self.assertIn(f"{sequence}.", quote)
        self.assertIn("矿业权出让收益（价款）缴纳或有偿处置证明材料", quote)
        self.assertIn("要求：提供缴纳凭证", quote)

    def test_table_reference_range_is_expanded(self) -> None:
        self.assertEqual(
            table_references("矿床勘查类型划分因素见表 E.1 至表 E.5。"),
            ("E.1", "E.2", "E.3", "E.4", "E.5"),
        )

    def test_authority_quote_is_extracted_from_the_full_chunk(self) -> None:
        quote, clause = authority_evidence_quote(
            "十、明确评审备案范围和权限。前置说明较长。"
            "自然资源部负责本级已颁发勘查许可证或采矿许可证的矿产资源储量评审备案工作，"
            "其他由省级自然资源主管部门负责。后续还有其他规定。"
        )

        self.assertEqual(clause, "十、")
        self.assertEqual(
            quote,
            "自然资源部负责本级已颁发勘查许可证或采矿许可证的矿产资源储量评审备案工作，"
            "其他由省级自然资源主管部门负责。",
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

    async def test_deterministic_comparison_fallback_keeps_four_documents(self) -> None:
        question = "关于矿体无限外推所依据的间距，不同标准有什么差异"
        plan = apply_semantic_plan(understand_query(question), None)
        hits = [
            {
                "document_id": f"doc-{index}",
                "title": f"标准{index}",
                "standard_no": f"DZ/T 000{index}-2020",
                "clause_no": "1.1",
                "quote": "无限外推按工程间距的1/2尖推。",
            }
            for index in range(6)
        ]

        result = await EvidenceReranker(Settings(OPENAI_API_KEY="")).judge(question, plan, hits)

        self.assertTrue(result.sufficient)
        self.assertEqual(len(result.hits), 4)
        self.assertEqual(len({hit["document_id"] for hit in result.hits}), 4)


class PlannerFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_planner_failure_preserves_a_safe_deterministic_plan(self) -> None:
        class BrokenLLM:
            enabled = True

            async def complete_json(self, messages, **kwargs):  # noqa: ANN001
                raise RuntimeError("provider unavailable")

        question = "矿体外推通常应遵循什么原则？"
        base = understand_query(question)
        settings = Settings(OPENAI_API_KEY="configured", QUERY_PLANNER_ENABLED=True)
        result = await RetrievalPlanner(settings, BrokenLLM()).plan(question, base)  # type: ignore[arg-type]

        self.assertFalse(result.used)
        self.assertEqual(result.plan.intent, "projection_rule")
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

    async def test_projection_comparison_uses_model_without_overriding_protected_intent(self) -> None:
        class PlanningLLM:
            enabled = True

            def __init__(self):
                self.calls = 0

            async def complete_json(self, messages, **kwargs):  # noqa: ANN001
                self.calls += 1
                return json.dumps(
                    {
                        "canonical_query": "不同标准矿体无限外推距离基准差异",
                        "intent": "general",
                        "search_mode": "default",
                        "subject_terms": ["矿体无限外推"],
                        "required_terms": ["工程间距"],
                        "subqueries": [
                            {"target": "推断资源量工程间距", "query": "无限外推 推断资源量工程间距"},
                            {"target": "经验工程间距", "query": "无限外推 经验工程间距"},
                        ],
                        "confidence": 0.9,
                    },
                    ensure_ascii=False,
                )

        question = "关于矿体无限外推所依据的间距，不同标准有什么差异"
        llm = PlanningLLM()
        settings = Settings(OPENAI_API_KEY="configured", QUERY_PLANNER_ENABLED=True)
        result = await RetrievalPlanner(settings, llm).plan(question, understand_query(question))  # type: ignore[arg-type]

        self.assertTrue(result.used)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(result.plan.intent, "projection_comparison")
        self.assertEqual(result.plan.search_mode, "comparison")
        self.assertTrue(result.plan.exhaustive_search)
        self.assertEqual(len(result.query_variants), 2)

    async def test_protected_transfer_intent_skips_model_reranking(self) -> None:
        plan = understand_query("哪些标准、制度规定了详查报告就可以转采")

        self.assertFalse(EvidenceReranker.needs_model(plan))


class FastAnswerTests(unittest.TestCase):
    def test_post_filing_materials_are_converted_to_actionable_steps(self) -> None:
        agent = object.__new__(MiningQAAgent)
        question = "资源储量评审备案后，在领取采矿证之前还需要办什么手续"
        source = Source(
            title="采矿权变更（续期）登记临时服务指南",
            chapter="申请材料 > 申请材料目录",
            quote=(
                "申请材料目录 1.采矿权登记申请书 2.申请人的企业法人营业执照副本，登记机关在线核查。"
                "3.不动产权证书（采矿权）或采矿许可证 4.矿产资源储量评审备案文件 "
                "5.矿业权出让收益（价款）缴纳或有偿处置证明材料。"
            ),
            source_type="official_fulltext",
            text_access="html_text",
            url="https://www.mnr.gov.cn/bsznxxk/fwzn/202507/t20250729_2895981.html",
            source_role="service_guide",
        )

        answer = agent._fast_answer(question, [source], understand_query(question)) or ""

        self.assertIn("以下 5 项", answer)
        self.assertIn("完成矿业权出让收益（价款）缴纳或有偿处置", answer)
        self.assertIn("无需另行提交", answer)
        self.assertNotIn("环境影响评价", answer)

    def test_compound_definition_uses_direct_resource_and_reserve_clauses(self) -> None:
        agent = object.__new__(MiningQAAgent)
        plan = understand_query("资源储量的定义")
        sources = [
            Source(
                title="固体矿产资源储量分类",
                standard_no="GB/T 17766-2020",
                chapter="2.7",
                quote=(
                    "2.7 资源量 mineral resources 经矿产资源勘查查明并经概略研究，"
                    "预期可经济开采的固体矿产资源。"
                ),
                source_type="local_kb",
                text_access="ocr_text",
            ),
            Source(
                title="固体矿产资源储量分类",
                standard_no="GB/T 17766-2020",
                chapter="2.12",
                quote=(
                    "2.12 储量 mineral reserves 探明资源量和(或)控制资源量中可经济采出的部分。"
                ),
                source_type="local_kb",
                text_access="ocr_text",
            ),
            Source(
                title="固体矿产资源储量分类",
                standard_no="GB/T 17766-2020",
                chapter="2.14",
                quote="2.14 证实储量 proved mineral reserves 基于探明资源量而估算的储量。",
                source_type="local_kb",
                text_access="ocr_text",
            ),
        ]

        selected = [source for source in sources if agent._definition_term_from_source(source, plan)]
        answer = agent._fast_answer("资源储量的定义", selected, plan) or ""

        self.assertIn("2.7", answer)
        self.assertIn("2.12", answer)
        self.assertNotIn("2.14", answer)
        self.assertIn("没有作为同名、独立术语", answer)

    def test_generic_mining_right_requirements_do_not_bypass_confirmation(self) -> None:
        agent = object.__new__(MiningQAAgent)
        sources = [
            Source(
                title="采矿权申请资料清单及要求",
                standard_no="自然资规〔2023〕4号附件4",
                chapter="附件4 > 适用类型",
                quote="自然资规〔2023〕4号附件4将采矿权申请资料分为新立、延续、变更、注销4种类型。",
                source_type="official_fulltext",
                text_access="pdf_text",
                source_role="policy_attachment",
            ),
            *[
                Source(
                    title="采矿权申请资料清单及要求",
                    standard_no="自然资规〔2023〕4号附件4",
                    chapter=f"附件4 > {label}",
                    quote=f"采矿权{label}申请表中共有{count}项带▲材料；要求栏的特殊规定优先于表中标记。",
                    source_type="official_fulltext",
                    text_access="pdf_text",
                    source_role="policy_attachment",
                )
                for label, count in (("新立", 14), ("延续", 10), ("变更", 50), ("注销", 6))
            ],
        ]

        answer = agent._fast_answer(
            "采矿权申请的前置条件及要件有哪些",
            sources,
            understand_query("采矿权申请的前置条件及要件有哪些"),
        ) or ""

        self.assertEqual(answer, "")
        self.assertIn(
            "application_type",
            understand_query("采矿权申请的前置条件及要件有哪些").classification.missing_slots,
        )

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

    def test_projection_comparison_classifies_experience_spacing(self) -> None:
        agent = object.__new__(MiningQAAgent)

        self.assertEqual(
            agent._projection_distance_bucket("按拟推资源量类型的经验工程间距1/2尖推"),
            "以拟推资源量类型的经验工程间距为外推依据",
        )

    def test_projection_selection_protects_reference_clause_and_prefers_infinite_focus(self) -> None:
        agent = object.__new__(MiningQAAgent)
        hits = [
            {
                "document_id": "doc-common",
                "title": "矿产地质勘查规范 稀有金属类",
                "standard_no": "DZ/T 0203-2020",
                "clause_no": "8.2.3.1",
                "quote": "8.2.3.1 有限外推原则：按实际工程间距或基本工程间距的1/2尖推。",
                "score": 0.9,
            },
            {
                "document_id": "doc-common",
                "title": "矿产地质勘查规范 稀有金属类",
                "standard_no": "DZ/T 0203-2020",
                "clause_no": "8.2.3.2",
                "quote": "8.2.3.2 无限外推原则：按基本工程间距的1/2尖推或1/4平推。",
                "score": 0.8,
            },
            {
                "document_id": "doc-geometry",
                "title": "固体矿产资源量估算规程 第2部分：几何法",
                "standard_no": "DZ/T 0338.2-2020",
                "clause_no": "5.4.2",
                "quote": (
                    "5.4.2 相邻的两个工程一个见矿，另一个不见矿时，采用有限外推法，"
                    "若实际工程间距大于推断资源量工程间距，则按推断资源量工程间距的1/2尖推。"
                ),
                "score": 0.7,
            },
        ]

        selected = agent._projection_comparison_hits(hits, "不同标准如何规定无限外推间距？")

        clauses = {(hit["standard_no"], hit["clause_no"]) for hit in selected}
        self.assertIn(("DZ/T 0338.2-2020", "5.4.2"), clauses)
        self.assertIn(("DZ/T 0203-2020", "8.2.3.2"), clauses)
        self.assertNotIn(("DZ/T 0203-2020", "8.2.3.1"), clauses)

    def test_projection_answer_labels_finite_reference_as_contrast(self) -> None:
        agent = object.__new__(MiningQAAgent)
        question = "不同标准对矿体无限外推所依据的间距有何差异？"
        sources = [
            Source(
                title="固体矿产资源量估算规程 第1部分：通则",
                standard_no="DZ/T 0338.1-2020",
                chapter="6.2.2.1",
                quote="b)无限外推：按拟推资源量类型的经验工程间距1/2尖推。",
                source_type="local_kb",
                text_access="ocr_text",
            ),
            Source(
                title="固体矿产资源量估算规程 第2部分：几何法",
                standard_no="DZ/T 0338.2-2020",
                chapter="5.4.2",
                quote=(
                    "5.4.2 相邻的两个工程一个见矿，另一个不见矿时，采用有限外推法，"
                    "若实际工程间距大于推断资源量工程间距，则按推断资源量工程间距的1/2尖推。"
                ),
                source_type="local_kb",
                text_access="ocr_text",
            ),
        ]

        answer = agent._fast_answer(question, sources, understand_query(question)) or ""

        self.assertIn("有限外推对照", answer)
        self.assertIn("不作为无限外推条款引用", answer)
        self.assertIn("DZ/T 0338.2-2020", answer)

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

    def test_authority_answer_uses_license_issuer_not_granting_authority(self) -> None:
        agent = object.__new__(MiningQAAgent)
        question = (
            "我现在持有的是省里发的钼矿采矿证，按照权限应该是自然资源部出让，"
            "我这种情况，应该去哪里申请资源储量评审备案？"
        )
        source = Source(
            title="自然资源部关于深化矿产资源管理改革若干事项的意见",
            standard_no="自然资规〔2023〕6号",
            chapter="十、",
            quote=(
                "自然资源部负责本级已颁发勘查许可证或采矿许可证的矿产资源储量评审备案工作，"
                "其他由省级自然资源主管部门负责。"
            ),
            source_type="official_fulltext",
            text_access="html_text",
        )

        answer = agent._fast_answer(question, [source], understand_query(question)) or ""

        self.assertIn("省级自然资源主管部门", answer)
        self.assertIn("出让或配置权限与储量评审备案权限不是同一概念", answer)
        self.assertNotIn("应由 **自然资源部** 负责", answer)

    def test_authority_answer_asks_for_issuer_when_user_only_gives_mine_scale(self) -> None:
        agent = object.__new__(MiningQAAgent)
        question = "我是一个大型的金矿，我的储量报告评审应该去哪个机构"
        source = Source(
            title="自然资源部关于深化矿产资源管理改革若干事项的意见",
            standard_no="自然资规〔2023〕6号",
            chapter="十、",
            quote=(
                "自然资源部负责本级已颁发勘查许可证或采矿许可证的矿产资源储量评审备案工作，"
                "其他由省级自然资源主管部门负责。"
            ),
            source_type="official_fulltext",
            text_access="html_text",
        )

        answer = agent._fast_answer(question, [source], understand_query(question)) or ""

        self.assertIn("许可证的 **颁发机关**", answer)
        self.assertIn("不是仅按矿种、矿山规模判断", answer)


class ControlledRetrievalEnhancementTests(unittest.TestCase):
    def test_supplemental_plans_keep_one_refined_query_and_one_targeted_variant(self) -> None:
        agent = object.__new__(MiningQAAgent)
        agent.settings = Settings(
            CONTROLLED_MULTI_QUERY_ENABLED=True,
            CONTROLLED_MULTI_QUERY_MAX=2,
        )
        plan = apply_semantic_plan(
            understand_query("不同标准对矿体无限外推所依据的间距有什么差异"),
            None,
        )
        result = RerankResult(
            hits=(),
            sufficient=False,
            used=True,
            elapsed_ms=1.0,
            direct_evidence_count=0,
            refined_query="矿体无限外推 工程间距 距离基准",
        )
        variants = (
            QueryVariant(target="推断资源量工程间距", query="无限外推 推断资源量工程间距"),
            QueryVariant(target="经验工程间距", query="无限外推 经验工程间距"),
        )

        supplemental = agent._supplemental_plans(plan, variants, result)

        self.assertEqual(len(supplemental), 2)
        self.assertEqual([is_multi for _, is_multi in supplemental], [False, True])
        self.assertTrue(all(item.intent == "projection_comparison" for item, _ in supplemental))

    def test_mmr_only_runs_after_same_document_trigger(self) -> None:
        store = object.__new__(KnowledgeStore)
        store.db_path = Path("/tmp/not-used.sqlite")
        plan = understand_query("不同制度对资源储量管理有什么差异")
        candidates = []
        for index, document_id in enumerate(("same", "same", "same", "same", "other-a", "other-b")):
            row = {
                "chunk_id": f"chunk-{index}",
                "document_id": document_id,
                "title": f"文件{document_id}",
                "section_path": f"第{index}条",
                "clause_no": str(index),
                "text": "资源储量管理规定" if document_id == "same" else f"独立规定{document_id}",
            }
            candidates.append((index, {"row": row, "final_score": 0.99 - index * 0.04}))

        with (
            patch("mining_qa.knowledge_store.get_settings", return_value=Settings(MMR_ENABLED=True)),
            patch.object(store, "_candidate_vectors", return_value={}),
        ):
            reranked, stats = store._apply_mmr(candidates, plan)

        self.assertTrue(stats["used"])
        self.assertEqual(stats["duplicate_ratio_before"], 0.8)
        self.assertLess(stats["duplicate_ratio_after"], stats["duplicate_ratio_before"])
        self.assertEqual(reranked[0][1]["row"]["chunk_id"], "chunk-0")

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

    def test_transfer_selection_rejects_equivalent_wording_without_required_conditions(self) -> None:
        agent = object.__new__(MiningQAAgent)
        question = "哪些标准、制度规定了详查报告就可以转采？"
        hits = [
            {
                "document_id": "preface",
                "title": "某矿产地质勘查规范",
                "standard_no": "DZ/T 9999-2020",
                "clause_no": "前言",
                "quote": "前言提到本次修订增加了可作为矿山设计开采依据的相关内容。",
                "score": 0.95,
            },
            {
                "document_id": "salt",
                "title": "矿产地质勘查规范 盐类 第1部分：总则",
                "standard_no": "DZ/T 0212.1-2020",
                "clause_no": "4.2.3",
                "quote": (
                    "卤水矿及深层固体盐类矿床详查报告，经可行性研究具有工业价值，"
                    "可作为矿山设计开采依据。"
                ),
                "score": 0.8,
            },
        ]

        selected = agent._select_evidence_hits(hits, question, understand_query(question))

        self.assertEqual([hit["document_id"] for hit in selected], ["salt"])

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

    def test_scoped_service_material_question_drops_unrelated_guides(self) -> None:
        agent = object.__new__(MiningQAAgent)
        question = "压矿审批需要提交什么材料"
        plan = understand_query(question)
        hits = [
            {
                "document_id": "unrelated-guide",
                "title": "勘查许可变更申请临时服务指南",
                "section_path": "申请材料",
                "quote": "申请材料目录。",
                "document_type": "service_guide",
                "source_role": "service_guide",
            }
        ]

        selected = agent._select_evidence_hits(hits, question, plan)

        self.assertEqual(selected, [])


if __name__ == "__main__":
    unittest.main()
