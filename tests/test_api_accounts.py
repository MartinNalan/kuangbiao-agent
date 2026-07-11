from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


TEST_DATA = tempfile.TemporaryDirectory()
os.environ["APP_DB_PATH"] = os.path.join(TEST_DATA.name, "api-application.sqlite")
os.environ["AUTH_REQUIRED"] = "true"
os.environ["REGISTRATION_ENABLED"] = "true"
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["DAILY_QUOTA_DEFAULT"] = "2"
os.environ["QUOTA_TIMEZONE"] = "Asia/Shanghai"
os.environ["EMAIL_VERIFICATION_ENABLED"] = "true"
os.environ["EMAIL_VERIFICATION_SECRET"] = "api-test-verification-secret-with-sufficient-length"
os.environ["EMAIL_CODE_COOLDOWN_SECONDS"] = "0"
os.environ["EMAIL_CODE_DAILY_LIMIT"] = "10"
os.environ["EMAIL_DEBUG"] = "true"

from mining_qa.api import app  # noqa: E402
from mining_qa.auth import get_account_store  # noqa: E402
from mining_qa.schemas import AskResponse, Limitations  # noqa: E402


class FakeAnsweredAgent:
    def __init__(self, settings):
        self.settings = settings

    async def ask(self, request):
        return AskResponse(
            answer="测试答案",
            session_id=request.session_id or "generated-session",
            status="answered",
            limitations=Limitations(has_clause_level_evidence=True),
            confidence="high",
        )


class FakeOutOfScopeAgent:
    def __init__(self, settings):
        self.settings = settings

    async def ask(self, request):
        return AskResponse(
            answer="该问题不属于地质领域标准规范问答范围。",
            session_id=request.session_id or "generated-session",
            status="out_of_scope",
            confidence="high",
        )


class FailingAgent:
    def __init__(self, settings):
        self.settings = settings

    async def ask(self, request):
        raise RuntimeError("simulated system failure")


class EchoRetrievalQuestionAgent:
    def __init__(self, settings):
        self.settings = settings

    async def ask(self, request):
        return AskResponse(
            answer=request.retrieval_question,
            session_id=request.session_id or "generated-session",
            status="answered",
            limitations=Limitations(has_clause_level_evidence=True),
            confidence="high",
        )


class ApiAccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.store = get_account_store()
        cls.admin = cls.store.create_user(
            "admin-api-test",
            "admin-password-2026",
            "测试管理员",
            10,
            role="admin",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        TEST_DATA.cleanup()

    def setUp(self) -> None:
        self.client = TestClient(app, raise_server_exceptions=False)

    def create_invite(self, label: str = "API test invitation") -> str:
        _, invite = self.store.create_invitation(
            self.admin["user_id"],
            label,
            max_uses=1,
            expires_in_days=30,
        )
        return invite

    def register(self, client: TestClient, email: str) -> dict:
        invite = self.create_invite(email)
        sent = client.post(
            "/api/auth/email-code",
            json={"email": email, "invite_code": invite},
        )
        self.assertEqual(sent.status_code, 200, sent.text)
        code = sent.json()["debug_code"]
        registration = client.post(
            "/api/auth/register",
            json={
                "email": email,
                "display_name": "API 测试用户",
                "password": "api-user-password-2026",
                "invite_code": invite,
                "email_code": code,
            },
        )
        self.assertEqual(registration.status_code, 200, registration.text)
        return registration.json()["user"]

    def test_registration_login_and_shared_web_api_quota(self) -> None:
        user = self.register(self.client, "api-user@example.com")
        self.assertTrue(user["email_verified"])
        self.assertEqual(user["daily_limit"], 2)

        created_key = self.client.post("/api/account/api-keys", json={"name": "integration test"})
        self.assertEqual(created_key.status_code, 200, created_key.text)
        api_key = created_key.json()["api_key"]
        self.assertTrue(api_key.startswith("kb_live_"))

        with patch("mining_qa.api.MiningQAAgent", FakeAnsweredAgent):
            answered = self.client.post("/api/ask", json={"question": "金矿工程间距是多少？"})
        self.assertEqual(answered.status_code, 200, answered.text)
        self.assertTrue(answered.json()["quota"]["consumed"])
        self.assertEqual(answered.json()["quota"]["remaining"], 1)

        with patch("mining_qa.api.MiningQAAgent", FakeOutOfScopeAgent):
            rejected = self.client.post(
                "/api/ask",
                headers={"X-API-Key": api_key},
                json={"question": "1+1等于几？"},
            )
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(rejected.json()["status"], "out_of_scope")
        self.assertFalse(rejected.json()["quota"]["consumed"])
        self.assertEqual(rejected.json()["quota"]["remaining"], 1)

        with patch("mining_qa.api.MiningQAAgent", FakeAnsweredAgent):
            second_answer = self.client.post("/api/ask", json={"question": "再问一个地质问题"})
        self.assertEqual(second_answer.status_code, 200, second_answer.text)
        self.assertTrue(second_answer.json()["quota"]["consumed"])
        self.assertEqual(second_answer.json()["quota"]["remaining"], 0)

        exhausted = self.client.post("/api/ask", json={"question": "第三个地质问题"})
        self.assertEqual(exhausted.status_code, 429, exhausted.text)
        self.assertEqual(exhausted.json()["detail"]["code"], "DAILY_QUOTA_EXCEEDED")

        usage = self.client.get("/api/usage", headers={"X-API-Key": api_key})
        self.assertEqual(usage.status_code, 200, usage.text)
        self.assertEqual(usage.json()["usage"]["quota"]["used"], 2)

    def test_follow_up_uses_previous_user_question_before_domain_gate(self) -> None:
        client = TestClient(app, raise_server_exceptions=False)
        self.register(client, "follow-up@example.com")
        with patch("mining_qa.api.MiningQAAgent", EchoRetrievalQuestionAgent):
            first = client.post(
                "/api/ask",
                json={"question": "勘查实施方案的评审或审查是怎么规定的？"},
            )
            second = client.post(
                "/api/ask",
                json={
                    "question": "是否还有其他文件规定了相关内容？",
                    "session_id": first.json()["session_id"],
                },
            )

        self.assertEqual(second.status_code, 200, second.text)
        self.assertIn("勘查实施方案的评审或审查是怎么规定的", second.json()["answer"])
        self.assertIn("是否还有其他文件规定了相关内容", second.json()["answer"])

    def test_feedback_enters_admin_triage_queue(self) -> None:
        user_client = TestClient(app, raise_server_exceptions=False)
        self.register(user_client, "feedback-api@example.com")
        with patch("mining_qa.api.MiningQAAgent", FakeAnsweredAgent):
            answer = user_client.post("/api/ask", json={"question": "采矿证延续需要什么材料？"})
        payload = answer.json()
        submitted = user_client.post(
            "/api/feedback",
            json={
                "session_id": payload["session_id"],
                "request_id": payload["request_id"],
                "rating": "unsatisfied",
                "reason": "missing_evidence",
                "comment": "缺少附件材料清单",
            },
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(submitted.json()["review_lane"], "kb_review")
        self.assertEqual(submitted.json()["status"], "open")

        login = self.client.post(
            "/api/auth/login",
            json={"account": "admin-api-test", "password": "admin-password-2026"},
        )
        self.assertEqual(login.status_code, 200, login.text)
        queue = self.client.get("/api/admin/feedback?status=open")
        self.assertEqual(queue.status_code, 200, queue.text)
        item = next(row for row in queue.json()["items"] if row["feedback_id"] == submitted.json()["feedback_id"])
        self.assertEqual(item["question"], "采矿证延续需要什么材料？")
        resolved = self.client.post(
            f"/api/admin/feedback/{item['feedback_id']}/status",
            json={"status": "resolved", "resolution_note": "已发布补库任务"},
        )
        self.assertEqual(resolved.status_code, 200, resolved.text)
        self.assertEqual(resolved.json()["item"]["status"], "resolved")

    def test_wrong_email_code_is_rejected(self) -> None:
        invite = self.create_invite("wrong email code")
        sent = self.client.post(
            "/api/auth/email-code",
            json={"email": "wrong-api-code@example.com", "invite_code": invite},
        )
        self.assertEqual(sent.status_code, 200, sent.text)
        registration = self.client.post(
            "/api/auth/register",
            json={
                "email": "wrong-api-code@example.com",
                "display_name": "验证码错误",
                "password": "api-user-password-2026",
                "invite_code": invite,
                "email_code": "000000",
            },
        )
        self.assertEqual(registration.status_code, 400, registration.text)
        self.assertEqual(registration.json()["detail"]["code"], "INVALID_EMAIL_CODE")

    def test_system_error_refunds_reserved_quota(self) -> None:
        user = self.register(self.client, "refund@example.com")
        with patch("mining_qa.api.MiningQAAgent", FailingAgent):
            failed = self.client.post("/api/ask", json={"question": "触发系统异常"})
        self.assertEqual(failed.status_code, 500, failed.text)
        summary = self.store.account_summary(user["user_id"], "Asia/Shanghai")
        self.assertEqual(summary["quota"]["used"], 0)
        self.assertEqual(summary["quota"]["reserved"], 0)
        self.assertEqual(summary["quota"]["remaining"], 2)

    def test_admin_can_set_limit_and_add_today_quota(self) -> None:
        user_client = TestClient(app, raise_server_exceptions=False)
        user = self.register(user_client, "admin-target@example.com")
        login = self.client.post(
            "/api/auth/login",
            json={"account": "admin-api-test", "password": "admin-password-2026"},
        )
        self.assertEqual(login.status_code, 200, login.text)

        changed = self.client.post(
            f"/api/admin/users/{user['user_id']}/daily-limit",
            json={"daily_limit": 20, "reason": "扩大测试范围"},
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        added = self.client.post(
            f"/api/admin/users/{user['user_id']}/quota",
            json={"extra_requests": 5, "reason": "专项测试"},
        )
        self.assertEqual(added.status_code, 200, added.text)
        self.assertEqual(added.json()["quota"]["effective_limit"], 25)

        users = self.client.get("/api/admin/users")
        self.assertEqual(users.status_code, 200, users.text)
        target = next(item for item in users.json()["items"] if item["user_id"] == user["user_id"])
        self.assertEqual(target["quota"]["remaining"], 25)

    def test_email_code_requires_a_valid_invitation(self) -> None:
        response = self.client.post(
            "/api/auth/email-code",
            json={"email": "no-invite@example.com", "invite_code": "KB-INVALID-CODE"},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"]["code"], "INVALID_INVITE")

    def test_unauthenticated_me_is_a_clean_status_response(self) -> None:
        response = self.client.get("/api/auth/me")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {"authenticated": False, "user": None, "registration_enabled": True},
        )


if __name__ == "__main__":
    unittest.main()
