from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mining_qa.ann_index import AnnManifest
from mining_qa.config import Settings
from mining_qa.embedding_provider import EmbeddingConfig, EmbeddingProvider
from mining_qa.knowledge_store import (
    KnowledgeStore,
    column_has_leading_index,
    connect,
    retrieval_recall_limit,
)
from mining_qa.llm_client import LLMClient
from mining_qa.query_understanding import understand_query


class RetrievalBudgetTests(unittest.TestCase):
    def test_default_model_and_vector_budgets_are_bounded(self) -> None:
        settings = Settings()

        self.assertEqual(settings.query_planner_max_tokens, 600)
        self.assertEqual(settings.evidence_reranker_max_tokens, 800)
        self.assertEqual(settings.answer_max_tokens, 1000)
        self.assertEqual(settings.structured_temperature, 0.0)
        self.assertEqual(settings.answer_temperature, 0.2)
        self.assertEqual(settings.controlled_multi_query_max, 2)
        self.assertEqual(settings.ann_expansion_search, 64)
        self.assertEqual(settings.vector_fallback_scan_limit, 100)

    def test_recall_budget_depends_on_scope_and_comparison_mode(self) -> None:
        scoped = understand_query("金矿勘查Ⅰ类型的推荐工程间距是多少")
        general = understand_query("压覆矿产资源审批需要注意什么")
        comparison = understand_query("不同标准对矿体无限外推有什么差异")
        materials = understand_query("采矿权延续需要提交什么材料")

        self.assertEqual(retrieval_recall_limit(scoped, 10), 30)
        self.assertEqual(retrieval_recall_limit(general, 10), 40)
        self.assertEqual(retrieval_recall_limit(comparison, 20), 80)
        self.assertEqual(retrieval_recall_limit(materials, 10), 60)

    def test_llm_plan_payload_omits_trace_only_fields(self) -> None:
        payload = understand_query("不同标准对矿体无限外推有什么差异").to_llm_payload()

        self.assertIn("intent", payload)
        self.assertIn("required_evidence_groups", payload)
        self.assertNotIn("original_query", payload)
        self.assertNotIn("retrieval_query", payload)
        self.assertNotIn("planner_confidence", payload)


class SqliteRetrievalGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = KnowledgeStore(Path(self.temp_dir.name) / "knowledge.sqlite")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_embedding_primary_key_indexes_chunk_id(self) -> None:
        with connect(self.store.db_path) as connection:
            connection.execute(
                """
                create table chunk_embeddings (
                  chunk_id text not null,
                  vector_model text not null,
                  vector_json text not null,
                  primary key (chunk_id, vector_model)
                )
                """
            )
            self.assertTrue(column_has_leading_index(connection, "chunk_embeddings", "chunk_id"))

    def test_local_vector_fallback_rejects_more_than_one_hundred_rows(self) -> None:
        with connect(self.store.db_path) as connection:
            connection.execute(
                """
                insert into documents(
                  document_id, title, document_type, status, source_type, text_access,
                  validation_status, visibility, review_status, ingestion_time, updated_at,
                  page_count, chunk_count, table_count, can_answer
                ) values ('doc', '测试标准', 'standard', 'current', 'local_kb', 'ocr_text',
                          'parsed', 'internal', 'approved_for_service', 'now', 'now', 1, 101, 0, 1)
                """
            )
            connection.execute(
                "create table chunk_vectors (chunk_id text primary key, vector_json text not null)"
            )
            connection.executemany(
                """
                insert into chunks(
                  chunk_id, document_id, chunk_type, title, text, source_type, text_access,
                  parse_method, validation_status, visibility, created_at
                ) values (?, 'doc', 'text', '测试标准', '测试内容', 'local_kb', 'ocr_text',
                          'test', 'parsed', 'internal', 'now')
                """,
                [(f"chunk-{index}",) for index in range(101)],
            )
            connection.executemany(
                "insert into chunk_vectors(chunk_id, vector_json) values (?, '{}')",
                [(f"chunk-{index}",) for index in range(101)],
            )

            self.assertFalse(
                self.store._vector_scope_within_limit(  # noqa: SLF001
                    connection,
                    "chunk_vectors",
                    ["d.document_id in (?)"],
                    ["doc"],
                )
            )

    def test_ann_manifest_validation_is_cached(self) -> None:
        manifest = AnnManifest(
            model="test-model",
            dimensions=512,
            dtype="f16",
            count=1,
            max_updated_at="now",
            chunk_ids=("chunk-1",),
        )

        class Result:
            @staticmethod
            def fetchone():
                return {
                    "count": 1,
                    "min_dimensions": 512,
                    "max_dimensions": 512,
                    "max_updated_at": "now",
                }

        class Connection:
            def __init__(self):
                self.calls = 0

            def execute(self, *_args, **_kwargs):
                self.calls += 1
                return Result()

        connection = Connection()
        self.assertTrue(self.store._ann_manifest_matches(connection, manifest, "test-model", 512))  # type: ignore[arg-type]  # noqa: SLF001
        self.assertTrue(self.store._ann_manifest_matches(connection, manifest, "test-model", 512))  # type: ignore[arg-type]  # noqa: SLF001
        self.assertEqual(connection.calls, 1)


class LlmConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_calls_reuse_one_async_http_client(self) -> None:
        class Response:
            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                return {"choices": [{"message": {"content": '{"ok": true}'}}]}

        class FakeAsyncClient:
            instances = 0
            payloads = []

            def __init__(self, **_kwargs):
                type(self).instances += 1
                self.is_closed = False

            async def post(self, *_args, **kwargs):
                type(self).payloads.append(kwargs.get("json") or {})
                return Response()

            async def aclose(self) -> None:
                self.is_closed = True

        with patch("mining_qa.llm_client.httpx.AsyncClient", FakeAsyncClient):
            client = LLMClient(Settings(OPENAI_API_KEY="test"))
            await client.complete_json([{"role": "user", "content": "one"}], max_tokens=10)
            await client.complete_json([{"role": "user", "content": "two"}], max_tokens=10)
            await client.aclose()

        self.assertEqual(FakeAsyncClient.instances, 1)
        self.assertTrue(FakeAsyncClient.payloads)
        self.assertTrue(
            all(payload.get("thinking") == {"type": "disabled"} for payload in FakeAsyncClient.payloads)
        )

    async def test_non_deepseek_json_call_uses_standard_payload(self) -> None:
        class Response:
            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                return {"choices": [{"message": {"content": '{"ok": true}'}}]}

        class FakeAsyncClient:
            payload = None

            def __init__(self, **_kwargs):
                self.is_closed = False

            async def post(self, *_args, **kwargs):
                type(self).payload = kwargs.get("json") or {}
                return Response()

            async def aclose(self) -> None:
                self.is_closed = True

        with patch("mining_qa.llm_client.httpx.AsyncClient", FakeAsyncClient):
            client = LLMClient(
                Settings(
                    OPENAI_API_KEY="test",
                    OPENAI_BASE_URL="https://llm.example.com/v1",
                    OPENAI_MODEL="other-chat-model",
                )
            )
            await client.complete_json([{"role": "user", "content": "one"}], max_tokens=10)
            await client.aclose()

        self.assertNotIn("thinking", FakeAsyncClient.payload)


class EmbeddingBatchTests(unittest.TestCase):
    def test_provider_honors_batch_size_and_reuses_one_client(self) -> None:
        class Response:
            def __init__(self, inputs):
                self.inputs = inputs

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {
                    "data": [
                        {"index": index, "embedding": [1.0, float(index + 1)]}
                        for index, _ in enumerate(self.inputs)
                    ]
                }

        class FakeClient:
            instances = 0
            batches = []

            def __init__(self, **_kwargs):
                type(self).instances += 1

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def post(self, *_args, **kwargs):
                inputs = (kwargs.get("json") or {}).get("input") or []
                type(self).batches.append(list(inputs))
                return Response(inputs)

        config = EmbeddingConfig(
            provider="aliyun",
            api_key="test",
            base_url="https://embedding.example.com/v1",
            model="test-model",
            dimensions=2,
            batch_size=2,
        )
        with patch("mining_qa.embedding_provider.httpx.Client", FakeClient):
            vectors = EmbeddingProvider(config).embed(["a", "b", "c", "d", "e"])

        self.assertEqual(FakeClient.instances, 1)
        self.assertEqual(FakeClient.batches, [["a", "b"], ["c", "d"], ["e"]])
        self.assertEqual(len(vectors), 5)


if __name__ == "__main__":
    unittest.main()
