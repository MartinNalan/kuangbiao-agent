from __future__ import annotations

from collections import defaultdict
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
from threading import Lock
from time import perf_counter
import types
from typing import Any, Callable

import numpy as np

from .config import get_settings
from .mnr_policy_allowlist import normalize_document_number
from .query_understanding import QueryPlan, query_plan_from_payload


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = PROJECT_ROOT / "scripts"
DEFAULT_RUNTIME_MANIFEST = (
    PROJECT_ROOT
    / "data"
    / "knowledge_base_v4"
    / "runtime_private"
    / "hybrid_fixed20_v1"
    / "runtime_manifest.json"
)
SCHEMA_VERSION = "geowiki-v4-local-production-runtime.v1"
MODEL = "qwen3.7-text-embedding"
DIMENSION = 1024
QUERY_INSTRUCT = (
    "Given a Chinese question about mineral resources laws, policies, technical "
    "standards, and administrative procedures, retrieve the most relevant "
    "authoritative provision or evidence passage."
)
FINAL_POOL_SIZE = 20
STATUS_MARKERS = (
    "是否现行",
    "是否废止",
    "还有效",
    "是否有效",
    "已废止",
    "被替代",
    "现行规定",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_pairs(pairs: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for left, right in pairs:
        digest.update(left.encode("utf-8"))
        digest.update(b"\0")
        digest.update(right.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _load_t068() -> types.ModuleType:
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    return importlib.import_module("run_v4_generic_fixed20_shadow_replay")


def _runtime_source_hashes(t068: types.ModuleType) -> dict[str, str]:
    found: dict[Path, types.ModuleType] = {}

    def visit(module: types.ModuleType) -> None:
        source = getattr(module, "__file__", None)
        if not source:
            return
        path = Path(source).resolve()
        if path.suffix != ".py" or not path.is_relative_to(SCRIPT_DIR) or path in found:
            return
        found[path] = module
        for member in vars(module).values():
            if isinstance(member, types.ModuleType):
                visit(member)

    visit(t068)
    return {
        str(path.relative_to(PROJECT_ROOT)): sha256_file(path)
        for path in sorted(found)
    }


def _resolve_artifact(item: dict[str, Any]) -> Path:
    path = Path(str(item["path"]))
    return path if path.is_absolute() else PROJECT_ROOT / path


def _readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _source_role(document_type: str, standard_no: str | None, title: str) -> str:
    if document_type == "policy_attachment":
        return "policy_attachment"
    if document_type in {"service_guide", "administrative_service_guide"}:
        return "service_guide"
    if standard_no == "自然资规〔2023〕4号":
        return "parent_policy"
    if document_type in {
        "law",
        "regulation",
        "administrative_regulation",
        "department_rule",
        "departmental_rule",
        "policy_document",
    }:
        return "policy_document"
    if "300问" in title:
        return "interpretive_material"
    return "standard_or_other"


def _source_access(document: dict[str, Any]) -> tuple[str, str]:
    document_type = str(document.get("document_type") or "")
    url = str(document.get("source_url") or "").lower()
    if document_type in {
        "law",
        "regulation",
        "administrative_regulation",
        "department_rule",
        "departmental_rule",
        "policy_document",
        "service_guide",
        "administrative_service_guide",
        "policy_attachment",
    }:
        return "official_fulltext", "html_text" if ".doc" not in url else "pdf_text"
    return "local_kb", "ocr_text"


class FrozenQueryEmbedder:
    """A deterministic injected embedder used by local shadow replay/tests."""

    def __init__(self, vectors_by_text: dict[str, np.ndarray]) -> None:
        self.vectors_by_text = vectors_by_text
        self.calls: list[str] = []

    def embed_query(self, text: str) -> np.ndarray:
        self.calls.append(text)
        try:
            return np.asarray(self.vectors_by_text[text], dtype=np.float32)
        except KeyError as exc:
            raise RuntimeError(f"frozen query vector missing: {text}") from exc

    def close(self) -> None:
        return None


class QwenQueryEmbedder:
    """Thread-safe runtime query embedding with the frozen T036 contract."""

    def __init__(self, t068: types.ModuleType) -> None:
        settings = get_settings()
        api_key = (settings.embedding_api_key or settings.dashscope_api_key).strip()
        base_url = settings.embedding_base_url.strip().rstrip("/")
        # v3 currently uses text-embedding-v4.  Keep that rollback setting
        # untouched and give the v4 runtime an isolated model contract while
        # reusing the same authorized provider credentials/base URL.
        configured_model = os.getenv("V4_EMBEDDING_MODEL", MODEL).strip() or MODEL
        configured_dimension = int(
            os.getenv("V4_EMBEDDING_DIMENSIONS", str(DIMENSION)) or DIMENSION
        )
        if not api_key or not base_url:
            raise RuntimeError("v4 query embedding is not configured")
        if configured_model and configured_model != MODEL:
            raise RuntimeError(
                f"v4 runtime requires {MODEL}, configured {configured_model}"
            )
        if configured_dimension != DIMENSION:
            raise RuntimeError(
                f"v4 runtime requires dimension {DIMENSION}, configured {configured_dimension}"
            )
        self._client = t068.t063.t036.DashscopeNativeClient(
            api_key=api_key,
            base_url=base_url,
            model=MODEL,
            dimension=DIMENSION,
            timeout_seconds=settings.request_timeout_seconds,
            max_retries=2,
            max_connections=2,
        )
        self._lock = Lock()

    def embed_query(self, text: str) -> np.ndarray:
        with self._lock:
            vectors, _, _ = self._client.embed(
                [text],
                text_type="query",
                instruct=QUERY_INSTRUCT,
            )
        return vectors[0]

    def close(self) -> None:
        self._client.close()


class V4KnowledgeStore:
    """Read-only v4 search/catalog adapter for the existing KB HTTP contract."""

    def __init__(
        self,
        manifest_path: Path = DEFAULT_RUNTIME_MANIFEST,
        *,
        query_embedder: Any | None = None,
        legacy_admin_store: Any | None = None,
        validate_hashes: bool = True,
    ) -> None:
        self.manifest_path = manifest_path.resolve()
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self._validate_manifest(validate_hashes=validate_hashes)
        self.t068 = _load_t068()
        self._validate_runtime_sources()
        self.legacy_admin_store = legacy_admin_store

        artifacts = self.manifest["artifacts"]
        self.db_path = _resolve_artifact(artifacts["corpus"])
        self.retrieval_units_path = _resolve_artifact(artifacts["retrieval_units"])
        self.fts_path = _resolve_artifact(artifacts["fts"])
        self.vector_path = _resolve_artifact(artifacts["document_vectors"])
        self.mapping_path = _resolve_artifact(artifacts["row_mapping"])
        self.concept_families_path = _resolve_artifact(artifacts["concept_families"])

        self.rows, self.row_by_id = self.t068.read_rows(self.retrieval_units_path)
        self.units = self.t068.t063.load_retrieval_units(self.retrieval_units_path)
        eligible_ids = [
            row["retrieval_unit_id"]
            for row in self.rows
            if row.get("search_eligible")
        ]
        if eligible_ids != [unit.unit_id for unit in self.units]:
            raise RuntimeError("v4 runtime retrieval-unit order changed")
        mapping_ids = [
            json.loads(line)["retrieval_unit_id"]
            for line in self.mapping_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if mapping_ids != eligible_ids:
            raise RuntimeError("v4 runtime vector row mapping changed")

        self.document_vectors = np.load(
            self.vector_path,
            mmap_mode="r",
            allow_pickle=False,
        )
        if self.document_vectors.shape != (len(self.units), DIMENSION):
            raise RuntimeError("v4 runtime document-vector shape changed")
        self.unit_index = {
            unit.unit_id: index for index, unit in enumerate(self.units)
        }
        self.units_by_document: dict[str, list[Any]] = defaultdict(list)
        for unit in self.units:
            self.units_by_document[unit.document_id].append(unit)
        self.rows_by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self.rows:
            self.rows_by_document[row["document_id"]].append(row)

        self.child_catalog = self.t068.t064.direct_child_catalog(self.rows)
        self.appendix_catalog = self.t068.appendix_child_catalog(self.rows)
        self.list_manifests = self.t068.t061.t058.list_manifests(self.rows)
        self.gap_families = self.t068.t061.t058.t055.explicit_family_catalog(
            self.rows
        )
        self.governed_families = self.t068.load_governed_families(
            self.concept_families_path
        )
        self.classification_catalog = self.t068.t061.section_catalog(self.rows)[0]
        self.documents = self._load_documents()

        self.embedding_configuration_error: str | None = None
        if query_embedder is not None:
            self.query_embedder = query_embedder
        else:
            try:
                self.query_embedder = QwenQueryEmbedder(self.t068)
            except RuntimeError as exc:
                self.query_embedder = None
                self.embedding_configuration_error = str(exc)

    def _validate_manifest(self, *, validate_hashes: bool) -> None:
        if (
            self.manifest.get("schema_version") != SCHEMA_VERSION
            or self.manifest.get("status") != "local_shadow_ready"
        ):
            raise RuntimeError("v4 local runtime manifest is not ready")
        authorization = self.manifest["authorization"]
        if (
            not authorization["v3_remains_default"]
            or authorization["cloud_activation_authorized"]
            or authorization["deployment_authorized"]
            or authorization["service_restart_authorized"]
            or authorization["knowledge_graph_build_authorized"]
            or authorization["cloud_sync_required"]
        ):
            raise RuntimeError("v4 local runtime authorization boundary changed")
        if not validate_hashes:
            return
        for item in self.manifest["artifacts"].values():
            path = _resolve_artifact(item)
            if sha256_file(path) != item["sha256"]:
                raise RuntimeError(f"v4 runtime artifact changed: {item['path']}")
        source = self.manifest["runtime_sources"]
        for key in ("adapter", "governed_query_routing", "query_understanding"):
            item = source[key]
            if sha256_file(_resolve_artifact(item)) != item["sha256"]:
                raise RuntimeError(f"v4 runtime source changed: {key}")

    def _validate_runtime_sources(self) -> None:
        source = self.manifest["runtime_sources"]
        hashes = _runtime_source_hashes(self.t068)
        if hashes != source["reachable_script_hashes"]:
            raise RuntimeError("v4 reachable runtime scripts changed")
        bundle = sha256_pairs(list(hashes.items()))
        if bundle != source["bundle_sha256"]:
            raise RuntimeError("v4 runtime source bundle changed")

    def _load_documents(self) -> dict[str, dict[str, Any]]:
        with _readonly_connection(self.db_path) as connection:
            return {
                row["document_id"]: dict(row)
                for row in connection.execute("select * from documents")
            }

    def close(self) -> None:
        close = getattr(self.query_embedder, "close", None)
        if callable(close):
            close()

    def health(self) -> dict[str, Any]:
        candidate_count = 0
        if self.legacy_admin_store is not None:
            try:
                candidate_count = int(
                    self.legacy_admin_store.health().get("candidate_count") or 0
                )
            except Exception:
                candidate_count = 0
        return {
            "status": "ok",
            "service": "mining-knowledge-base",
            "runtime_version": "v4",
            "runtime_id": self.manifest["runtime_id"],
            "runtime_status": self.manifest["status"],
            "storage": "sqlite_fts5_plus_numpy_exact_cosine",
            "db_path": str(self.db_path),
            "document_count": len(self.documents),
            "chunk_count": len(self.units),
            "retrieval_leaf_count": len(self.units),
            "candidate_count": candidate_count,
            "vector_count": int(self.document_vectors.shape[0]),
            "embedding_count": int(self.document_vectors.shape[0]),
            "embedding_model": MODEL,
            "embedding_dimension": DIMENSION,
            "query_embedding_ready": self.query_embedder is not None,
            "query_embedding_error": self.embedding_configuration_error,
            "kg_entity_count": 0,
            "kg_relation_count": 0,
            "ann_available": False,
            "ann_count": 0,
            "cloud_sync_required": False,
        }

    def _embed_query(self, text: str) -> tuple[np.ndarray | None, float, str | None]:
        if self.query_embedder is None:
            return None, 0.0, self.embedding_configuration_error or "query embedding unavailable"
        started = perf_counter()
        try:
            vector = np.asarray(self.query_embedder.embed_query(text), dtype=np.float32)
            if vector.shape != (DIMENSION,) or np.any(~np.isfinite(vector)):
                raise RuntimeError("invalid v4 query vector")
            norm = float(np.linalg.norm(vector))
            if norm <= 0:
                raise RuntimeError("zero v4 query vector")
            vector = vector / norm
            return vector, (perf_counter() - started) * 1000.0, None
        except Exception as exc:
            return None, (perf_counter() - started) * 1000.0, str(exc)

    def _scope_unit_indices(
        self,
        plan: QueryPlan,
        filters: dict[str, Any],
    ) -> tuple[list[int] | None, dict[str, Any]]:
        document_ids: set[str] | None = None
        explicit_document_id = str(filters.get("document_id") or "").strip()
        if explicit_document_id:
            document_ids = {explicit_document_id}

        requested_numbers: list[str] = []
        filter_number = filters.get("standard_no")
        if isinstance(filter_number, str) and filter_number.strip():
            requested_numbers.append(filter_number.strip())
        elif isinstance(filter_number, list):
            requested_numbers.extend(str(value).strip() for value in filter_number if str(value).strip())
        # The T068 baseline does not turn a standard number parsed from the
        # question into a hard corpus scope.  Only an explicit API filter is
        # hard here; a future query-to-filter rule needs its own A/B instead
        # of entering silently during production wiring.
        if requested_numbers:
            normalized = {
                normalize_document_number(number) for number in requested_numbers
            }
            number_documents = {
                document_id
                for document_id, document in self.documents.items()
                if normalize_document_number(document.get("standard_no")) in normalized
            }
            document_ids = number_documents if document_ids is None else document_ids & number_documents

        requested_types = filters.get("document_types") or []
        if isinstance(requested_types, str):
            requested_types = [requested_types]
        requested_types = {str(value) for value in requested_types if str(value)}
        if requested_types:
            type_documents = {
                document_id
                for document_id, document in self.documents.items()
                if document.get("document_type") in requested_types
            }
            document_ids = type_documents if document_ids is None else document_ids & type_documents

        if document_ids is None:
            return None, {"applied": False, "document_ids": []}
        indices = [
            index
            for index, unit in enumerate(self.units)
            if unit.document_id in document_ids
        ]
        return indices, {
            "applied": True,
            "document_ids": sorted(document_ids),
            "unit_count": len(indices),
        }

    def _lexical_frontier(self, query: str, fts: Any) -> dict[str, Any]:
        candidates, candidate_trace = fts.candidates(query)
        if candidates:
            stage1, stage1_trace = self.t068.t063.t029.rerank_fts_candidates(
                candidates,
                query,
                candidate_trace=candidate_trace,
                top_k=len(candidates),
            )
            fallback = None
        else:
            stage1, full_trace = self.t068.t063.FullScanSearcher(self.units).search(
                query,
                top_k=len(self.units),
            )
            stage1_trace = {**candidate_trace, "rerank_trace": full_trace}
            fallback = "zero_fts_candidates_to_fullscan"
        document_ids = self.t068.t063.ordered_documents(stage1)[
            : self.t068.STAGE1_DOCUMENT_COUNT
        ]
        stage2_units = [
            unit
            for document_id in document_ids
            for unit in self.units_by_document[document_id]
        ]
        lexical, stage2_trace = self.t068.t063.FullScanSearcher(stage2_units).search(
            query,
            top_k=min(self.t068.LEXICAL_TOP_K, len(stage2_units)),
        )
        ids = [row.unit.unit_id for row in lexical]
        return {
            "candidate_ids": ids,
            "candidate_order": ids,
            "trace": {
                "lexical_candidate_leaf_count": len(candidates),
                "lexical_fallback": fallback,
                "lexical_top50_ids": ids,
                "dense_top60_ids": [],
                "candidate_union_count": len(ids),
                "stage1_document_ids": document_ids,
                "stage1_trace": stage1_trace,
                "stage2_trace": stage2_trace,
                "route_details": {},
                "admission_trace": {"used": False},
            },
        }

    def _scoped_frontier(
        self,
        query: str,
        query_vector: np.ndarray | None,
        indices: list[int],
    ) -> dict[str, Any]:
        scoped_units = [self.units[index] for index in indices]
        if not scoped_units:
            return {
                "candidate_ids": [],
                "candidate_order": [],
                "trace": {
                    "lexical_top50_ids": [],
                    "dense_top60_ids": [],
                    "candidate_union_count": 0,
                    "route_details": {},
                    "scoped": True,
                },
            }
        lexical, lexical_trace = self.t068.t063.FullScanSearcher(scoped_units).search(
            query,
            top_k=min(self.t068.LEXICAL_TOP_K, len(scoped_units)),
        )
        dense: list[Any] = []
        if query_vector is not None:
            scoped_vectors = np.asarray(self.document_vectors[indices], dtype=np.float32)
            scores = np.asarray(scoped_vectors @ query_vector, dtype=np.float32)
            order = np.argsort(-scores, kind="stable")[: self.t068.DENSE_TOP_K]
            dense = [
                self.t068.t063.RankedUnit(
                    unit=scoped_units[int(index)],
                    score=float(scores[int(index)]),
                    matched_terms=0,
                )
                for index in order
            ]
        if dense:
            fused, details = self.t068.t063.equal_rrf(lexical, dense)
            protected, admission = self.t068.t063.t039.apply_head_admission(
                fused,
                lexical,
                dense,
                lexical_head_depth=self.t068.t063.LEXICAL_HEAD_DEPTH,
                dense_head_depth=self.t068.t063.DENSE_HEAD_DEPTH,
            )
            order_ids = [row.unit.unit_id for row in protected]
        else:
            details = {}
            admission = {"used": False}
            order_ids = [row.unit.unit_id for row in lexical]
        candidate_ids = list(
            dict.fromkeys(
                [row.unit.unit_id for row in lexical]
                + [row.unit.unit_id for row in dense]
            )
        )
        return {
            "candidate_ids": candidate_ids,
            "candidate_order": order_ids,
            "trace": {
                "lexical_top50_ids": [row.unit.unit_id for row in lexical],
                "dense_top60_ids": [row.unit.unit_id for row in dense],
                "candidate_union_count": len(candidate_ids),
                "route_details": details,
                "admission_trace": admission,
                "stage2_trace": lexical_trace,
                "scoped": True,
            },
        }

    def _status_search(
        self,
        query: str,
        plan: QueryPlan,
        started: float,
    ) -> dict[str, Any]:
        requested_numbers = {
            normalize_document_number(value)
            for value in plan.standard_numbers
            if normalize_document_number(value)
        }
        title_query = query
        for marker in STATUS_MARKERS:
            title_query = title_query.replace(marker, "")
        title_query = re.sub(r"[《》？?，,。\s]", "", title_query)
        matched = [
            document
            for document in self.documents.values()
            if (
                requested_numbers
                and normalize_document_number(document.get("standard_no"))
                in requested_numbers
            )
            or (
                not requested_numbers
                and title_query
                and (
                    title_query in _compact(str(document.get("title") or ""))
                    or _compact(str(document.get("title") or "")) in title_query
                )
            )
        ]
        items = []
        for document in matched[:20]:
            effective = str(document.get("effective_status") or "unverified")
            label = {
                "current": "现行有效",
                "repealed": "已废止或失效",
                "governance_conflict": "时效状态存在冲突，待人工复核",
                "unverified": "未完成官方时效核验",
            }.get(effective, effective)
            metadata = json.loads(document.get("source_metadata_json") or "{}")
            governance = metadata.get("t020_governance") or {}
            status_evidence = governance.get("status_evidence")
            checked_at = governance.get("checked_at")
            quote = f"该文件当前状态：{label}。"
            if status_evidence:
                quote += f" 状态依据：{status_evidence}"
            if checked_at:
                quote += f" 核验时间：{checked_at}。"
            source_type, text_access = _source_access(document)
            items.append(
                {
                    "chunk_id": None,
                    "document_id": document["document_id"],
                    "title": document["title"],
                    "standard_no": document.get("standard_no"),
                    "section_path": "文件时效状态",
                    "clause_no": "时效状态",
                    "page_start": None,
                    "page_end": None,
                    "page": None,
                    "quote": quote,
                    "evidence_text": quote,
                    "score": 0.99,
                    "hit_type": ["governance_status"],
                    "source_type": "official_metadata" if status_evidence else source_type,
                    "text_access": "metadata_only" if status_evidence else text_access,
                    "validation_status": document.get("review_status"),
                    "url": document.get("source_url"),
                    "source_platform": document.get("source_platform"),
                    "document_type": document.get("document_type"),
                    "source_role": _source_role(
                        str(document.get("document_type") or ""),
                        document.get("standard_no"),
                        str(document.get("title") or ""),
                    ),
                    "effective_status": effective,
                    "status_source": governance.get("status_source") or document.get("source_platform"),
                    "status_evidence": status_evidence,
                    "status_checked_at": checked_at,
                    "ocr_confidence": None,
                }
            )
        elapsed = (perf_counter() - started) * 1000.0
        return {
            "query": query,
            "results": items,
            "retrieval": self._retrieval_stats(
                full_text_ids=[],
                dense_ids=[],
                candidate_count=len(items),
                embedding_ms=0.0,
                total_ms=elapsed,
                vector_error=None,
                scoped=bool(requested_numbers),
                retrieval_round=1,
            ),
            "coverage": {
                "has_clause_level_evidence": bool(items),
                "has_page_level_evidence": False,
                "needs_web_supplement": not items,
                "notes": [] if items else ["知识库中没有可核验的目标文件时效元数据。"],
                "query_plan": {
                    "normalized_query": plan.normalized_query,
                    "intent": plan.intent,
                    "status_lookup": True,
                },
            },
        }

    @staticmethod
    def _retrieval_stats(
        *,
        full_text_ids: list[str],
        dense_ids: list[str],
        candidate_count: int,
        embedding_ms: float,
        total_ms: float,
        vector_error: str | None,
        scoped: bool,
        retrieval_round: int,
    ) -> dict[str, Any]:
        return {
            "full_text_hits": len(full_text_ids),
            "vector_hits": len(dense_ids),
            "graph_hits": 0,
            "web_hits": 0,
            "scoped_search": int(scoped),
            "vector_skipped": int(not dense_ids),
            "direct_evidence_hits": 0,
            "candidate_count": candidate_count,
            "ann_used": 0,
            "mmr_used": 0,
            "mmr_lambda": None,
            "duplicate_ratio_before": 0.0,
            "duplicate_ratio_after": 0.0,
            "vector_route": "exact_dense" if dense_ids else "none",
            "vector_error": vector_error,
            "retrieval_round": retrieval_round,
            "timings_ms": {
                "lexical_graph": 0.0,
                "embedding": round(embedding_ms, 3),
                "vector_search": 0.0,
                "vector_total": round(embedding_ms, 3),
                "mmr": 0.0,
                "total": round(total_ms, 3),
            },
        }

    def _stopped_response(self, query: str, plan: QueryPlan, started: float) -> dict[str, Any]:
        elapsed = (perf_counter() - started) * 1000.0
        return {
            "query": query,
            "results": [],
            "retrieval": self._retrieval_stats(
                full_text_ids=[],
                dense_ids=[],
                candidate_count=0,
                embedding_ms=0.0,
                total_ms=elapsed,
                vector_error=None,
                scoped=False,
                retrieval_round=0,
            ),
            "coverage": {
                "has_clause_level_evidence": False,
                "has_page_level_evidence": False,
                "needs_web_supplement": False,
                "notes": [
                    plan.confirmation_question
                    or "问题范围尚未确认，本次未执行知识库检索。"
                ],
                "query_plan": {
                    "normalized_query": plan.normalized_query,
                    "intent": plan.intent,
                    "governed_intent": plan.governed_intent,
                    "retrieval_allowed": False,
                },
            },
        }

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        started = perf_counter()
        query = str(payload.get("query") or "").strip()
        plan = query_plan_from_payload(query, payload.get("retrieval_plan"))
        if not plan.retrieval_allowed:
            return self._stopped_response(query, plan, started)
        if any(marker in query for marker in STATUS_MARKERS):
            return self._status_search(query, plan, started)

        filters = payload.get("filters") or {}
        options = payload.get("options") or {}
        top_k = max(1, min(int(options.get("top_k") or 10), 50))
        include_full_text = bool(options.get("include_full_text"))
        retrieval_round = max(1, int(options.get("retrieval_round") or 1))
        # T068's accepted generic contract uses the original question for all
        # routes.  Only an explicitly governed T074 route may substitute its
        # separately audited lexical/semantic texts.  Do not let unrelated
        # QueryPlan expansions silently change the production baseline.
        if plan.governed_intent and plan.governed_intent != "other":
            lexical_query = plan.lexical_query or plan.normalized_query or query
            semantic_query = plan.semantic_query or plan.normalized_query or query
            structural_question = (
                plan.structural_query or plan.normalized_query or query
            )
        else:
            lexical_query = query
            semantic_query = query
            structural_question = plan.structural_query or query

        query_vector, embedding_ms, vector_error = self._embed_query(semantic_query)
        scope_indices, scope_trace = self._scope_unit_indices(plan, filters)
        fts = self.t068.t063.t029.FtsSearcher(self.fts_path, self.units)
        try:
            if scope_indices is not None:
                frontier = self._scoped_frontier(
                    lexical_query,
                    query_vector,
                    scope_indices,
                )
            elif query_vector is not None:
                frontier = self.t068.retrieve_candidate_frontier(
                    question=lexical_query,
                    query_vector=query_vector,
                    units=self.units,
                    units_by_document=self.units_by_document,
                    row_by_id=self.row_by_id,
                    fts=fts,
                    document_vectors=self.document_vectors,
                )
            else:
                frontier = self._lexical_frontier(lexical_query, fts)
        finally:
            fts.close()

        if not frontier["candidate_order"]:
            elapsed = (perf_counter() - started) * 1000.0
            return {
                "query": query,
                "results": [],
                "retrieval": self._retrieval_stats(
                    full_text_ids=frontier["trace"].get("lexical_top50_ids", []),
                    dense_ids=frontier["trace"].get("dense_top60_ids", []),
                    candidate_count=0,
                    embedding_ms=embedding_ms,
                    total_ms=elapsed,
                    vector_error=vector_error,
                    scoped=scope_trace["applied"],
                    retrieval_round=retrieval_round,
                ),
                "coverage": {
                    "has_clause_level_evidence": False,
                    "has_page_level_evidence": False,
                    "needs_web_supplement": True,
                    "notes": ["本地v4知识库未命中可引用证据。"],
                    "query_plan": {"normalized_query": plan.normalized_query, "intent": plan.intent},
                },
            }

        if scope_indices is None and len(frontier["candidate_order"]) >= FINAL_POOL_SIZE:
            runtime = self.t068.generic_fixed20_runner(
                question=structural_question,
                original_candidate_ids=frontier["candidate_ids"],
                original_candidate_order=frontier["candidate_order"],
                row_by_id=self.row_by_id,
                child_catalog=self.child_catalog,
                appendix_catalog=self.appendix_catalog,
                rows_by_document=self.rows_by_document,
                list_manifests=self.list_manifests,
                gap_families=self.gap_families,
                governed_families=self.governed_families,
                classification_catalog=self.classification_catalog,
            )
            first_pool = runtime["pools"][self.t068.ARMS[2]]
            candidate_order = runtime["candidate_order"]
            reservations = runtime["reservations"]
            traces = runtime["traces"]
        else:
            first_pool = frontier["candidate_order"][:FINAL_POOL_SIZE]
            candidate_order = frontier["candidate_order"]
            reservations = []
            traces = {"scoped_direct_retrieval": {"triggered": scope_indices is not None}}
        output_ids = list(dict.fromkeys(first_pool + candidate_order))[:top_k]

        lexical_ids = frontier["trace"].get("lexical_top50_ids", [])
        dense_ids = frontier["trace"].get("dense_top60_ids", [])
        lexical_set = set(lexical_ids)
        dense_set = set(dense_ids)
        reservation_set = set(reservations)
        items = [
            self._result_item(
                unit_id,
                rank,
                lexical_set=lexical_set,
                dense_set=dense_set,
                reservation_set=reservation_set,
                include_full_text=include_full_text,
            )
            for rank, unit_id in enumerate(output_ids, 1)
        ]
        elapsed = (perf_counter() - started) * 1000.0
        notes = []
        if vector_error:
            notes.append(f"向量路线不可用，已使用关键词安全回退：{vector_error}")
        has_clause = any(item.get("clause_no") or item.get("section_path") for item in items)
        return {
            "query": query,
            "results": items,
            "retrieval": {
                **self._retrieval_stats(
                    full_text_ids=lexical_ids,
                    dense_ids=dense_ids,
                    candidate_count=len(candidate_order),
                    embedding_ms=embedding_ms,
                    total_ms=elapsed,
                    vector_error=vector_error,
                    scoped=scope_trace["applied"],
                    retrieval_round=retrieval_round,
                ),
                "fixed20_used": int(scope_indices is None),
                "structural_reservation_count": len(reservations),
            },
            "coverage": {
                "has_clause_level_evidence": has_clause,
                "has_page_level_evidence": any(item.get("page_start") for item in items),
                "needs_web_supplement": not bool(items),
                "notes": notes,
                "query_plan": {
                    "normalized_query": plan.normalized_query,
                    "intent": plan.intent,
                    "lexical_query": lexical_query,
                    "semantic_query": semantic_query,
                    "structural_query": structural_question,
                    "governed_intent": plan.governed_intent,
                    "governed_mapping_id": plan.governed_mapping_id,
                    "governed_mapping_applied": plan.governed_mapping_applied,
                    "retrieval_allowed": plan.retrieval_allowed,
                    "scope": scope_trace,
                    "structure_traces": traces,
                },
            },
        }

    def _result_item(
        self,
        unit_id: str,
        rank: int,
        *,
        lexical_set: set[str],
        dense_set: set[str],
        reservation_set: set[str],
        include_full_text: bool,
    ) -> dict[str, Any]:
        row = self.row_by_id[unit_id]
        document = self.documents[row["document_id"]]
        hit_type = []
        if unit_id in lexical_set:
            hit_type.append("full_text")
        if unit_id in dense_set:
            hit_type.append("vector")
        if unit_id in reservation_set:
            hit_type.append("structure")
        if not hit_type:
            hit_type.append("structure")
        source_type, text_access = _source_access(document)
        source_refs = row.get("source_page_refs") or []
        item = {
            "chunk_id": unit_id,
            "retrieval_unit_id": unit_id,
            "document_id": row["document_id"],
            "title": row["title"],
            "standard_no": row.get("standard_no"),
            "section_path": row.get("section_path"),
            "clause_no": row.get("clause_no"),
            "page_start": row.get("page_start"),
            "page_end": row.get("page_end"),
            "page": row.get("page_start"),
            "quote": row.get("citation_text") or "",
            "evidence_text": row.get("citation_text") or "",
            "score": round(max(0.05, 0.99 - (rank - 1) * 0.03), 4),
            "score_type": "normalized_final_rank",
            "hit_type": hit_type,
            "source_type": source_type,
            "text_access": text_access,
            "validation_status": row.get("review_status"),
            "url": document.get("source_url"),
            "source_platform": document.get("source_platform"),
            "document_type": document.get("document_type"),
            "source_role": _source_role(
                str(document.get("document_type") or ""),
                document.get("standard_no"),
                str(document.get("title") or ""),
            ),
            "effective_status": document.get("effective_status"),
            "status_source": document.get("source_platform"),
            "status_evidence": None,
            "status_checked_at": None,
            "ocr_confidence": None,
            "source_page_refs": source_refs,
            "source_unit_ids": row.get("source_unit_ids") or [],
        }
        if include_full_text:
            item["text"] = row.get("search_text") or row.get("citation_text") or ""
        return item

    def _public_document(self, document: dict[str, Any]) -> dict[str, Any]:
        source_type, text_access = _source_access(document)
        return {
            "document_id": document["document_id"],
            "title": document["title"],
            "standard_no": document.get("standard_no"),
            "document_type": document.get("document_type"),
            "status": document.get("status"),
            "effective_status": document.get("effective_status"),
            "source_type": source_type,
            "text_access": text_access,
            "validation_status": document.get("review_status"),
            "can_answer": bool(document.get("can_answer")),
            "publish_date": None,
            "implementation_date": None,
            "ingestion_time": document.get("created_at"),
            "url": document.get("source_url"),
            "source_platform": document.get("source_platform"),
            "page_count": document.get("page_count"),
            "chunk_count": document.get("unit_count"),
        }

    def standards(self, params: dict[str, Any]) -> dict[str, Any]:
        page = max(1, int(params.get("page") or 1))
        page_size = max(1, min(int(params.get("page_size") or 20), 100))
        rows = list(self.documents.values())
        q = str(params.get("q") or "").strip()
        if q:
            rows = [
                row
                for row in rows
                if q in str(row.get("title") or "") or q in str(row.get("standard_no") or "")
            ]
        for request_key, column in (
            ("standard_no", "standard_no"),
            ("status", "status"),
            ("document_type", "document_type"),
            ("validation_status", "review_status"),
        ):
            value = params.get(request_key)
            if value:
                rows = [row for row in rows if str(row.get(column) or "") == str(value)]
        rows.sort(key=lambda row: (str(row.get("standard_no") or ""), str(row.get("title") or "")))
        total = len(rows)
        offset = (page - 1) * page_size
        return {
            "items": [self._public_document(row) for row in rows[offset : offset + page_size]],
            "pagination": {"page": page, "page_size": page_size, "total": total},
        }

    def research_corpus(self, payload: dict[str, Any]) -> dict[str, Any]:
        limit = max(5, min(int(payload.get("limit") or 60), 200))
        title_terms = [str(value).strip() for value in payload.get("title_terms") or [] if str(value).strip()][:20]
        standard_numbers = [str(value).strip() for value in payload.get("standard_numbers") or [] if str(value).strip()][:20]
        document_types = {str(value).strip() for value in payload.get("document_types") or [] if str(value).strip()}
        rows = [
            row
            for row in self.documents.values()
            if row.get("effective_status") == "current"
            and row.get("review_status") == "approved_for_service"
            and int(row.get("can_answer") or 0) == 1
        ]
        if document_types:
            rows = [row for row in rows if row.get("document_type") in document_types]
        if title_terms or standard_numbers:
            normalized_numbers = {normalize_document_number(value) for value in standard_numbers}
            rows = [
                row
                for row in rows
                if any(term in str(row.get("title") or "") for term in title_terms)
                or normalize_document_number(row.get("standard_no")) in normalized_numbers
            ]
        rows.sort(key=lambda row: (str(row.get("standard_no") or ""), str(row.get("title") or "")))
        total = len(rows)
        snapshot = "kb_" + self.manifest["artifacts"]["corpus"]["sha256"][:16]
        return {
            "items": [self._public_document(row) for row in rows[:limit]],
            "total": total,
            "returned": min(total, limit),
            "truncated": total > limit,
            "knowledge_snapshot": snapshot,
        }

    def document(self, document_id: str) -> dict[str, Any] | None:
        document = self.documents.get(document_id)
        return self._public_document(document) if document else None

    def chunk(self, chunk_id: str, include_full_text: bool = False) -> dict[str, Any] | None:
        row = self.row_by_id.get(chunk_id)
        if row is None:
            return None
        item = self._result_item(
            chunk_id,
            1,
            lexical_set=set(),
            dense_set=set(),
            reservation_set=set(),
            include_full_text=include_full_text,
        )
        return item

    def create_candidate(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.legacy_admin_store is None:
            raise RuntimeError("v4 runtime candidate administration requires the legacy admin store")
        return self.legacy_admin_store.create_candidate(payload)

    def candidates(self, page: int = 1, page_size: int = 50) -> dict[str, Any]:
        if self.legacy_admin_store is None:
            return {"items": [], "pagination": {"page": page, "page_size": page_size, "total": 0}}
        return self.legacy_admin_store.candidates(page=page, page_size=page_size)
