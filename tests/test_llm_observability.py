from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mining_qa.config import Settings
from mining_qa.llm_client import LLMClient
from mining_qa.llm_observability import llm_call_context


class LLMObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_completion_preserves_cache_usage_without_prompt_body(self) -> None:
        class Response:
            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                return {
                    "choices": [
                        {
                            "message": {"content": '{"ok":true}'},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 120,
                        "completion_tokens": 8,
                        "total_tokens": 128,
                        "prompt_cache_hit_tokens": 96,
                        "prompt_cache_miss_tokens": 24,
                    },
                }

        class FakeAsyncClient:
            def __init__(self, **_kwargs):
                self.is_closed = False

            async def post(self, *_args, **_kwargs):
                return Response()

            async def aclose(self) -> None:
                self.is_closed = True

        with TemporaryDirectory() as directory:
            ledger = Path(directory) / "llm.jsonl"
            settings = Settings(
                OPENAI_API_KEY="test",
                LLM_USAGE_LEDGER_ENABLED=True,
                LLM_USAGE_LEDGER_PATH=str(ledger),
                LLM_USAGE_LEDGER_MAX_BYTES=4096,
            )
            messages = [
                {"role": "system", "content": "固定规则"},
                {"role": "user", "content": "不得写入账本的私有问题"},
            ]
            with patch("mining_qa.llm_client.httpx.AsyncClient", FakeAsyncClient):
                client = LLMClient(settings)
                with llm_call_context("question_resolution"):
                    result = await client.complete_json_detailed(messages, max_tokens=50)
                await client.aclose()

            self.assertEqual(result.prompt_cache_hit_tokens, 96)
            self.assertEqual(result.prompt_cache_miss_tokens, 24)
            record = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(record["stage"], "question_resolution")
            self.assertEqual(record["prompt_cache_hit_tokens"], 96)
            self.assertEqual(record["cache_hit_ratio"], 0.8)
            self.assertNotIn("不得写入账本的私有问题", ledger.read_text(encoding="utf-8"))
            self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)

    async def test_openai_cached_token_shape_is_normalized(self) -> None:
        class Response:
            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                return {
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 2,
                        "total_tokens": 102,
                        "prompt_tokens_details": {"cached_tokens": 64},
                    },
                }

        class FakeAsyncClient:
            def __init__(self, **_kwargs):
                self.is_closed = False

            async def post(self, *_args, **_kwargs):
                return Response()

            async def aclose(self) -> None:
                self.is_closed = True

        settings = Settings(OPENAI_API_KEY="test", OPENAI_BASE_URL="https://llm.example/v1")
        with patch("mining_qa.llm_client.httpx.AsyncClient", FakeAsyncClient):
            client = LLMClient(settings)
            result = await client.complete_detailed([{"role": "user", "content": "one"}])
            await client.aclose()
        self.assertEqual(result.prompt_cache_hit_tokens, 64)
        self.assertEqual(result.prompt_cache_miss_tokens, 36)


if __name__ == "__main__":
    unittest.main()
