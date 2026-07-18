from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from mining_qa.config import Settings
from mining_qa.research import (
    ResearchAnalyzer,
    ResearchPlan,
    ResearchPlanner,
    ResearchTaskRunner,
    _strip_out_of_scope_projection,
)
from mining_qa.schemas import Source


class FakeResearchLLM:
    enabled = True

    async def complete_json(self, messages, *, max_tokens=None):
        return json.dumps(
            {
                "facts": [
                    {
                        "document_id": "DZ/T 9999-2020",
                        "classification": "stricter",
                        "dimension": "尖推和平推比例",
                        "finding": "仅允许1/2尖推，未采用平推。",
                        "source_indices": ["1"],
                    }
                ]
            },
            ensure_ascii=False,
        )


class GenericPlannerLLM:
    enabled = True

    async def complete_json(self, messages, *, max_tokens=None):
        return json.dumps(
            {
                "canonical_question": "比较不同标准的无限外推规定",
                "corpus_title_terms": ["矿产地质勘查规范"],
                "document_types": ["industry_standard"],
                "comparison_dimensions": ["所依据的工程间距"],
                "evidence_queries": ["外推 工程间距 1/2 1/4"],
                "required_evidence_groups": [
                    ["外推", "尖推", "平推"],
                    ["工程间距", "基本间距"],
                    ["1/2", "1/4"],
                ],
            },
            ensure_ascii=False,
        )


class BadServicePlannerLLM:
    enabled = True

    async def complete_json(self, messages, *, max_tokens=None):
        return json.dumps(
            {
                "canonical_question": "采矿权延续申请需要提交哪些材料",
                "corpus_title_terms": ["矿产资源开采登记管理办法"],
                "document_types": ["regulation", "department_rule"],
                "comparison_dimensions": ["发证机关"],
                "evidence_queries": ["采矿许可证 发证机关"],
                "required_evidence_groups": [["采矿许可证"], ["发证机关"]],
            },
            ensure_ascii=False,
        )


class BadProjectionTargetPlannerLLM:
    enabled = True

    async def complete_json(self, messages, *, max_tokens=None):
        return json.dumps(
            {
                "canonical_question": "比较不同标准的无限外推规则",
                "corpus_title_terms": ["矿产地质勘查规范"],
                "document_types": ["industry_standard"],
                "comparison_dimensions": ["外推类型", "所依据的工程间距", "尖推和平推比例"],
                "evidence_queries": ["无限外推 工程间距"],
                "evidence_targets": [
                    {"label": "外推类型", "query": "无限外推 工程间距", "required": True},
                    {"label": "所依据的工程间距", "query": "1/2尖推 1/4平推", "required": True},
                    {"label": "尖推和平推比例", "query": "边缘见矿工程外", "required": True},
                ],
            },
            ensure_ascii=False,
        )


class TruncatingResearchLLM:
    enabled = True

    def __init__(self):
        self.calls = 0

    async def complete_json(self, messages, *, max_tokens=None):
        self.calls += 1
        payload = json.loads(messages[-1]["content"])
        evidence = payload["evidence"]
        if len(evidence) > 1:
            return '{"facts": ['
        item = evidence[0]
        return json.dumps(
            {
                "facts": [
                    {
                        "document_id": item["document_id"],
                        "classification": "special_provision",
                        "dimension": "无限外推规则",
                        "finding": item["quote"],
                        "source_indices": [item["source_index"]],
                    }
                ]
            },
            ensure_ascii=False,
        )


class DisabledResearchLLM:
    enabled = False


