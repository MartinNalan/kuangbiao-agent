from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mining_qa.domain_lexicon import clear_domain_lexicon_cache, matched_lexicon_entries
from mining_qa.lexicon_governance import LexiconGovernanceStore, LexiconReviewError
from mining_qa.query_understanding import understand_query


def candidate_payload(**overrides):
    payload = {
        "target_lexicon_id": None,
        "user_expression": "矿证",
        "canonical_term": "采矿许可证",
        "intent_label": "license_reference",
        "domain": "mining_right_registration",
        "aliases": ["矿山证"],
        "positive_expansions": ["采矿许可证", "采矿权"],
        "negative_terms": ["毕业证"],
        "evidence_required_patterns": ["采矿许可证"],
        "required_context_terms": ["矿"],
        "forbidden_context_terms": ["毕业"],
        "positive_examples": ["我的矿证是省里发的"],
        "negative_examples": ["毕业证去哪里补办"],
        "match_type": "phrase",
        "domain_gate_enabled": True,
        "intent_trigger_enabled": True,
        "priority": 90,
        "risk_level": "medium",
        "status": "pending",
        "source_type": "manual",
        "source_reference": None,
        "review_note": "测试候选",
    }
    payload.update(overrides)
    return payload


class LexiconGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.runtime_path = root / "domain_lexicon_runtime.json"
        self.store = LexiconGovernanceStore(root / "application.sqlite", self.runtime_path)
        self.env = patch.dict(
            os.environ,
            {"DOMAIN_LEXICON_RUNTIME_PATH": str(self.runtime_path)},
        )
        self.env.start()
        clear_domain_lexicon_cache()

    def tearDown(self) -> None:
        self.env.stop()
        clear_domain_lexicon_cache()
        self.tempdir.cleanup()

    def test_builtin_entries_are_seeded_for_admin_governance(self) -> None:
        summary = self.store.summary()
        entries = self.store.list_entries(status="active")

        self.assertGreaterEqual(summary["active_entries"], 23)
        generic = next(entry for entry in entries if entry["user_expression"] == "去哪里申请")
        self.assertFalse(generic["domain_gate_enabled"])
        self.assertTrue(generic["required_context_terms"])
        background = next(entry for entry in entries if entry["user_expression"] == "大型金矿")
        self.assertFalse(background["intent_trigger_enabled"])

    def test_preview_compares_current_and_proposed_matching(self) -> None:
        preview = self.store.preview_candidate("我的矿证应该去哪里备案", candidate_payload())

        self.assertFalse(preview["current"]["domain_gate_passed"])
        self.assertTrue(preview["proposed"]["domain_gate_passed"])
        self.assertIn("采矿许可证", preview["proposed"]["expansions"])

    def test_generic_domain_gate_candidate_requires_context_before_approval(self) -> None:
        candidate = self.store.create_candidate(
            candidate_payload(
                user_expression="去哪办",
                required_context_terms=[],
                positive_examples=["这个业务去哪办"],
                negative_examples=["护照去哪办"],
            ),
            "admin-1",
        )

        with self.assertRaises(LexiconReviewError):
            self.store.review_candidate(candidate["candidate_id"], "approve", "批准", "admin-1")

    def test_approved_candidate_is_published_and_can_be_disabled(self) -> None:
        candidate = self.store.create_candidate(candidate_payload(), "admin-1")
        preview = self.store.preview_candidate(
            "矿证如何办理",
            candidate_payload(),
            candidate_id=candidate["candidate_id"],
            actor_user_id="admin-1",
        )
        self.assertTrue(preview["verification_passed"])
        approved = self.store.review_candidate(
            candidate["candidate_id"],
            "approve",
            "正反例已核验",
            "admin-1",
        )
        lexicon_id = approved["target_lexicon_id"]
        clear_domain_lexicon_cache()

        self.assertTrue(self.runtime_path.exists())
        self.assertTrue(matched_lexicon_entries("矿证如何办理", purpose="domain_gate"))
        plan = understand_query("矿证如何办理")
        self.assertEqual(plan.intent, "service_procedure_basis")
        self.assertNotIn("采矿许可证", plan.retrieval_query)
        self.assertNotIn("毕业证", plan.negative_terms)

        self.store.set_entry_status(lexicon_id, "disabled", "误判率过高", "admin-1")
        clear_domain_lexicon_cache()
        self.assertFalse(
            any(
                entry["lexicon_id"] == lexicon_id
                for entry in matched_lexicon_entries("矿证如何办理", purpose="domain_gate")
            )
        )

        self.store.set_entry_status(lexicon_id, "active", "完成复核", "admin-1")
        clear_domain_lexicon_cache()
        self.assertTrue(
            any(
                entry["lexicon_id"] == lexicon_id
                for entry in matched_lexicon_entries("矿证如何办理", purpose="domain_gate")
            )
        )
        self.assertGreaterEqual(len(self.store.list_audit()), 4)

    def test_approval_requires_a_current_successful_preview(self) -> None:
        candidate = self.store.create_candidate(candidate_payload(), "admin-1")

        with self.assertRaises(LexiconReviewError):
            self.store.review_candidate(candidate["candidate_id"], "approve", "直接批准", "admin-1")

        self.store.preview_candidate(
            "矿证如何办理",
            candidate_payload(),
            candidate_id=candidate["candidate_id"],
            actor_user_id="admin-1",
        )
        changed_payload = candidate_payload(canonical_term="采矿许可证件")
        self.store.update_candidate(candidate["candidate_id"], changed_payload, "admin-1")

        with self.assertRaises(LexiconReviewError):
            self.store.review_candidate(candidate["candidate_id"], "approve", "修改后直接批准", "admin-1")

    def test_failed_negative_example_preview_cannot_be_approved(self) -> None:
        payload = candidate_payload(
            user_expression="去哪办",
            aliases=[],
            required_context_terms=["矿"],
            forbidden_context_terms=[],
            positive_examples=["采矿许可证去哪办"],
            negative_examples=["矿泉水许可去哪办"],
        )
        candidate = self.store.create_candidate(payload, "admin-1")
        preview = self.store.preview_candidate(
            "采矿许可证去哪办",
            payload,
            candidate_id=candidate["candidate_id"],
            actor_user_id="admin-1",
        )

        self.assertFalse(preview["verification_passed"])
        self.assertFalse(self.store.get_candidate(candidate["candidate_id"])["preview_ready"])
        with self.assertRaises(LexiconReviewError):
            self.store.review_candidate(candidate["candidate_id"], "approve", "忽略反例", "admin-1")

    def test_admin_override_of_builtin_entry_survives_store_reinitialization(self) -> None:
        lexicon_id = "lex-authority-background-scale"
        self.store.set_entry_status(lexicon_id, "disabled", "停用背景触发", "admin-1")

        reloaded = LexiconGovernanceStore(self.store.db_path, self.runtime_path)
        entry = next(item for item in reloaded.list_entries(limit=2000) if item["lexicon_id"] == lexicon_id)

        self.assertEqual(entry["status"], "disabled")
        self.assertEqual(entry["origin"], "admin_override")


if __name__ == "__main__":
    unittest.main()
