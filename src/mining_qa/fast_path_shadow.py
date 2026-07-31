from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
from threading import Lock
from time import monotonic, perf_counter
from typing import Any

from .agent import MiningQAAgent
from .config import Settings
from .fast_path import evaluate_fast_path_shadow
from .gap_tasks import KnowledgeGapTaskStore
from .query_understanding import QueryPlan, understand_query
from .schemas import AskFilters, AskRequest, AskResponse, Source


SCHEMA_VERSION = "geowiki-fast-path-production-shadow.v1"
CandidateRunner = Callable[
    [str, AskFilters, QueryPlan],
    Awaitable["ShadowCandidate"],
]


@dataclass(frozen=True)
class ShadowCandidate:
    response: AskResponse
    deterministic_answer_rendered: bool


def _compact_text(value: str | None, *, limit: int = 120) -> str | None:
    compact = re.sub(r"\s+", " ", str(value or "")).strip()
    return compact[:limit] or None


def evidence_locators(sources: Iterable[Source]) -> tuple[dict[str, str | None], ...]:
    """Return the only source metadata allowed in the bounded shadow log."""

    unique: dict[tuple[str, str], dict[str, str | None]] = {}
    for source in sources:
        document_ref = _compact_text(source.standard_no)
        if not document_ref:
            title_digest = hashlib.sha256(source.title.encode("utf-8")).hexdigest()[:16]
            document_ref = f"title_sha256:{title_digest}"
        clause = _compact_text(source.chapter)
        key = (document_ref, clause or "")
        unique[key] = {"document_ref": document_ref, "clause": clause}
    return tuple(unique[key] for key in sorted(unique))


class BoundedJsonlWriter:
    """A small, permission-restricted JSONL writer with deterministic rotation."""

    def __init__(self, path: str | Path, *, max_bytes: int, backup_count: int):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = Lock()

    def write(self, payload: dict[str, Any]) -> bool:
        line = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            + "\n"
        ).encode("utf-8")
        if len(line) > self.max_bytes:
            return False
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                current_size = self.path.stat().st_size if self.path.exists() else 0
                if current_size and current_size + len(line) > self.max_bytes:
                    self._rotate()
                descriptor = os.open(
                    self.path,
                    os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                    0o600,
                )
                try:
                    os.write(descriptor, line)
                finally:
                    os.close(descriptor)
                os.chmod(self.path, 0o600)
            return True
        except OSError:
            return False

    def _rotate(self) -> None:
        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        if self.path.exists():
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))


