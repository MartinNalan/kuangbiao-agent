from __future__ import annotations

"""T094 Store using the clean, production-only fixed-20 implementation."""

from collections import defaultdict
from collections.abc import Mapping
from concurrent.futures import Future
import hashlib
import json
from pathlib import Path
from threading import Lock, local
from typing import Any

import numpy as np

from . import v4_fixed20_core
from .knowledge_client import (
    T092_DECISION_HANDSHAKE_KEY,
    T092_DECISION_REQUEST_KEY,
    T092_DECISION_VERSION,
    build_t092_decision_envelope,
)
from .technical_sufficiency_decision_t092 import (
    ALGORITHM_SNAPSHOT_ID,
    TRACE_KEY,
    compile_technical_sufficiency_decision_t092,
)
from .v4_runtime_t094_contract import validate_t094_runtime_import_closure
from .v4_retrieval_store_clean import (
    CleanResilientV4KnowledgeStore,
    DIMENSION,
    PROJECT_ROOT,
    RUNTIME_MANIFEST_SCHEMA_VERSION,
    ResilientQwenQueryEmbedder,
    readonly_connection,
    resolve_artifact,
    sha256_file,
)


RUNTIME_ID = "v4-hybrid-fixed20-p1fix-t094-v1"
DEFAULT_RUNTIME_MANIFEST = (
    PROJECT_ROOT
    / "data"
    / "knowledge_base_v4"
    / "runtime_private"
    / "hybrid_fixed20_t094_v1"
    / "runtime_manifest.json"
)
REQUIRED_RUNTIME_SOURCE_PATHS = (
    "src/mining_qa/v4_retrieval_store_t094.py",
    "src/mining_qa/v4_retrieval_store_clean.py",
    "src/mining_qa/v4_fixed20_core.py",
    "src/mining_qa/v4_retrieval_primitives.py",
)


