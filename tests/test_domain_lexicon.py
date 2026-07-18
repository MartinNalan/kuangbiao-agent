import unittest

from mining_qa.domain_gate import DomainGate
from mining_qa.domain_lexicon import clear_domain_lexicon_cache, governed_domain_terms
from mining_qa.knowledge_store import domain_lexicon, lexicon_query_expansions, matched_lexicon_entries
from mining_qa.query_understanding import understand_query


REQUIRED_FIELDS = {
    "lexicon_id",
    "user_expression",
    "canonical_term",
    "intent_label",
    "domain",
    "positive_expansions",
    "negative_terms",
    "evidence_required_patterns",
    "priority",
    "status",
    "created_at",
    "updated_at",
}


class DomainLexiconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        clear_domain_lexicon_cache()

    def test_active_entries_have_complete_unique_schema(self) -> None:
        entries = domain_lexicon()
        self.assertGreaterEqual(len(entries), 23)
        self.assertEqual(len({entry["lexicon_id"] for entry in entries}), len(entries))
        self.assertEqual(len({entry["user_expression"] for entry in entries}), len(entries))
        for entry in entries:
            self.assertTrue(REQUIRED_FIELDS.issubset(entry))
            self.assertEqual(entry["status"], "active")
            self.assertIsInstance(entry["positive_expansions"], list)
            self.assertIsInstance(entry["negative_terms"], list)
            self.assertIsInstance(entry["evidence_required_patterns"], list)

    def test_new_colloquial_terms_expand_to_governed_vocabulary(self) -> None:
        cases = {
            "压矿审批怎么处理": "压覆矿产资源",
            "水工环勘查应该用哪个规范": "GB/T 12719-2021",
            "探转采需要达到什么程度": "探矿权转采矿权",
            "矿权价款需要什么缴纳材料": "矿业权出让收益（价款）",
            "共伴生资源量怎么评价": "GB/T 25283-2023",
            "工程网度怎么确定": "勘查工程间距",
            "采空区积水如何治理": "矿山采空区",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertIn(expected, lexicon_query_expansions(query))

    def test_low_ambiguity_aliases_reach_existing_deterministic_intents(self) -> None:
        cases = {
            "储量备案应该去哪个机构": "authority_responsibility",
            "关于外推距离，不同标准规定是否不一致": "projection_comparison",
            "伴生矿产资源量类型如何确定，共伴生矿产是否相同": "companion_resource_type",
            "金矿Ⅰ类型工程网度是多少": "engineering_distance_lookup",
            "探转采需要达到什么条件": "exploration_to_mining_eligibility",
            "压矿审批需要提交什么材料": "service_materials",
        }
        for query, expected_intent in cases.items():
            with self.subTest(query=query):
                self.assertEqual(understand_query(query).intent, expected_intent)

    def test_positive_expansion_does_not_trigger_an_unrelated_entry(self) -> None:
        matches = matched_lexicon_entries("岩金工程间距是多少")
        self.assertNotIn("lex-authority-background-scale", {entry["lexicon_id"] for entry in matches})

    def test_background_context_expands_retrieval_without_overriding_intent(self) -> None:
        plan = understand_query("大型金矿有哪些勘查标准")

        self.assertNotEqual(plan.intent, "background_context")
        self.assertIn("岩金", plan.retrieval_query)

    def test_retrieval_only_entry_still_contributes_expansions(self) -> None:
        entries = [
            {
                "lexicon_id": "lex-retrieval-only",
                "user_expression": "矿证",
                "canonical_term": "采矿许可证",
                "intent_label": "license_reference",
                "domain": "mining_right_registration",
                "aliases": [],
                "positive_expansions": ["采矿权"],
                "negative_terms": [],
                "evidence_required_patterns": [],
                "required_context_terms": [],
                "forbidden_context_terms": [],
                "match_type": "phrase",
                "domain_gate_enabled": False,
                "intent_trigger_enabled": False,
                "priority": 50,
                "risk_level": "low",
                "status": "active",
            }
        ]

        self.assertIn("采矿权", lexicon_query_expansions("矿证怎么办", entries=entries))
        self.assertFalse(matched_lexicon_entries("矿证怎么办", entries=entries))

    def test_no_overly_broad_single_mining_right_token_is_active(self) -> None:
        expressions = {entry["user_expression"] for entry in domain_lexicon()}
        self.assertNotIn("矿权", expressions)
        self.assertNotIn("矿产", expressions)
        self.assertNotIn("地质", expressions)

    def test_governed_colloquial_term_passes_domain_gate(self) -> None:
        decision = DomainGate().check("探转采需要达到什么条件")

        self.assertTrue(decision.in_scope)
        self.assertIn("探转采", decision.matched_terms)

    def test_goaf_terms_pass_domain_gate_without_a_generic_mining_word(self) -> None:
        questions = (
            "采空区怎么处理",
            "采空区积水如何治理",
            "老采空区稳定性怎么评价",
            "采空场需要如何监测",
            "老空区塌陷怎么防治",
        )

        for question in questions:
            with self.subTest(question=question):
                decision = DomainGate().check(question)
                self.assertTrue(decision.in_scope)
                self.assertIn("采空区", decision.matched_terms)
                self.assertEqual(understand_query(question).intent, "general")

    def test_generic_action_phrase_needs_domain_context(self) -> None:
        self.assertFalse(DomainGate().check("护照应该去哪里申请").in_scope)
        self.assertTrue(DomainGate().check("采矿证应该去哪里申请").in_scope)

    def test_generic_document_words_do_not_admit_other_domains(self) -> None:
        for question in (
            "年度财务报告怎么写？",
            "HTTP标准是什么？",
            "软件工程规范有哪些？",
        ):
            with self.subTest(question=question):
                self.assertFalse(DomainGate().check(question).in_scope)


if __name__ == "__main__":
    unittest.main()
