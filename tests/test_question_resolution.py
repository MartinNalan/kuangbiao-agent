from __future__ import annotations

import json
import unittest

from mining_qa.config import Settings
from mining_qa.question_resolution import QuestionResolver


class FakeResolutionLLM:
    enabled = True

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    async def complete_json(self, messages, *, max_tokens=None):
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)

    async def aclose(self) -> None:
        return None


class FailingResolutionLLM:
    enabled = True

    async def complete_json(self, messages, *, max_tokens=None):
        raise TimeoutError("model timeout")

    async def aclose(self) -> None:
        return None


class QuestionResolverTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def settings() -> Settings:
        return Settings(
            OPENAI_API_KEY="configured",
            QUESTION_RESOLUTION_ENABLED=True,
            QUESTION_RESOLUTION_MIN_CONFIDENCE=0.55,
        )

    async def test_ambiguous_domain_question_returns_valid_options(self) -> None:
        llm = FakeResolutionLLM(
            {
                "canonical_question": "采空区怎么处理？",
                "intent": "goaf_management",
                "is_ambiguous": True,
                "confidence": 0.92,
                "missing_slots": ["处理目标"],
                "reason": "处理可能指稳定性、积水或塌陷治理。",
                "interpretations": [
                    {
                        "label": "稳定性评价",
                        "question": "采空区稳定性评价应依据哪些标准？",
                        "description": "关注稳定性评价与监测。",
                    },
                    {
                        "label": "积水治理",
                        "question": "采空区积水治理应依据哪些标准？",
                        "description": "关注积水调查与治理。",
                    },
                ],
            }
        )
        resolver = QuestionResolver(self.settings(), llm=llm)  # type: ignore[arg-type]

        result = await resolver.resolve("采空区怎么处理？")

        self.assertTrue(result.requires_clarification)
        self.assertTrue(result.model_used)
        self.assertEqual(len(result.clarification.options), 2)  # type: ignore[union-attr]
        self.assertEqual(llm.calls, 1)

    async def test_clear_engineering_distance_question_uses_fast_path(self) -> None:
        llm = FakeResolutionLLM({})
        resolver = QuestionResolver(self.settings(), llm=llm)  # type: ignore[arg-type]

        result = await resolver.resolve("金矿勘查一类型的推荐工程间距是多少？")

        self.assertFalse(result.requires_clarification)
        self.assertFalse(result.model_used)
        self.assertEqual(result.plan.intent, "engineering_distance_lookup")
        self.assertEqual(result.plan.target_exploration_type, "Ⅰ")
        self.assertEqual(llm.calls, 0)

    async def test_authority_question_without_license_issuer_requests_confirmation(self) -> None:
        llm = FakeResolutionLLM(
            {
                "canonical_question": "大型金矿的资源储量评审备案应去哪个机构？",
                "intent": "authority_responsibility",
                "is_ambiguous": True,
                "confidence": 0.94,
                "missing_slots": ["许可证颁发机关"],
                "reason": "评审备案权限取决于许可证颁发机关，而不是矿山规模。",
                "interpretations": [
                    {
                        "label": "自然资源部颁发",
                        "question": "自然资源部颁发采矿许可证的大型金矿应向哪个机构申请资源储量评审备案？",
                        "description": "许可证由自然资源部颁发。",
                    },
                    {
                        "label": "省级部门颁发",
                        "question": "省级自然资源主管部门颁发采矿许可证的大型金矿应向哪个机构申请资源储量评审备案？",
                        "description": "许可证由省级部门颁发。",
                    },
                ],
            }
        )
        resolver = QuestionResolver(self.settings(), llm=llm)  # type: ignore[arg-type]

        result = await resolver.resolve("我是一个大型的金矿，我的储量报告评审应该去哪个机构？")

        self.assertTrue(result.requires_clarification)
        self.assertEqual(len(result.clarification.options), 2)  # type: ignore[union-attr]
        self.assertEqual(llm.calls, 1)

    async def test_authority_schema_still_requests_confirmation_when_model_fails(self) -> None:
        resolver = QuestionResolver(
            self.settings(),
            llm=FailingResolutionLLM(),  # type: ignore[arg-type]
        )

        result = await resolver.resolve("大型金矿的资源储量评审备案应该去哪个机构？")

        self.assertTrue(result.requires_clarification)
        self.assertTrue(result.model_used)
        self.assertEqual(result.error, "TimeoutError")
        self.assertEqual(len(result.clarification.options), 2)  # type: ignore[union-attr]
        self.assertIn("许可证颁发机关", result.clarification.reason)  # type: ignore[union-attr]

    async def test_invalid_or_out_of_domain_options_degrade_without_confirmation(self) -> None:
        llm = FakeResolutionLLM(
            {
                "canonical_question": "采空区怎么处理？",
                "intent": "goaf_management",
                "is_ambiguous": True,
                "confidence": 0.9,
                "missing_slots": ["处理目标"],
                "reason": "存在多个方向。",
                "interpretations": [
                    {"label": "数学", "question": "1+1等于几？", "description": "领域外"},
                    {
                        "label": "稳定性",
                        "question": "采空区稳定性评价应依据哪些标准？",
                        "description": "领域内",
                    },
                ],
            }
        )
        resolver = QuestionResolver(self.settings(), llm=llm)  # type: ignore[arg-type]

        result = await resolver.resolve("采空区怎么处理？")

        self.assertFalse(result.requires_clarification)
        self.assertEqual(result.canonical_question, "采空区怎么处理?")

    async def test_model_cannot_drop_user_supplied_standard_number(self) -> None:
        llm = FakeResolutionLLM(
            {
                "canonical_question": "矿体无限外推如何规定？",
                "intent": "projection_rule",
                "is_ambiguous": True,
                "confidence": 0.95,
                "missing_slots": ["外推类型"],
                "reason": "需要确认。",
                "interpretations": [
                    {
                        "label": "无限外推",
                        "question": "矿体无限外推如何规定？",
                        "description": "未保留用户指定标准。",
                    },
                    {
                        "label": "有限外推",
                        "question": "矿体有限外推如何规定？",
                        "description": "未保留用户指定标准。",
                    },
                ],
            }
        )
        resolver = QuestionResolver(self.settings(), llm=llm)  # type: ignore[arg-type]
        question = "DZ/T 0338.1-2020 对矿体外推如何规定？"

        result = await resolver.resolve(question)

        self.assertFalse(result.requires_clarification)
        self.assertEqual(result.canonical_question, "DZ/T 0338.1-2020 对矿体外推如何规定?")
        self.assertIn("DZ/T 0338.1-2020", result.plan.standard_numbers)


if __name__ == "__main__":
    unittest.main()