class T094V4KnowledgeStore(CleanResilientV4KnowledgeStore):
    """Byte-identical T092 behavior without the historical A/B import chain.

    The initializer is intentionally explicit.  The inherited T088/T090/T092
    constructors load the historical runner before their override hook; doing
    that even transiently would violate the clean T094 production boundary.
    Search, result formatting, resilience and single-flight behavior continue
    to use the already-accepted base-class methods.
    """

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
        self.t068 = v4_fixed20_core
        self.legacy_admin_store = legacy_admin_store

        artifacts = self.manifest["artifacts"]
        self.db_path = resolve_artifact(artifacts["corpus"])
        self.retrieval_units_path = resolve_artifact(artifacts["retrieval_units"])
        self.fts_path = resolve_artifact(artifacts["fts"])
        self.vector_path = resolve_artifact(artifacts["document_vectors"])
        self.mapping_path = resolve_artifact(artifacts["row_mapping"])
        self.concept_families_path = resolve_artifact(artifacts["concept_families"])

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
            self.vector_path, mmap_mode="r", allow_pickle=False
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
                self.query_embedder = ResilientQwenQueryEmbedder(self.t068)
            except RuntimeError as exc:
                self.query_embedder = None
                self.embedding_configuration_error = str(exc)

        self._search_state_lock = Lock()
        self._search_inflight: dict[str, Future[dict[str, Any]]] = {}
        self._search_call_state = local()

    def _validate_manifest(self, *, validate_hashes: bool) -> None:
        if (
            self.manifest.get("schema_version") != RUNTIME_MANIFEST_SCHEMA_VERSION
            or self.manifest.get("status") != "local_shadow_ready"
            or self.manifest.get("runtime_id") != RUNTIME_ID
            or self.manifest.get("algorithm_snapshot_id") != ALGORITHM_SNAPSHOT_ID
        ):
            raise RuntimeError("T094 runtime manifest identity changed")
        authorization = self.manifest["authorization"]
        if (
            not authorization["v3_remains_default"]
            or authorization["cloud_activation_authorized"]
            or authorization["deployment_authorized"]
            or authorization["service_restart_authorized"]
            or authorization["knowledge_graph_build_authorized"]
            or authorization["cloud_sync_required"]
        ):
            raise RuntimeError("T094 runtime authorization boundary changed")
        if not validate_hashes:
            return
        for item in self.manifest["artifacts"].values():
            if sha256_file(resolve_artifact(item)) != item["sha256"]:
                raise RuntimeError(f"T094 runtime artifact changed: {item['path']}")
        sources = self.manifest.get("runtime_sources") or {}
        closure = sources.get("python_import_closure")
        if not isinstance(closure, Mapping):
            raise RuntimeError("T094 Python import closure is missing")
        validate_t094_runtime_import_closure(closure)
        closure_paths = set((closure.get("files") or {}).keys())
        missing_core = sorted(set(REQUIRED_RUNTIME_SOURCE_PATHS) - closure_paths)
        if missing_core:
            raise RuntimeError(f"T094 clean runtime source is missing: {missing_core}")

    def _load_documents(self) -> dict[str, dict[str, Any]]:
        with readonly_connection(self.db_path) as connection:
            return {
                row["document_id"]: dict(row)
                for row in connection.execute("select * from documents")
            }

    def _search_key(self, payload: dict[str, Any]) -> str:
        base_key = super()._search_key(payload)
        envelope = payload.get(T092_DECISION_REQUEST_KEY)
        transport_sha256 = (
            envelope.get("transport_sha256")
            if isinstance(envelope, Mapping)
            else None
        )
        contract = {
            "base_search_key": base_key,
            "decision_envelope_present": T092_DECISION_REQUEST_KEY in payload,
            "technical_sufficiency_decision_transport_sha256": transport_sha256,
        }
        encoded = json.dumps(
            contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _decision_status(envelope: Mapping[str, Any]) -> str:
        return "accepted" if envelope.get("decision_payload") is not None else "not_applicable"

    @staticmethod
    def _trace_from_response(response: Mapping[str, Any]) -> Mapping[str, Any] | None:
        coverage = response.get("coverage")
        if not isinstance(coverage, Mapping):
            return None
        query_plan = coverage.get("query_plan")
        if not isinstance(query_plan, Mapping):
            return None
        traces = query_plan.get("structure_traces")
        if not isinstance(traces, Mapping):
            return None
        trace = traces.get(TRACE_KEY)
        return trace if isinstance(trace, Mapping) else None

    def _handshake(
        self,
        *,
        status: str,
        envelope: Mapping[str, Any] | None,
        reason: str | None = None,
        trace_decision_sha256: Any | None = None,
    ) -> dict[str, Any]:
        value = {
            "status": status,
            "decision_version": T092_DECISION_VERSION,
            "runtime_id": RUNTIME_ID,
            "decision_status": (
                self._decision_status(envelope) if envelope is not None else "implicit"
            ),
            "decision_sha256": (
                envelope.get("decision_sha256") if envelope is not None else None
            ),
            "transport_sha256": (
                envelope.get("transport_sha256") if envelope is not None else None
            ),
            "trace_decision_sha256": trace_decision_sha256,
            "trace_key": TRACE_KEY,
        }
        if reason:
            value["reason"] = reason
        return value

    def _fail_closed_response(
        self,
        payload: Mapping[str, Any],
        *,
        envelope: Mapping[str, Any] | None,
        reason: str,
    ) -> dict[str, Any]:
        handshake = self._handshake(
            status="rejected", envelope=envelope, reason=reason
        )
        return {
            "query": str(payload.get("query") or ""),
            "results": [],
            "retrieval": {
                "full_text_hits": 0,
                "vector_hits": 0,
                "candidate_count": 0,
                "fixed20_used": 0,
                "decision_handshake_rejected": 1,
                T092_DECISION_HANDSHAKE_KEY: handshake,
            },
            "coverage": {
                "has_clause_level_evidence": False,
                "has_page_level_evidence": False,
                "needs_web_supplement": False,
                "notes": ["T092技术充分性决策握手校验失败，已安全停止检索。"],
                "query_plan": {T092_DECISION_HANDSHAKE_KEY: handshake},
                T092_DECISION_HANDSHAKE_KEY: handshake,
            },
        }

    @staticmethod
    def _trace_matches_envelope(
        trace: Mapping[str, Any], envelope: Mapping[str, Any]
    ) -> bool:
        decision_payload = envelope.get("decision_payload")
        expected_operator = (
            decision_payload.get("operator")
            if isinstance(decision_payload, Mapping)
            else None
        )
        expected_refs = (
            decision_payload.get("evidence_refs")
            if isinstance(decision_payload, Mapping)
            else []
        )
        return (
            trace.get("decision_sha256") == envelope.get("decision_sha256")
            and trace.get("operator") == expected_operator
            and trace.get("evidence_refs") == expected_refs
        )

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_envelope = payload.get(T092_DECISION_REQUEST_KEY)
        if T092_DECISION_REQUEST_KEY not in payload:
            return self._fail_closed_response(
                payload, envelope=None, reason="decision_envelope_required"
            )
        if not isinstance(raw_envelope, Mapping):
            return self._fail_closed_response(
                payload, envelope=None, reason="decision_envelope_not_an_object"
            )
        envelope = dict(raw_envelope)
        decision_question = envelope.get("decision_question")
        if not isinstance(decision_question, str):
            return self._fail_closed_response(
                payload,
                envelope=envelope,
                reason="decision_question_missing_or_invalid",
            )
        local_decision = compile_technical_sufficiency_decision_t092(
            decision_question
        )
        expected = build_t092_decision_envelope(decision_question, local_decision)
        if envelope != expected:
            return self._fail_closed_response(
                payload,
                envelope=envelope,
                reason="decision_envelope_field_mismatch",
            )

        token = self.t068.bind_verified_decision(decision_question, local_decision)
        try:
            response = super().search(payload)
        finally:
            self.t068.reset_verified_decision(token)
        trace = self._trace_from_response(response)
        decision_status = self._decision_status(envelope)
        if decision_status == "accepted" and (
            trace is None or not self._trace_matches_envelope(trace, envelope)
        ):
            return self._fail_closed_response(
                payload,
                envelope=envelope,
                reason="response_decision_trace_mismatch",
            )
        if (
            decision_status == "not_applicable"
            and trace is not None
            and not self._trace_matches_envelope(trace, envelope)
        ):
            return self._fail_closed_response(
                payload,
                envelope=envelope,
                reason="response_not_applicable_trace_mismatch",
            )
        handshake = self._handshake(
            status="verified",
            envelope=envelope,
            trace_decision_sha256=(
                trace.get("decision_sha256") if trace is not None else None
            ),
        )
        response.setdefault("retrieval", {})[T092_DECISION_HANDSHAKE_KEY] = handshake
        response.setdefault("coverage", {})[T092_DECISION_HANDSHAKE_KEY] = handshake
        return response


__all__ = (
    "ALGORITHM_SNAPSHOT_ID",
    "DEFAULT_RUNTIME_MANIFEST",
    "REQUIRED_RUNTIME_SOURCE_PATHS",
    "RUNTIME_ID",
    "T094V4KnowledgeStore",
)
