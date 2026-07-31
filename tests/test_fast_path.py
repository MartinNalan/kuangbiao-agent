from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
import tempfile
from time import perf_counter
import unittest

from mining_qa.config import Settings
from mining_qa.fast_path import evaluate_fast_path_shadow
from mining_qa.fast_path_shadow import (
    BoundedJsonlWriter,
    FastPathShadowService,
    ShadowCandidate,
)
from mining_qa.query_understanding import understand_query
from mining_qa.schemas import AskResponse, Limitations, Source


def eligible_case() -> tuple[dict, dict]:
    case = {
        "status": "answered",
        "answer": "由自然资源部负责。",
        "limitations": {"has_clause_level_evidence": True},
        "sources": [
            {
                "chapter": "十、",
                "quote": "自然资源部负责本级已颁发采矿许可证的矿产资源储量评审备案工作。",
                "effective_status": "current",
                "validation_status": "pass",
                "source_type": "local_kb",
            }
        ],
    }
    trace = {
        "plan": {
            "retrieval_allowed": True,
            "authority_role_ambiguous": False,
            "classification": {"missing_slots": [], "ambiguities": []},
        },
        "planner": {"used": False},
        "reranker": None,
        "generation": {"used": False, "reason": "deterministic_answer_template"},
    }
    return case, trace


class FastPathDecisionTests(unittest.TestCase):
    def test_accepts_closed_deterministic_evidence(self) -> None:
        case, trace = eligible_case()

        decision = evaluate_fast_path_shadow(case, trace)

        self.assertTrue(decision.eligible)
        self.assertEqual(decision.reasons, ())

    def test_short_exploration_spacing_phrase_uses_deterministic_distance_intent(self) -> None:
        plan = understand_query("金矿的勘查间距")

        self.assertEqual(plan.intent, "engineering_distance_lookup")


def source(*, clause: str = "F.1", quote: str = "参考基本勘查工程间距") -> Source:
    return Source(
        title="矿产地质勘查规范 岩金",
        standard_no="DZ/T 0205-2020",
        chapter=clause,
        quote=quote,
        source_type="local_kb",
        text_access="pdf_text",
        validation_status="pass",
        effective_status="current",
    )


def response(*, clause: str = "F.1", answer: str = "确定性答案", quote: str = "证据") -> AskResponse:
    return AskResponse(
        answer=answer,
        session_id="not-logged",
        status="answered",
        sources=[source(clause=clause, quote=quote)],
        limitations=Limitations(has_clause_level_evidence=True),
        confidence="high",
    )


def shadow_settings(root: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "FAST_PATH_SHADOW_ENABLED": True,
        "FAST_PATH_SHADOW_SAMPLE_RATE": 1.0,
        "FAST_PATH_SHADOW_LOG_PATH": str(root / "shadow.jsonl"),
        "FAST_PATH_SHADOW_MAX_BYTES": 1024,
        "FAST_PATH_SHADOW_BACKUP_COUNT": 2,
        "FAST_PATH_SHADOW_DEDUP_TTL_SECONDS": 60,
        "FAST_PATH_SHADOW_DEDUP_MAX_ENTRIES": 128,
        "FAST_PATH_SHADOW_MAX_CONCURRENCY": 1,
        "FAST_PATH_SHADOW_HASH_KEY": "unit-test-shadow-hmac-key",
    }
    values.update(overrides)
    return Settings(**values)


class FastPathProductionShadowTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_is_non_blocking_and_busy_work_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            started = asyncio.Event()
            release = asyncio.Event()

            async def runner(question, filters, plan):
                del question, filters, plan
                started.set()
                await release.wait()
                return ShadowCandidate(response(), True)

            service = FastPathShadowService(
                shadow_settings(Path(directory)),
                candidate_runner=runner,
            )
            before = perf_counter()
            self.assertTrue(service.submit("金矿的勘查间距", response()))
            self.assertLess(perf_counter() - before, 0.05)
            await started.wait()
            self.assertFalse(service.submit("金矿勘查工程间距是多少", response()))
            self.assertEqual(service.health()["stats"]["busy_dropped"], 1)
            release.set()
            await service.wait_idle()

    async def test_deduplicates_the_same_query_until_ttl_expires(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            now = [0.0]
            calls = 0

            async def runner(question, filters, plan):
                nonlocal calls
                del question, filters, plan
                calls += 1
                return ShadowCandidate(response(), True)

            service = FastPathShadowService(
                shadow_settings(Path(directory)),
                candidate_runner=runner,
                clock=lambda: now[0],
            )
            self.assertTrue(service.submit("金矿的勘查间距", response()))
            await service.wait_idle()
            self.assertFalse(service.submit("金矿的勘查间距", response()))
            now[0] = 61.0
            self.assertTrue(service.submit("金矿的勘查间距", response()))
            await service.wait_idle()
            self.assertEqual(calls, 2)

    async def test_zero_sampling_skips_matches_but_always_logs_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            async def matching_runner(question, filters, plan):
                del question, filters, plan
                return ShadowCandidate(response(), True)

            settings = shadow_settings(root, FAST_PATH_SHADOW_SAMPLE_RATE=0.0)
            matching = FastPathShadowService(settings, candidate_runner=matching_runner)
            self.assertTrue(matching.submit("金矿的勘查间距", response()))
            await matching.wait_idle()
            self.assertFalse((root / "shadow.jsonl").exists())

            async def mismatching_runner(question, filters, plan):
                del question, filters, plan
                return ShadowCandidate(
                    response(clause="F.2", answer="PRIVATE_ANSWER", quote="PRIVATE_QUOTE"),
                    True,
                )

            mismatch = FastPathShadowService(settings, candidate_runner=mismatching_runner)
            private_question = "金矿的勘查间距 PRIVATE_QUESTION"
            self.assertTrue(
                mismatch.submit(
                    private_question,
                    response(answer="PRIVATE_FULL_ANSWER", quote="PRIVATE_FULL_QUOTE"),
                )
            )
            await mismatch.wait_idle()
            text = (root / "shadow.jsonl").read_text(encoding="utf-8")
            record = json.loads(text)
            self.assertEqual(record["outcome"], "eligible_mismatch")
            self.assertTrue(record["query_hash"].startswith("hmac-sha256:"))
            for forbidden in (
                private_question,
                "PRIVATE_ANSWER",
                "PRIVATE_QUOTE",
                "PRIVATE_FULL_ANSWER",
                "PRIVATE_FULL_QUOTE",
                "not-logged",
            ):
                self.assertNotIn(forbidden, text)
            self.assertEqual(stat.S_IMODE(os.stat(root / "shadow.jsonl").st_mode), 0o600)

    async def test_unresolved_question_is_rejected_without_running_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls = 0

            async def runner(question, filters, plan):
                nonlocal calls
                del question, filters, plan
                calls += 1
                return ShadowCandidate(response(), True)

            service = FastPathShadowService(
                shadow_settings(Path(directory)),
                candidate_runner=runner,
            )
            self.assertTrue(service.submit("采矿权办理需要什么材料？", response()))
            await service.wait_idle()
            self.assertEqual(calls, 0)
            self.assertEqual(service.health()["stats"]["rejected"], 1)


class FastPathDecisionRegressionTests(unittest.TestCase):
    def test_rejects_answer_model_generation(self) -> None:
        case, trace = eligible_case()
        trace["generation"] = {"used": True, "finish_reason": "stop"}

        decision = evaluate_fast_path_shadow(case, trace)

        self.assertFalse(decision.eligible)
        self.assertIn("answer_model_used", decision.reasons)
        self.assertIn("no_deterministic_renderer_contract", decision.reasons)

    def test_rejects_unresolved_slots(self) -> None:
        case, trace = eligible_case()
        trace["plan"]["classification"]["missing_slots"] = ["license_issuer_level"]

        decision = evaluate_fast_path_shadow(case, trace)

        self.assertFalse(decision.eligible)
        self.assertIn("unresolved_question_state", decision.reasons)

    def test_rejects_non_current_or_uncitable_source(self) -> None:
        case, trace = eligible_case()
        case["sources"][0]["effective_status"] = "repealed"
        case["sources"][0]["quote"] = ""

        decision = evaluate_fast_path_shadow(case, trace)

        self.assertFalse(decision.eligible)
        self.assertIn("source_1_not_current", decision.reasons)
        self.assertIn("source_1_not_clause_citable", decision.reasons)

    def test_accepts_governed_official_fulltext_stored_in_local_kb(self) -> None:
        case, trace = eligible_case()
        case["sources"][0]["source_type"] = "official_fulltext"

        decision = evaluate_fast_path_shadow(case, trace)

        self.assertTrue(decision.eligible)


class BoundedShadowLogTests(unittest.TestCase):
    def test_rotation_never_exceeds_configured_file_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadow.jsonl"
            writer = BoundedJsonlWriter(path, max_bytes=1024, backup_count=2)
            for index in range(12):
                self.assertTrue(writer.write({"index": index, "padding": "x" * 700}))
            files = sorted(Path(directory).glob("shadow.jsonl*"))
            self.assertLessEqual(len(files), 3)
            self.assertTrue(all(item.stat().st_size <= 1024 for item in files))
            self.assertLessEqual(sum(item.stat().st_size for item in files), 3 * 1024)


if __name__ == "__main__":
    unittest.main()