class ResearchPlannerTests(unittest.IsolatedAsyncioTestCase):
    def test_projection_fallback_uses_relation_evidence_groups(self) -> None:
        plan = ResearchPlanner._fallback("不同标准对矿体无限外推所依据的间距有何差异？")

        self.assertIn("矿产地质勘查规范", plan.corpus_title_terms)
        self.assertIn("DZ/T 0338.1-2020", plan.anchor_standard_numbers)
        self.assertIn("DZ/T 0338.2-2020", plan.anchor_standard_numbers)
        self.assertEqual(len(plan.required_evidence_groups), 4)
        self.assertIn("外推", plan.required_evidence_groups[0])
        self.assertIn("工程间距", plan.required_evidence_groups[1])
        self.assertIn("1/2", plan.required_evidence_groups[2])
        self.assertIn("无限外推", plan.required_evidence_groups[3])

    async def test_model_plan_cannot_remove_infinite_projection_scope(self) -> None:
        planner = ResearchPlanner(
            Settings(OPENAI_API_KEY="configured"),
            GenericPlannerLLM(),  # type: ignore[arg-type]
        )

        plan = await planner.plan("不同标准对矿体无限外推所依据的间距有何差异？")

        self.assertEqual(plan.intent, "projection_comparison")
        self.assertEqual(plan.strategy, "cross_document_comparison")
        self.assertIn("DZ/T 0338.1-2020", plan.anchor_standard_numbers)
        self.assertIn("DZ/T 0338.2-2020", plan.anchor_standard_numbers)
        self.assertTrue(any("无限外推" in group for group in plan.required_evidence_groups))
        geometry = {
            "title": "固体矿产资源量估算规程 第2部分：几何法",
            "standard_no": "DZ/T 0338.2-2020",
            "clause_no": "5.4.2",
            "quote": (
                "相邻的两个工程一个见矿，另一个不见矿时，采用有限外推法，"
                "若实际工程间距大于推断资源量工程间距，则按推断资源量工程间距的1/2尖推。"
            ),
        }
        self.assertTrue(ResearchTaskRunner._hit_matches_research_plan(geometry, plan))

    async def test_projection_plan_rejects_model_authored_dimension_targets(self) -> None:
        planner = ResearchPlanner(
            Settings(OPENAI_API_KEY="configured"),
            BadProjectionTargetPlannerLLM(),  # type: ignore[arg-type]
        )

        plan = await planner.plan("不同标准对矿体无限外推所依据的间距有何差异？")

        self.assertEqual(plan.intent, "projection_comparison")
        self.assertEqual(plan.evidence_targets, ())
        self.assertTrue(any("无限外推" in query for query in plan.evidence_queries))
        self.assertEqual(
            plan.comparison_dimensions,
            ("外推类型", "所依据的工程间距", "尖推和平推比例", "适用条件和例外"),
        )

    async def test_service_material_plan_cannot_drop_policy_attachment_scope(self) -> None:
        planner = ResearchPlanner(
            Settings(OPENAI_API_KEY="configured"),
            BadServicePlannerLLM(),  # type: ignore[arg-type]
        )

        plan = await planner.plan("采矿权延续申请需要提交哪些材料和要件？")

        self.assertEqual(plan.intent, "service_materials")
        self.assertEqual(plan.strategy, "document_inventory")
        self.assertIn("采矿权申请资料清单及要求", plan.corpus_title_terms)
        self.assertIn("自然资规〔2023〕4号附件4", plan.anchor_standard_numbers)
        self.assertIn("policy_attachment", plan.document_types)
        self.assertNotIn("发证机关", plan.comparison_dimensions)

    async def test_post_filing_steps_plan_cannot_become_cross_document_comparison(self) -> None:
        planner = ResearchPlanner(
            Settings(OPENAI_API_KEY="configured"),
            BadServicePlannerLLM(),  # type: ignore[arg-type]
        )

        plan = await planner.plan("资源储量评审备案后，在领取采矿证之前还需要办什么手续")

        self.assertEqual(plan.intent, "service_materials")
        self.assertEqual(plan.strategy, "document_inventory")
        self.assertEqual(plan.corpus_title_terms, ("采矿权变更（续期）登记临时服务指南",))
        self.assertEqual(plan.document_types, ("service_guide", "administrative_service_guide"))
        self.assertEqual(plan.anchor_standard_numbers, ())
        self.assertIn("矿业权出让收益", plan.required_evidence_groups[2])

    def test_direct_evidence_filter_rejects_an_ordinary_spacing_table(self) -> None:
        groups = ResearchPlanner._fallback("不同标准对矿体无限外推所依据的间距有何差异？").required_evidence_groups
        ordinary = {
            "title": "矿产地质勘查规范 岩金",
            "section_path": "表 F.1 参考基本勘查工程间距",
            "quote": "Ⅰ类型坑探穿脉80～160m，钻探走向80～160m。",
        }
        direct = {
            "title": "固体矿产资源量估算规程 第1部分：通则",
            "clause_no": "6.2.2.1",
            "quote": "无限外推允许以经验工程间距1/2尖推。",
        }
        finite_only = {
            "title": "固体矿产资源量估算规程 第2部分：几何法",
            "clause_no": "5.4.2",
            "quote": "相邻的两个工程一个见矿，另一个不见矿时，采用有限外推法，自见矿工程外推工程间距的1/2尖灭。",
        }

        self.assertFalse(ResearchTaskRunner._hit_matches_evidence_groups(ordinary, groups))
        self.assertTrue(ResearchTaskRunner._hit_matches_evidence_groups(direct, groups))
        self.assertFalse(ResearchTaskRunner._hit_matches_evidence_groups(finite_only, groups))

    def test_projection_plan_keeps_geometry_clause_as_a_labeled_contrast(self) -> None:
        plan = ResearchPlanner._fallback("不同标准对矿体无限外推所依据的间距有何差异？")
        geometry = {
            "title": "固体矿产资源量估算规程 第2部分：几何法",
            "standard_no": "DZ/T 0338.2-2020",
            "clause_no": "5.4.2",
            "quote": (
                "5.4.2 相邻的两个工程一个见矿，另一个不见矿时，采用有限外推法，"
                "若实际工程间距大于推断资源量工程间距，则按推断资源量工程间距的1/2尖推。"
            ),
        }

        self.assertTrue(ResearchTaskRunner._hit_matches_research_plan(geometry, plan))

    def test_transfer_plan_anchors_policy_and_report_limit(self) -> None:
        plan = ResearchPlanner._fallback("哪个标准或文件规定详查报告可以转采？")

        self.assertEqual(plan.intent, "exploration_to_mining_eligibility")
        self.assertEqual(plan.strategy, "relation_discovery")
        self.assertIn("自然资规〔2023〕4号", plan.anchor_standard_numbers)
        self.assertIn("DZ/T 0430-2023", plan.anchor_standard_numbers)

    def test_transfer_filter_accepts_design_basis_but_rejects_stage_only_text(self) -> None:
        plan = ResearchPlanner._fallback("哪个标准或文件规定详查报告可以转采？")
        direct = {
            "title": "矿产地质勘查规范 盐类 第1部分：总则",
            "standard_no": "DZ/T 0212.1-2020",
            "clause_no": "4.2.3",
            "quote": (
                "卤水矿及深层固体盐类矿床详查报告，经可行性研究具有工业价值，"
                "可作为矿山设计开采依据。"
            ),
        }
        ordinary = {
            "title": "矿产地质勘查规范 某矿种",
            "standard_no": "DZ/T 9999-2020",
            "clause_no": "4.2.3",
            "quote": "详查阶段应基本查明矿床地质特征，并做出是否有必要转入勘探的评价。",
        }

        self.assertTrue(ResearchTaskRunner._hit_matches_research_plan(direct, plan))
        self.assertFalse(ResearchTaskRunner._hit_matches_research_plan(ordinary, plan))

    def test_anchor_documents_are_prioritized_before_source_cap(self) -> None:
        plan = ResearchPlanner._fallback("哪个标准或文件规定详查报告可以转采？")
        documents = [
            {"document_id": f"doc-{index}", "standard_no": f"DZ/T 9{index:03d}-2020"}
            for index in range(35)
        ]
        documents.extend(
            [
                {"document_id": "policy", "standard_no": "自然资规〔2023〕4号"},
                {"document_id": "limit", "standard_no": "DZ/T 0430-2023"},
            ]
        )

        ordered = ResearchTaskRunner._prioritize_documents(documents, plan)

        self.assertEqual(
            {item["standard_no"] for item in ordered[:2]},
            {"自然资规〔2023〕4号", "DZ/T 0430-2023"},
        )

    def test_normative_reference_lists_are_not_treated_as_substantive_requirements(self) -> None:
        reference_hit = {
            "clause_no": "2",
            "page": 3,
            "quote": (
                "GB/T 17766 固体矿产资源储量分类 DZ/T 0339 矿床工业指标论证技术要求 "
                "DZ/T 0340 矿产勘查矿石加工选冶技术性能试验研究程度要求"
            ),
        }

        self.assertTrue(
            ResearchTaskRunner._hit_is_normative_reference_list(
                reference_hit,
                "不同矿种规范的选冶试验程度有哪些差异？",
            )
        )
        self.assertFalse(
            ResearchTaskRunner._hit_is_normative_reference_list(
                reference_hit,
                "哪些规范引用了 DZ/T 0340？",
            )
        )

    async def test_stage_requirement_plan_keeps_matrix_scope_after_model_planning(self) -> None:
        question = "锂矿在详查阶段，对于矿石加工选冶技术性能的要求是怎样的？"
        planner = ResearchPlanner(
            Settings(OPENAI_API_KEY="configured"),
            GenericPlannerLLM(),  # type: ignore[arg-type]
        )

        plan = await planner.plan(question)

        self.assertEqual(plan.intent, "technical_stage_requirement")
        self.assertEqual(plan.strategy, "requirements_matrix")
        self.assertEqual(plan.anchor_standard_numbers, ("DZ/T 0340-2020",))
        self.assertEqual(plan.corpus_title_terms, ("矿产勘查矿石加工选冶技术性能试验研究程度要求",))
        self.assertIn("6.4.1", plan.evidence_queries[0])

    def test_generic_requirement_questions_use_matrix_strategy(self) -> None:
        plan = ResearchPlanner._fallback("矿区开发论证的技术研究要求应达到什么程度？")

        self.assertEqual(plan.strategy, "requirements_matrix")
        self.assertFalse(plan.required_evidence_groups)

    async def test_requirement_matrix_retrieves_each_evidence_query_independently(self) -> None:
        plan = ResearchPlan(
            canonical_question="矿区开发论证的条件和技术要求是什么？",
            intent="general",
            strategy="requirements_matrix",
            document_types=("policy_document", "national_standard"),
            evidence_queries=("行政准入条件", "技术研究要求"),
        )

        class FakeKnowledge:
            async def search(self, query, filters, query_plan, **kwargs):
                document_id = filters["document_id"]
                if query == "行政准入条件" and document_id == "policy":
                    return SimpleNamespace(
                        results=[
                            {
                                "title": "管理办法",
                                "standard_no": "政策文件",
                                "clause_no": "第三条",
                                "quote": "应当符合行政准入条件。",
                                "source_type": "local_kb",
                                "text_access": "html_text",
                            }
                        ]
                    )
                if query == "技术研究要求" and document_id == "standard":
                    return SimpleNamespace(
                        results=[
                            {
                                "title": "技术规范",
                                "standard_no": "GB/T 0000-2020",
                                "clause_no": "5.2",
                                "quote": "应完成相应技术研究。",
                                "source_type": "local_kb",
                                "text_access": "ocr_text",
                            }
                        ]
                    )
                return SimpleNamespace(results=[])

        class FakeStore:
            def update_research_task(self, *args, **kwargs):
                return None

        documents = [
            {"document_id": "policy", "document_type": "policy_document"},
            {"document_id": "standard", "document_type": "national_standard"},
        ]
        sources, failures, covered = await ResearchTaskRunner()._retrieve_documents(
            FakeStore(),
            "task",
            {"retrieval_question": plan.canonical_question, "filters": {}},
            plan,
            documents,
            len(documents),
            FakeKnowledge(),
            Settings(OPENAI_API_KEY="configured"),
        )

        self.assertEqual(failures, 0)
        self.assertEqual(covered, set(plan.evidence_queries))
        self.assertEqual(set(sources), {"policy", "standard"})


class ResearchAnalyzerTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_indices_govern_internal_document_identity(self) -> None:
        source = Source(
            title="测试规范",
            standard_no="DZ/T 9999-2020",
            chapter="5.1",
            quote="无限外推允许按经验工程间距1/2尖推。",
            source_type="local_kb",
            text_access="ocr_text",
        )
        analyzer = ResearchAnalyzer(
            Settings(OPENAI_API_KEY="configured"),
            FakeResearchLLM(),  # type: ignore[arg-type]
        )
        facts = await analyzer.analyze_batch(
            "不同标准如何规定无限外推？",
            ResearchPlan(
                canonical_question="不同标准如何规定无限外推",
                comparison_dimensions=("尖推和平推比例",),
            ),
            [(1, source, "internal-document-id")],
        )

        self.assertEqual(facts[0]["document_id"], "internal-document-id")
        self.assertEqual(facts[0]["source_indices"], [1])
        self.assertEqual(facts[0]["classification"], "special_provision")
        self.assertNotIn("未采用", facts[0]["finding"])

    def test_compaction_keeps_only_fact_referenced_sources(self) -> None:
        sources = [
            Source(title="A", chapter="1", quote="A", source_type="local_kb", text_access="ocr_text"),
            Source(title="B", chapter="2", quote="B", source_type="local_kb", text_access="ocr_text"),
            Source(title="C", chapter="3", quote="C", source_type="local_kb", text_access="ocr_text"),
        ]
        facts = [
            {
                "document_id": "doc-c",
                "classification": "special_provision",
                "dimension": "条件",
                "finding": "C",
                "source_indices": [3],
            }
        ]

        compact_facts, compact_sources = ResearchTaskRunner._compact_fact_sources(facts, sources)

        self.assertEqual([source.title for source in compact_sources], ["C"])
        self.assertEqual(compact_facts[0]["source_indices"], [1])

    async def test_invalid_large_json_is_split_without_marking_direct_evidence_insufficient(self) -> None:
        llm = TruncatingResearchLLM()
        analyzer = ResearchAnalyzer(
            Settings(OPENAI_API_KEY="configured"),
            llm,  # type: ignore[arg-type]
        )
        sources = [
            (
                index,
                Source(
                    title=f"测试规范{index}",
                    standard_no=f"DZ/T 900{index}-2020",
                    chapter="6.2",
                    quote="无限外推允许按经验工程间距1/2尖推。",
                    source_type="local_kb",
                    text_access="ocr_text",
                ),
                f"doc-{index}",
            )
            for index in (1, 2)
        ]

        facts = await analyzer.analyze_batch(
            "不同标准如何规定无限外推？",
            ResearchPlan(canonical_question="比较无限外推", comparison_dimensions=("工程间距",)),
            sources,
        )

        self.assertEqual(len(facts), 2)
        self.assertTrue(all(fact["classification"] != "insufficient_evidence" for fact in facts))
        self.assertGreaterEqual(llm.calls, 4)

    def test_summary_scope_rejects_finite_projection_substitution(self) -> None:
        self.assertFalse(
            ResearchTaskRunner._summary_matches_scope(
                "不同标准如何规定无限外推？",
                "各标准对有限外推采用相同规则。",
            )
        )

    def test_summary_scope_rejects_unsupported_absence_claim(self) -> None:
        self.assertFalse(
            ResearchTaskRunner._summary_matches_scope(
                "不同标准如何规定无限外推？",
                "石灰岩规范未规定尖推。",
            )
        )

    def test_fact_scope_removes_finite_projection_contrast(self) -> None:
        finding = "铝土矿规范规定无限外推按1/2尖推或1/4平推，但有限外推采用其他比例"

        scoped = _strip_out_of_scope_projection(finding, "比较无限外推规定")

        self.assertIn("无限外推", scoped)
        self.assertNotIn("有限外推", scoped)

    def test_finite_projection_facts_exclude_infinite_rules_and_split_partial_mineralization(self) -> None:
        sources = [
            (
                1,
                Source(
                    title="几何法",
                    standard_no="DZ/T 0338.2-2020",
                    chapter="5.4.2",
                    quote=(
                        "相邻的两个工程一个见矿，另一个不见矿时，采用有限外推法，"
                        "若实际工程间距大于推断资源量工程间距，则按推断资源量工程间距的1/2尖推。"
                    ),
                    source_type="local_kb",
                    text_access="ocr_text",
                ),
                "geometry",
            ),
            (
                2,
                Source(
                    title="测试规范",
                    standard_no="GB/T 13908-2020",
                    chapter="6.2",
                    quote=(
                        "相邻工程一个见矿，另一个未见矿时，按实际工程间距的1/2尖推；"
                        "部分见矿时按2/3尖推、1/3平推。"
                    ),
                    source_type="local_kb",
                    text_access="ocr_text",
                ),
                "standard",
            ),
            (
                3,
                Source(
                    title="通则",
                    standard_no="DZ/T 0338.1-2020",
                    chapter="6.2.2.1",
                    quote="见矿工程向外再没有工程控制时，采用无限外推，按经验工程间距的1/2尖推。",
                    source_type="local_kb",
                    text_access="ocr_text",
                ),
                "general",
            ),
        ]

        facts = ResearchTaskRunner._projection_facts(
            sources,
            "矿体有限外推所依据的距离，在不同标准中有哪些具体差异？",
        )

        self.assertTrue(facts)
        self.assertEqual({fact["projection_type"] for fact in facts}, {"有限外推"})
        self.assertTrue(
            any(
                fact["distance_relationship"]
                == "实际工程间距大于推断资源量工程间距时改用推断资源量工程间距"
                for fact in facts
            )
        )
        self.assertTrue(
            any(
                fact["pointed_ratio"] == "2/3" and fact["flat_ratio"] == "1/3"
                for fact in facts
            )
        )

    def test_complete_comparison_allows_irrelevant_candidates(self) -> None:
        status, missing = ResearchTaskRunner._research_final_status(
            ResearchPlan(canonical_question="比较外推", strategy="cross_document_comparison"),
            [
                {"document_id": "a", "classification": "special_provision"},
                {"document_id": "b", "classification": "special_provision"},
            ],
            candidate_truncated=False,
            failed_documents=0,
        )

        self.assertEqual(status, "completed")
        self.assertFalse(missing)

    async def test_projection_rendering_has_explicit_difference_summary(self) -> None:
        sources = [
            Source(
                title="几何法",
                standard_no="DZ/T 0338.2-2020",
                chapter="5.4.2",
                quote="x",
                source_type="local_kb",
                text_access="ocr_text",
            ),
            Source(
                title="测试规范",
                standard_no="GB/T 13908-2020",
                chapter="6.2",
                quote="x",
                source_type="local_kb",
                text_access="ocr_text",
            ),
        ]
        facts = [
            {
                "document_id": "a",
                "classification": "special_provision",
                "projection_type": "有限外推",
                "trigger_condition": "相邻工程一个见矿、另一个未见矿",
                "distance_basis": "实际工程间距大于推断资源量工程间距时改用推断资源量工程间距",
                "distance_relationship": "实际工程间距大于推断资源量工程间距时改用推断资源量工程间距",
                "pointed_ratio": "1/2",
                "flat_ratio": "1/4",
                "adjacent_engineering_condition": "相邻工程一个见矿、另一个未见矿",
                "source_indices": [1],
            },
            {
                "document_id": "b",
                "classification": "special_provision",
                "projection_type": "有限外推",
                "trigger_condition": "相邻工程部分见矿",
                "distance_basis": "实际工程间距",
                "distance_relationship": None,
                "pointed_ratio": "2/3",
                "flat_ratio": "1/3",
                "adjacent_engineering_condition": "相邻工程部分见矿",
                "source_indices": [2],
            },
        ]

        answer = ResearchTaskRunner._render_projection_comparison("有限外推差异", facts, sources)

        self.assertIn("改按推断资源量工程间距计算外推距离", answer)
        self.assertIn("2/3 尖推、1/3 平推", answer)
        self.assertIn("| 外推类型 |", answer)

    async def test_answer_keeps_table_without_repeating_direct_evidence_list(self) -> None:
        source = Source(
            title="测试规范",
            standard_no="DZ/T 9999-2020",
            chapter="6.1",
            quote="勘探阶段应开展实验室流程试验。",
            source_type="local_kb",
            text_access="ocr_text",
        )
        answer = await ResearchTaskRunner()._render_answer(
            "不同规范的试验程度有何差异？",
            ResearchPlan(canonical_question="比较试验程度", comparison_dimensions=("试验程度",)),
            [
                {
                    "document_id": "doc-1",
                    "classification": "special_provision",
                    "dimension": "试验程度",
                    "finding": "勘探阶段应开展实验室流程试验。",
                    "source_indices": [1],
                }
            ],
            [source],
            DisabledResearchLLM(),  # type: ignore[arg-type]
            Settings(),
        )

        self.assertIn("**对比结果**", answer)
        self.assertIn("| 文件 | 判定 | 比较维度 | 具体发现 | 依据条款 |", answer)
        self.assertNotIn("**代表性直接依据**", answer)
        self.assertNotIn("工程间距、外推比例", answer)

    async def test_stage_requirement_rendering_uses_a_condition_matrix(self) -> None:
        question = "锂矿在详查阶段，对于矿石加工选冶技术性能的要求是怎样的？"
        sources = [
            Source(title="矿产勘查矿石加工选冶技术性能试验研究程度要求", standard_no="DZ/T 0340-2020", chapter="6.4.1", quote="6.4.1 小型资源量规模易选矿石，在工艺矿物学基本研究的基础上，进行类比研究。", source_type="local_kb", text_access="ocr_text"),
            Source(title="矿产勘查矿石加工选冶技术性能试验研究程度要求", standard_no="DZ/T 0340-2020", chapter="6.4.2", quote="6.4.2 大中型资源量规模易选矿石或中小型资源量规模较易选矿石，在工艺矿物学基本研究的基础上，进行可选性试验。", source_type="local_kb", text_access="ocr_text"),
            Source(title="矿产勘查矿石加工选冶技术性能试验研究程度要求", standard_no="DZ/T 0340-2020", chapter="6.4.3", quote="6.4.3 大型资源量规模较易选矿石或中小型资源量规模难选矿石，在工艺矿物学基本研究的基础上，进行实验室流程试验。", source_type="local_kb", text_access="ocr_text"),
            Source(title="矿产勘查矿石加工选冶技术性能试验研究程度要求", standard_no="DZ/T 0340-2020", chapter="6.4.4", quote="6.4.4 大型资源量规模难选矿石，在工艺矿物学详细研究的基础上，进行实验室流程试验。", source_type="local_kb", text_access="ocr_text"),
        ]
        answer = await ResearchTaskRunner()._render_answer(
            question,
            ResearchPlanner._fallback(question),
            [],
            sources,
            DisabledResearchLLM(),  # type: ignore[arg-type]
            Settings(),
        )

        self.assertIn("| 资源量规模与矿石类型 | 试验研究要求 | 依据条款 |", answer)
        self.assertIn("6.4.1", answer)
        self.assertIn("6.4.4", answer)
        self.assertNotIn("工程间距", answer)
        self.assertNotIn("外推比例", answer)

    async def test_transfer_answer_uses_relation_sections_without_internal_ids_or_table(self) -> None:
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
                source_type="local_kb",
                text_access="html_text",
            ),
            Source(
                title="矿产地质勘查规范 盐类 第1部分：总则",
                standard_no="DZ/T 0212.1-2020",
                chapter="4.2.3",
                quote=(
                    "卤水矿及深层固体盐类矿床详查报告，经可行性研究具有工业价值，"
                    "可作为矿山设计开采依据。"
                ),
                source_type="local_kb",
                text_access="ocr_text",
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

        answer = await ResearchTaskRunner()._render_answer(
            "哪个标准或文件规定详查报告可以转采？",
            ResearchPlanner._fallback("哪个标准或文件规定详查报告可以转采？"),
            [],
            sources,
            DisabledResearchLLM(),  # type: ignore[arg-type]
            Settings(),
        )

        self.assertIn("**一般转采规定**", answer)
        self.assertIn("**分矿种特殊规定**", answer)
        self.assertIn("**报告类型限制**", answer)
        self.assertNotIn("| 文件 |", answer)
        self.assertNotIn("compilation_", answer)

    def test_service_material_answer_lists_attachment_rows_in_sequence(self) -> None:
        sources = [
            Source(
                title="采矿权申请资料清单及要求",
                standard_no="自然资规〔2023〕4号附件4",
                chapter="附件4 > 延续 > 材料 4",
                quote="采矿权延续申请材料第4项：采矿许可证正、副本。",
                source_type="official_fulltext",
                text_access="html_text",
                source_role="policy_attachment",
            ),
            Source(
                title="采矿权申请资料清单及要求",
                standard_no="自然资规〔2023〕4号附件4",
                chapter="附件4 > 延续 > 材料 1",
                quote="采矿权延续申请材料第1项：采矿权申请登记书或申请书。",
                source_type="official_fulltext",
                text_access="html_text",
                source_role="policy_attachment",
            ),
        ]

        answer = ResearchTaskRunner._render_service_material_answer(
            ResearchPlanner._fallback("采矿权延续申请需要提交哪些材料和要件？"),
            sources,
        )

        self.assertIn("自然资规〔2023〕4号附件4", answer)
        self.assertLess(answer.index("第1项"), answer.index("第4项"))
        self.assertNotIn("发证机关", answer)

    def test_post_filing_research_answer_converts_material_to_procedure(self) -> None:
        question = "资源储量评审备案后，在领取采矿证之前还需要办什么手续"
        source = Source(
            title="采矿权变更（续期）登记临时服务指南",
            chapter="申请材料 > 申请材料目录",
            quote=(
                "申请材料目录 1.采矿权登记申请书 2.申请人的企业法人营业执照副本 "
                "3.不动产权证书（采矿权）或采矿许可证 4.矿产资源储量评审备案文件 "
                "5.矿业权出让收益（价款）缴纳或有偿处置证明材料。"
            ),
            source_type="official_fulltext",
            text_access="html_text",
            url="https://www.mnr.gov.cn/bsznxxk/fwzn/202507/t20250729_2895981.html",
            source_role="service_guide",
        )

        answer = ResearchTaskRunner._render_service_material_answer(
            ResearchPlanner._fallback(question),
            [source],
        )

        self.assertIn("以下 5 项", answer)
        self.assertIn("完成矿业权出让收益（价款）缴纳或有偿处置", answer)
        self.assertIn("2895981.html", answer)
        self.assertNotIn("没有命中", answer)


if __name__ == "__main__":
    unittest.main()