class FastPathShadowService:
    """Run deterministic candidates after the public answer without a queue.

    The service deliberately retains at most ``max_concurrency`` questions in
    memory, rejects work while busy, and stores neither user text nor answer or
    evidence bodies. Matching/rejected rows are sampled; mismatches and runtime
    errors are always written to the bounded log.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        candidate_runner: CandidateRunner | None = None,
        clock: Callable[[], float] = monotonic,
    ):
        self.settings = settings
        self.enabled = settings.fast_path_shadow_enabled
        self._candidate_runner = candidate_runner or self._run_default_candidate
        self._clock = clock
        self._writer = BoundedJsonlWriter(
            settings.fast_path_shadow_log_path,
            max_bytes=settings.fast_path_shadow_max_bytes,
            backup_count=settings.fast_path_shadow_backup_count,
        )
        configured_key = (
            settings.fast_path_shadow_hash_key.strip()
            or settings.email_verification_secret.strip()
        )
        self._hash_key = configured_key.encode("utf-8") if configured_key else secrets.token_bytes(32)
        self._dedup: OrderedDict[str, float] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self._active = 0
        self._stats = {
            "submitted": 0,
            "started": 0,
            "busy_dropped": 0,
            "deduplicated": 0,
            "matched": 0,
            "mismatched": 0,
            "rejected": 0,
            "errors": 0,
            "logged": 0,
        }

    def submit(
        self,
        question: str,
        full_response: AskResponse,
        filters: AskFilters | None = None,
    ) -> bool:
        """Schedule one shadow comparison and return without awaiting it."""

        if not self.enabled or full_response.status != "answered":
            return False
        self._stats["submitted"] += 1
        query_hash = self._query_hash(question)
        now = self._clock()
        if self._is_duplicate(query_hash, now):
            self._stats["deduplicated"] += 1
            return False
        if self._active >= self.settings.fast_path_shadow_max_concurrency:
            self._stats["busy_dropped"] += 1
            return False

        self._remember(query_hash, now)
        self._active += 1
        full_evidence = evidence_locators(full_response.sources)
        task = asyncio.get_running_loop().create_task(
            self._run_guarded(
                question,
                query_hash,
                full_response.status,
                full_evidence,
                filters.model_copy(deep=True) if filters else AskFilters(),
            ),
            name=f"fast-path-shadow:{query_hash[-12:]}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _run_guarded(
        self,
        question: str,
        query_hash: str,
        full_status: str,
        full_evidence: tuple[dict[str, str | None], ...],
        filters: AskFilters,
    ) -> None:
        started = perf_counter()
        self._stats["started"] += 1
        try:
            plan = understand_query(question)
            preflight_reasons = self._preflight_reasons(plan)
            if preflight_reasons:
                self._stats["rejected"] += 1
                self._write_if_needed(
                    query_hash,
                    {
                        "outcome": "rejected",
                        "shadow_closed": False,
                        "evidence_consistent": None,
                        "full_status": full_status,
                        "shadow_status": "not_run",
                        "full_evidence": full_evidence,
                        "shadow_evidence": (),
                        "elapsed_ms": round((perf_counter() - started) * 1000, 3),
                        "rejection_reasons": preflight_reasons,
                    },
                )
                return

            candidate = await self._candidate_runner(question, filters, plan)
            response = candidate.response
            generation_reason = (
                "deterministic_definition_template"
                if plan.intent == "definition_explanation"
                else "deterministic_answer_template"
            )
            trace = {
                "plan": plan.to_payload(),
                "planner": {"used": False},
                "reranker": {"used": False},
                "generation": {
                    "used": False,
                    "reason": generation_reason
                    if candidate.deterministic_answer_rendered
                    else "deterministic_renderer_unavailable",
                },
            }
            decision = evaluate_fast_path_shadow(
                {
                    "status": response.status,
                    "answer": "rendered" if candidate.deterministic_answer_rendered else "",
                    "limitations": response.limitations.model_dump(mode="json"),
                    "sources": [source.model_dump(mode="json") for source in response.sources],
                },
                trace,
            )
            shadow_evidence = evidence_locators(response.sources)
            evidence_consistent = bool(decision.eligible and full_evidence == shadow_evidence)
            if not decision.eligible:
                outcome = "rejected"
                self._stats["rejected"] += 1
            elif evidence_consistent:
                outcome = "eligible_consistent"
                self._stats["matched"] += 1
            else:
                outcome = "eligible_mismatch"
                self._stats["mismatched"] += 1
            self._write_if_needed(
                query_hash,
                {
                    "outcome": outcome,
                    "shadow_closed": decision.eligible,
                    "evidence_consistent": evidence_consistent if decision.eligible else None,
                    "full_status": full_status,
                    "shadow_status": response.status,
                    "full_evidence": full_evidence,
                    "shadow_evidence": shadow_evidence,
                    "elapsed_ms": round((perf_counter() - started) * 1000, 3),
                    "rejection_reasons": decision.reasons,
                },
                force=outcome == "eligible_mismatch",
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._stats["errors"] += 1
            self._write_if_needed(
                query_hash,
                {
                    "outcome": "error",
                    "shadow_closed": False,
                    "evidence_consistent": None,
                    "full_status": full_status,
                    "shadow_status": "error",
                    "full_evidence": full_evidence,
                    "shadow_evidence": (),
                    "elapsed_ms": round((perf_counter() - started) * 1000, 3),
                    "rejection_reasons": (f"runtime_error:{type(error).__name__}",),
                },
                force=True,
            )
        finally:
            self._active -= 1

    def _preflight_reasons(self, plan: QueryPlan) -> tuple[str, ...]:
        reasons: list[str] = []
        classification = plan.classification
        if not plan.retrieval_allowed:
            reasons.append("retrieval_not_allowed")
        if classification and (classification.missing_slots or classification.ambiguities):
            reasons.append("unresolved_question_state")
        if plan.authority_role_ambiguous:
            reasons.append("authority_role_ambiguous")
        return tuple(dict.fromkeys(reasons))

    async def _run_default_candidate(
        self,
        question: str,
        filters: AskFilters,
        plan: QueryPlan,
    ) -> ShadowCandidate:
        shadow_settings = self.settings.model_copy(
            update={
                "openai_api_key": "",
                "query_planner_enabled": False,
                "unified_query_planning_enabled": False,
                "parallel_query_analysis_enabled": False,
                "evidence_reranker_enabled": False,
                "question_resolution_enabled": False,
                "enable_sync_web_supplement": False,
                "retrieval_trace_enabled": False,
            }
        )
        agent = MiningQAAgent(shadow_settings)
        # A shadow miss is an observation, not authorization to create a real
        # knowledge-gap record containing the question.
        agent.gap_tasks = KnowledgeGapTaskStore(Path(os.devnull))
        request = AskRequest(question=question, filters=filters)
        request._retrieval_question = question
        request._query_plan = plan
        try:
            response = await agent.ask(request)
            rendered = agent._fast_answer(question, response.sources, plan)
            return ShadowCandidate(
                response=response,
                deterministic_answer_rendered=bool(str(rendered or "").strip()),
            )
        finally:
            await agent.aclose()

    def _query_hash(self, question: str) -> str:
        normalized = re.sub(r"\s+", " ", question).strip()
        digest = hmac.new(self._hash_key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"hmac-sha256:{digest}"

    def _sampled(self, query_hash: str) -> bool:
        rate = self.settings.fast_path_shadow_sample_rate
        if rate <= 0:
            return False
        if rate >= 1:
            return True
        digest = query_hash.rsplit(":", 1)[-1]
        bucket = int(digest[:16], 16) / float(0xFFFFFFFFFFFFFFFF)
        return bucket < rate

    def _write_if_needed(
        self,
        query_hash: str,
        payload: dict[str, Any],
        *,
        force: bool = False,
    ) -> None:
        if not force and not self._sampled(query_hash):
            return
        record = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "query_hash": query_hash,
            **payload,
        }
        if self._writer.write(record):
            self._stats["logged"] += 1

    def _is_duplicate(self, query_hash: str, now: float) -> bool:
        self._prune_dedup(now)
        expires_at = self._dedup.get(query_hash)
        return bool(expires_at and expires_at > now)

    def _remember(self, query_hash: str, now: float) -> None:
        self._prune_dedup(now)
        while len(self._dedup) >= self.settings.fast_path_shadow_dedup_max_entries:
            self._dedup.popitem(last=False)
        self._dedup[query_hash] = now + self.settings.fast_path_shadow_dedup_ttl_seconds

    def _prune_dedup(self, now: float) -> None:
        expired = [key for key, value in self._dedup.items() if value <= now]
        for key in expired:
            self._dedup.pop(key, None)

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def shutdown(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def health(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": "bounded_non_blocking_shadow",
            "max_concurrency": self.settings.fast_path_shadow_max_concurrency,
            "active": self._active,
            "sample_rate": self.settings.fast_path_shadow_sample_rate,
            "dedup_ttl_seconds": self.settings.fast_path_shadow_dedup_ttl_seconds,
            "dedup_entries": len(self._dedup),
            "log_max_bytes": self.settings.fast_path_shadow_max_bytes,
            "log_backup_count": self.settings.fast_path_shadow_backup_count,
            "stats": dict(self._stats),
        }


_service: FastPathShadowService | None = None


def get_fast_path_shadow_service(settings: Settings) -> FastPathShadowService:
    global _service
    if _service is None:
        _service = FastPathShadowService(settings)
    return _service


async def shutdown_fast_path_shadow_service() -> None:
    if _service is not None:
        await _service.shutdown()
