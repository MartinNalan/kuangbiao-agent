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

    async def test_broad_goaf_schema_overrides_model_under_confirmation(self) -> None:
        llm = FakeResolutionLLM(
            {
                "canonical_question": "采空区怎么处理？",
                "intent": "general",
                "is_ambiguous": False,
                "confidence": 0.88,
                "missing_slots": [],
                "reason": "问题可以直接检索。",
                "interpretations": [],
            }
        )
        resolver = QuestionResolver(self.settings(), llm=llm)  # type: ignore[arg-type]

        result = await resolver.resolve("采空区怎么处理？")

        self.assertTrue(result.requires_clarification)
        self.assertEqual(len(result.clarification.options), 4)  # type: ignore[union-attr]
        labels = {item.label for item in result.clarification.options}  # type: ignore[union-attr]
        self.assertEqual(labels, {"稳定性评价", "积水与水害", "塌陷监测", "工程治理"})

    async def test_specific_goaf_goal_does_not_trigger_schema_confirmation(self) -> None:
        llm = FakeResolutionLLM(
            {
                "canonical_question": "采空区稳定性应如何评价？",
                "intent": "general",
                "is_ambiguous": False,
                "confidence": 0.92,
                "missing_slots": [],
                "reason": "目标已经明确。",
                "interpretations": [],
            }
        )
        resolver = QuestionResolver(self.settings(), llm=llm)  # type: ignore[arg-type]

        result = await resolver.resolve("采空区稳定性怎么评价？")

        self.assertFalse(result.requires_clarification)
        self.assertEqual(result.canonical_question, "采空区稳定性应如何评价?")

    async def test_mining_license_typo_is_replanned_before_material_schema(self) -> None:
        llm = FakeResolutionLLM(
            {
                "canonical_question": "采矿许可证办理需要什么要件？",
                "intent": "service_materials",
                "is_ambiguous": True,
                "confidence": 0.94,
                "missing_slots": ["发证机关"],
                "reason": "需要确认发证机关。",
                "interpretations": [
                    {
                        "label": "自然资源部发证",
                        "question": "自然资源部颁发的采矿许可证办理需要什么要件？",
                        "description": "按部级发证理解。",
                    },
                    {
                        "label": "省级发证",
                        "question": "省级部门颁发的采矿许可证办理需要什么要件？",
                        "description": "按省级发证理解。",
                    },
                ],
            }
        )
        resolver = QuestionResolver(self.settings(), llm=llm)  # type: ignore[arg-type]

        result = await resolver.resolve("采矿正办理需要什么要件", mode="deep")

        self.assertEqual(result.canonical_question, "采矿许可证办理需要什么要件?")
        self.assertEqual(result.plan.intent, "service_materials")
        self.assertTrue(result.requires_clarification)
        self.assertEqual(
            [item.label for item in result.clarification.options],  # type: ignore[union-attr]
            ["新立申请", "延续申请", "变更申请", "注销申请"],
        )

    async def test_material_schema_overrides_wrong_issuer_ambiguity(self) -> None:
        llm = FakeResolutionLLM(
            {
                "canonical_question": "采矿证办理需要什么要件？",
                "intent": "service_materials",
                "is_ambiguous": True,
                "confidence": 0.92,
                "missing_slots": ["发证机关"],
                "reason": "模型错误地按发证机关分叉。",
                "interpretations": [
                    {
                        "label": "部级",
                        "question": "自然资源部颁发的采矿证办理需要什么要件？",
                    },
                    {
                        "label": "省级",
                        "question": "省级部门颁发的采矿证办理需要什么要件？",
                    },
                ],
            }
        )
        resolver = QuestionResolver(self.settings(), llm=llm)  # type: ignore[arg-type]

        result = await resolver.resolve("采矿证办理需要什么要件", mode="deep")

        self.assertTrue(result.requires_clarification)
        self.assertNotIn("发证", result.clarification.reason)  # type: ignore[union-attr]
        self.assertEqual(result.clarification.options[0].question, "采矿权新立申请需要提交哪些材料和要件？")  # type: ignore[union-attr]

    async def test_recent_user_context_restores_material_intent_after_wrong_frame(self) -> None:
        llm = FakeResolutionLLM(
            {
                "canonical_question": "铜矿矿产资源储量评审备案应向哪个机关申请？",
                "intent": "authority_responsibility",
                "is_ambiguous": True,
                "confidence": 0.96,
                "missing_slots": ["许可证颁发机关"],
                "reason": "错误沿用评审备案事项。",
                "interpretations": [],
            }
        )
        resolver = QuestionResolver(self.settings(), llm=llm)  # type: ignore[arg-type]

        result = await resolver.resolve(
            "我之前的问题是我要办采矿证，和评审备案机关无关啊",
            mode="deep",
            conversation_context=(
                "采矿正办理需要什么要件",
                "采矿证办理需要什么要件",
                "我不知道哪个机关发证，我是一个铜矿",
            ),
        )

        self.assertEqual(result.canonical_question, "采矿证办理需要什么要件")
        self.assertEqual(result.plan.intent, "service_materials")
        self.assertTrue(result.requires_clarification)
        self.assertEqual(result.clarification.options[1].label, "延续申请")  # type: ignore[union-attr]

    async def test_specific_mining_right_application_runs_model_without_reasking_type(self) -> None:
        llm = FakeResolutionLLM(
            {
                "canonical_question": "采矿权延续申请需要提交哪些材料？",
                "intent": "service_materials",
                "is_ambiguous": False,
                "confidence": 0.95,
                "missing_slots": [],
                "reason": "办理类型明确。",
                "interpretations": [],
            }
        )
        resolver = QuestionResolver(self.settings(), llm=llm)  # type: ignore[arg-type]

        result = await resolver.resolve("采矿证延续需要什么材料")

        self.assertTrue(result.model_used)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(result.plan.intent, "service_materials")
        self.assertFalse(result.requires_clarification)

    async def test_post_filing_license_steps_use_first_semantic_stage_without_type_clarification(self) -> None:
        llm = FakeResolutionLLM(
            {
                "canonical_question": "矿产资源储量评审备案后，在领取采矿许可证前还需办理哪些登记手续？",
                "intent": "service_materials",
                "is_ambiguous": False,
                "confidence": 0.96,
                "missing_slots": [],
                "reason": "目标是领取采矿许可证前的登记材料和待办事项。",
                "interpretations": [],
            }
        )
        resolver = QuestionResolver(self.settings(), llm=llm)  # type: ignore[arg-type]

        result = await resolver.resolve(
            "资源储量评审备案后，在领取采矿证之前还需要办什么手续",
            mode="deep",
        )

        self.assertTrue(result.model_used)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(result.plan.intent, "service_materials")
        self.assertFalse(result.requires_clarification)
        self.assertIn("领取采矿许可证", result.canonical_question)

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
                "canonical_question": "矿山地质环境问题怎么处理？",
                "intent": "general",
                "is_ambiguous": True,
                "confidence": 0.9,
                "missing_slots": ["处理目标"],
                "reason": "存在多个方向。",
                "interpretations": [
                    {"label": "数学", "question": "1+1等于几？", "description": "领域外"},
                    {
                        "label": "环境治理",
                        "question": "矿山地质环境治理应依据哪些标准？",
                        "description": "领域内",
                    },
                ],
            }
        )
        resolver = QuestionResolver(self.settings(), llm=llm)  # type: ignore[arg-type]

        result = await resolver.resolve("矿山地质环境问题怎么处理？")

        self.assertFalse(result.requires_clarification)
        self.assertEqual(result.canonical_question, "矿山地质环境问题怎么处理?")

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
