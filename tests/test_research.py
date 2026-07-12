from __future__ import annotations

import json
import unittest

from mining_qa.config import Settings
from mining_qa.research import (
    ResearchAnalyzer,
    ResearchPlan,
    ResearchPlanner,
    ResearchTaskRunner,
    _concise_research_quote,
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


class ResearchPlannerTests(unittest.IsolatedAsyncioTestCase):
    def test_projection_fallback_uses_relation_evidence_groups(self) -> None:
        plan = ResearchPlanner._fallback("不同标准对矿体无限外推所依据的间距有何差异？")

        self.assertIn("矿产地质勘查规范", plan.corpus_title_terms)
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

        self.assertTrue(any("无限外推" in group for group in plan.required_evidence_groups))

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

    def test_representative_quote_keeps_the_direct_infinite_projection_sentence(self) -> None:
        quote = (
            "相邻工程未见矿时，按实际工程间距1/2尖推。"
            "边缘见矿工程外一般按推断资源量勘查工程间距1/2尖推或1/4平推。"
        )

        concise = _concise_research_quote(quote, "不同标准的无限外推有何差异？")

        self.assertIn("边缘见矿工程外", concise)
        self.assertNotIn("相邻工程未见矿", concise)

    def test_fact_scope_removes_finite_projection_contrast(self) -> None:
        finding = "铝土矿规范规定无限外推按1/2尖推或1/4平推，但有限外推采用其他比例"

        scoped = _strip_out_of_scope_projection(finding, "比较无限外推规定")

        self.assertIn("无限外推", scoped)
        self.assertNotIn("有限外推", scoped)


if __name__ == "__main__":
    unittest.main()
