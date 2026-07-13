from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mining_qa.account_store import (
    ActiveResearchTaskError,
    AccountStore,
    DailyQuotaExceededError,
    EmailCodeCooldownError,
    EmailCodeDailyLimitError,
    InvalidCredentialsError,
    InvalidEmailCodeError,
    hash_password,
    verify_password,
)


VERIFICATION_SECRET = "test-verification-secret-with-sufficient-length"


class PasswordTests(unittest.TestCase):
    def test_scrypt_password_round_trip(self) -> None:
        encoded = hash_password("correct-horse-2026")
        self.assertTrue(verify_password("correct-horse-2026", encoded))
        self.assertFalse(verify_password("wrong-password", encoded))


class AccountStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "application.sqlite"
        self.store = AccountStore(self.db_path)
        self.admin = self.store.create_user(
            "admin",
            "admin-password-2026",
            "管理员",
            10,
            role="admin",
        )
        self.user = self._register_user("tester@example.com", daily_limit=10)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _register_user(self, email: str, daily_limit: int = 10) -> dict:
        _, invite_code = self.store.create_invitation(
            self.admin["user_id"],
            f"Invite for {email}",
            max_uses=1,
            expires_in_days=30,
        )
        code = "123456"
        self.store.create_email_verification(
            email,
            code,
            VERIFICATION_SECRET,
            ttl_minutes=10,
            cooldown_seconds=0,
            daily_send_limit=5,
            request_ip="127.0.0.1",
        )
        return self.store.register_user(
            email,
            "tester-password-2026",
            "测试用户",
            invite_code,
            code,
            daily_limit,
            VERIFICATION_SECRET,
        )

    def test_authentication_session_and_api_key(self) -> None:
        authenticated = self.store.authenticate_user("TESTER@example.com", "tester-password-2026")
        self.assertEqual(authenticated["user_id"], self.user["user_id"])
        self.assertTrue(authenticated["email_verified"])
        with self.assertRaises(InvalidCredentialsError):
            self.store.authenticate_user("tester@example.com", "not-the-password")

        _, session_token = self.store.create_session(self.user["user_id"], 24)
        session_user = self.store.authenticate_session(session_token)
        self.assertIsNotNone(session_user)
        self.assertEqual(session_user["user_id"], self.user["user_id"])

        key_record, plain_key = self.store.create_api_key(self.user["user_id"], "CI key")
        key_user = self.store.authenticate_api_key(plain_key)
        self.assertIsNotNone(key_user)
        self.assertEqual(key_user["api_key_id"], key_record["api_key_id"])
        self.assertEqual(len(self.store.list_api_keys(self.user["user_id"])), 1)
        self.store.revoke_api_key(self.user["user_id"], key_record["api_key_id"])
        self.store.revoke_api_key(self.user["user_id"], key_record["api_key_id"])
        self.assertIsNone(self.store.authenticate_api_key(plain_key))
        self.assertEqual(self.store.list_api_keys(self.user["user_id"]), [])
        history = self.store.list_api_keys(self.user["user_id"], include_revoked=True)
        self.assertEqual(len(history), 1)
        self.assertIsNotNone(history[0]["revoked_at"])

    def test_wrong_and_expired_email_codes_are_rejected(self) -> None:
        email = "wrong-code@example.com"
        _, invite = self.store.create_invitation(self.admin["user_id"], "wrong code", 1, 30)
        self.store.create_email_verification(email, "654321", VERIFICATION_SECRET, 10, 0, 5, None)
        with self.assertRaises(InvalidEmailCodeError):
            self.store.register_user(
                email,
                "tester-password-2026",
                "错误验证码",
                invite,
                "000000",
                10,
                VERIFICATION_SECRET,
            )

        expired_email = "expired@example.com"
        _, expired_invite = self.store.create_invitation(self.admin["user_id"], "expired", 1, 30)
        self.store.create_email_verification(expired_email, "112233", VERIFICATION_SECRET, 10, 0, 5, None)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE email_verifications SET expires_at = '2000-01-01T00:00:00+00:00' WHERE email = ?",
                (expired_email,),
            )
            connection.commit()
        with self.assertRaises(InvalidEmailCodeError):
            self.store.register_user(
                expired_email,
                "tester-password-2026",
                "过期验证码",
                expired_invite,
                "112233",
                10,
                VERIFICATION_SECRET,
            )

    def test_email_code_cooldown_and_daily_limit(self) -> None:
        email = "cooldown@example.com"
        self.store.create_email_verification(email, "111111", VERIFICATION_SECRET, 10, 60, 5, None)
        with self.assertRaises(EmailCodeCooldownError):
            self.store.create_email_verification(email, "222222", VERIFICATION_SECRET, 10, 60, 5, None)

        limited_email = "limited@example.com"
        self.store.create_email_verification(limited_email, "111111", VERIFICATION_SECRET, 10, 0, 2, None)
        self.store.create_email_verification(limited_email, "222222", VERIFICATION_SECRET, 10, 0, 2, None)
        with self.assertRaises(EmailCodeDailyLimitError):
            self.store.create_email_verification(limited_email, "333333", VERIFICATION_SECRET, 10, 0, 2, None)

    def test_out_of_scope_and_system_errors_do_not_consume_quota(self) -> None:
        conversation_id = self.store.ensure_conversation(self.user["user_id"], None, "金矿工程间距是多少？")
        statuses = ["answered", "out_of_scope", "insufficient_evidence", "queued_for_enrichment"]
        expected = {
            "answered": True,
            "out_of_scope": False,
            "insufficient_evidence": True,
            "queued_for_enrichment": True,
        }
        for index, result_status in enumerate(statuses):
            request_id = f"req_{index}"
            self.store.reserve_qa_quota(
                self.user["user_id"],
                request_id,
                "web",
                None,
                conversation_id,
                11,
                "Asia/Shanghai",
            )
            settled = self.store.settle_qa_quota(request_id, result_status, 120, "Asia/Shanghai")
            self.assertEqual(settled["consumed"], expected[result_status])

        self.store.reserve_qa_quota(
            self.user["user_id"],
            "req_error",
            "api",
            None,
            conversation_id,
            11,
            "Asia/Shanghai",
        )
        refunded = self.store.settle_qa_quota("req_error", "system_error", 0, "Asia/Shanghai")
        self.assertFalse(refunded["consumed"])
        self.assertEqual(refunded["used"], 3)
        self.assertEqual(refunded["remaining"], 7)

    def test_feedback_is_classified_and_can_be_resolved(self) -> None:
        conversation_id = self.store.ensure_conversation(self.user["user_id"], None, "采矿证延续需要什么材料？")
        self.store.save_exchange(
            self.user["user_id"],
            conversation_id,
            "req_feedback",
            "采矿证延续需要什么材料？",
            "测试回答",
            {},
        )

        feedback = self.store.create_feedback(
            self.user["user_id"],
            None,
            conversation_id,
            "req_feedback",
            "unsatisfied",
            "missing_evidence",
            "缺少附件清单",
            None,
        )

        self.assertEqual(feedback["review_lane"], "kb_review")
        self.assertEqual(feedback["status"], "open")
        self.assertEqual(feedback["question"], "采矿证延续需要什么材料？")
        listed = self.store.list_feedback(status_filter="open")
        self.assertEqual(listed[0]["feedback_id"], feedback["feedback_id"])

        resolved = self.store.update_feedback_status(
            feedback["feedback_id"],
            "resolved",
            "附件已补充",
            self.admin["user_id"],
        )
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["resolved_by"], self.admin["user_id"])

    def test_latest_user_question_returns_conversation_context(self) -> None:
        conversation_id = self.store.ensure_conversation(self.user["user_id"], None, "第一问")
        self.store.save_exchange(
            self.user["user_id"],
            conversation_id,
            "req_context",
            "勘查实施方案的评审或审查是怎么规定的？",
            "测试回答",
            {},
        )
        self.assertEqual(
            self.store.latest_user_question(self.user["user_id"], conversation_id),
            "勘查实施方案的评审或审查是怎么规定的？",
        )
        self.store.save_exchange(
            self.user["user_id"],
            conversation_id,
            "req_context_second",
            "是否还有其他文件规定？",
            "第二个测试回答",
            {},
        )
        self.assertEqual(
            self.store.recent_user_questions(self.user["user_id"], conversation_id, limit=2),
            [
                "勘查实施方案的评审或审查是怎么规定的？",
                "是否还有其他文件规定？",
            ],
        )

    def test_quota_reservation_is_atomic(self) -> None:
        self.store.set_daily_limit(
            self.user["user_id"],
            5,
            "concurrency test",
            self.admin["user_id"],
            "Asia/Shanghai",
        )
        conversation_id = self.store.ensure_conversation(self.user["user_id"], None, "并发测试")

        def reserve(index: int) -> bool:
            try:
                self.store.reserve_qa_quota(
                    self.user["user_id"],
                    f"parallel_{index}",
                    "api",
                    None,
                    conversation_id,
                    4,
                    "Asia/Shanghai",
                )
                return True
            except DailyQuotaExceededError:
                return False

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(reserve, range(20)))
        self.assertEqual(sum(results), 5)
        snapshot = self.store.quota_snapshot(self.user["user_id"], "Asia/Shanghai")
        self.assertEqual(snapshot["reserved"], 5)
        self.assertEqual(snapshot["remaining"], 0)

    def test_admin_can_change_limit_and_add_daily_requests(self) -> None:
        updated = self.store.set_daily_limit(
            self.user["user_id"],
            20,
            "扩大测试范围",
            self.admin["user_id"],
            "Asia/Shanghai",
        )
        self.assertEqual(updated["daily_limit"], 20)
        quota = self.store.adjust_daily_quota(
            self.user["user_id"],
            7,
            "专项测试",
            self.admin["user_id"],
            "Asia/Shanghai",
        )
        self.assertEqual(quota["effective_limit"], 27)
        summary = self.store.account_summary(self.user["user_id"], "Asia/Shanghai")
        self.assertEqual(len(summary["adjustments"]), 2)

    def test_deep_research_reserves_and_consumes_three_quota_units(self) -> None:
        conversation_id = self.store.ensure_conversation(self.user["user_id"], None, "跨标准比较")
        reserved = self.store.reserve_qa_quota(
            self.user["user_id"],
            "req_deep_units",
            "api",
            None,
            conversation_id,
            5,
            "Asia/Shanghai",
            quota_units=3,
            request_mode="deep",
        )
        self.assertEqual(reserved["reserved"], 3)
        self.assertEqual(reserved["remaining"], 7)

        settled = self.store.settle_qa_quota(
            "req_deep_units",
            "answered",
            100,
            "Asia/Shanghai",
        )
        self.assertTrue(settled["consumed"])
        self.assertEqual(settled["consumed_units"], 3)
        self.assertEqual(settled["used"], 3)
        self.assertEqual(settled["remaining"], 7)

    def test_basic_answer_upgrade_reserves_only_two_additional_units(self) -> None:
        conversation_id = self.store.ensure_conversation(self.user["user_id"], None, "矿体外推差异")
        self.store.reserve_qa_quota(
            self.user["user_id"],
            "req_basic_upgrade",
            "web",
            None,
            conversation_id,
            6,
            "Asia/Shanghai",
        )
        self.store.settle_qa_quota("req_basic_upgrade", "answered", 30, "Asia/Shanghai")
        self.store.save_exchange(
            self.user["user_id"],
            conversation_id,
            "req_basic_upgrade",
            "矿体外推差异",
            "基本答案",
            {"mode": "basic"},
        )

        self.assertEqual(
            self.store.research_upgrade_quota_cost(
                self.user["user_id"],
                "req_basic_upgrade",
                conversation_id,
                "矿体外推差异",
            ),
            2,
        )
        self.assertEqual(
            self.store.research_upgrade_quota_cost(
                self.user["user_id"],
                "req_basic_upgrade",
                conversation_id,
                "另一个问题",
            ),
            3,
        )

    def test_only_one_active_research_task_is_allowed_per_user(self) -> None:
        conversation_id = self.store.ensure_conversation(self.user["user_id"], None, "研究任务")
        for request_id in ("req_research_one", "req_research_two"):
            self.store.reserve_qa_quota(
                self.user["user_id"],
                request_id,
                "api",
                None,
                conversation_id,
                4,
                "Asia/Shanghai",
                quota_units=3,
                request_mode="deep",
            )
        self.store.create_research_task(
            task_id="research_one",
            request_id="req_research_one",
            user_id=self.user["user_id"],
            api_key_id=None,
            conversation_id=conversation_id,
            channel="api",
            question="研究任务一",
            retrieval_question="研究任务一",
            filters={},
            reserved_quota_units=3,
        )
        with self.assertRaises(ActiveResearchTaskError):
            self.store.create_research_task(
                task_id="research_two",
                request_id="req_research_two",
                user_id=self.user["user_id"],
                api_key_id=None,
                conversation_id=conversation_id,
                channel="api",
                question="研究任务二",
                retrieval_question="研究任务二",
                filters={},
                reserved_quota_units=3,
            )


if __name__ == "__main__":
    unittest.main()
