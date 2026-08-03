from __future__ import annotations

import asyncio
import json
import os
import unittest

from mining_qa.config import Settings
from mining_qa.prompt_layout import unwrap_output_schema_envelope
from mining_qa.question_resolution import QuestionResolver
from mining_qa.query_understanding import understand_query
from mining_qa.retrieval_planner import RetrievalPlanner


class CaptureLLM:
    enabled = True

    def __init__(self):
        self.messages: list[dict[str, str]] = []

    async def complete_json(self, messages, *, max_tokens=None):  # noqa: ANN001
        self.messages = messages
        return "{}"

    async def aclose(self) -> None:
        return None


class PromptLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def _resolver_messages(self, question: str, layout: str):
        llm = CaptureLLM()
        settings = Settings(
            OPENAI_API_KEY="configured",
            QUESTION_RESOLUTION_ENABLED=True,
            PROMPT_LAYOUT_VARIANT=layout,
            UNIFIED_QUERY_PLANNING_ENABLED=False,
        )
        await QuestionResolver(settings, llm=llm).resolve(question, mode="basic")  # type: ignore[arg-type]
        return llm.messages

    async def _planner_messages(self, question: str, layout: str):
        llm = CaptureLLM()
        settings = Settings(
            OPENAI_API_KEY="configured",
            PROMPT_LAYOUT_VARIANT=layout,
            UNIFIED_QUERY_PLANNING_ENABLED=False,
        )
        await RetrievalPlanner(settings, llm=llm).plan(  # type: ignore[arg-type]
            question,
            understand_query(question),
        )
        return llm.messages

    async def test_legacy_layout_retains_schema_in_dynamic_payload(self) -> None:
        for messages in (
            await self._resolver_messages("采矿权延续需要哪些材料？", "legacy"),
            await self._planner_messages("采矿权延续需要哪些材料？", "legacy"),
        ):
            self.assertNotIn("固定输出结构如下", messages[0]["content"])
            self.assertIn("output_schema", json.loads(messages[1]["content"]))

    async def test_schema_prefix_layout_moves_schema_before_intent_rule(self) -> None:
        for messages in (
            await self._resolver_messages("采矿权延续需要哪些材料？", "schema_prefix"),
            await self._planner_messages("采矿权延续需要哪些材料？", "schema_prefix"),
        ):
            system = messages[0]["content"]
            self.assertIn("固定输出结构如下", system)
            self.assertIn("顶层必须直接包含上述字段", system)
            self.assertNotIn('"output_schema":', system)
            self.assertNotIn("output_schema", json.loads(messages[1]["content"]))
            self.assertLess(system.index("固定输出结构如下"), system.index("Prompt Registry"))

    def test_schema_envelope_guard_only_unwraps_exact_legacy_mistake(self) -> None:
        actual = {"canonical_question": "金矿勘查间距是多少？"}
        self.assertEqual(
            unwrap_output_schema_envelope({"output_schema": actual}),
            actual,
        )
        with_sibling = {"output_schema": actual, "confidence": 0.9}
        self.assertIs(unwrap_output_schema_envelope(with_sibling), with_sibling)

    async def test_schema_prefix_materially_increases_cross_intent_prefix(self) -> None:
        questions = ("金矿勘查间距是多少？", "采矿权延续需要哪些材料？")
        for capture in (self._resolver_messages, self._planner_messages):
            legacy = [await capture(question, "legacy") for question in questions]
            optimized = [await capture(question, "schema_prefix") for question in questions]
            legacy_prefix = len(
                os.path.commonprefix(
                    [legacy[0][0]["content"], legacy[1][0]["content"]]
                )
            )
            optimized_prefix = len(
                os.path.commonprefix(
                    [optimized[0][0]["content"], optimized[1][0]["content"]]
                )
            )
            self.assertGreater(optimized_prefix - legacy_prefix, 600)


if __name__ == "__main__":
    unittest.main()
