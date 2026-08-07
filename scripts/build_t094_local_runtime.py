#!/usr/bin/env python3
"""Build or validate the clean, asset-reusing T094 runtime Manifest.

Only the private Manifest is written.  The accepted corpus, retrieval units,
FTS database, document vectors, row mapping, and governed concept registry are
read-only inputs reused byte-for-byte from T092.  Production Python sources
are derived exclusively from the repository import graph; no Gold fixture,
evaluation result, or report participates in source discovery.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from mining_qa.runtime_import_closure import sha256_file  # noqa: E402
from mining_qa.technical_sufficiency_decision_t092 import (  # noqa: E402
    ALGORITHM_SNAPSHOT_ID,
)
from mining_qa.v4_runtime_t094_contract import (  # noqa: E402
    collect_t094_runtime_import_closure,
    validate_t094_runtime_import_closure,
)


TASK_ID = "T094"
RUNTIME_ID = "v4-hybrid-fixed20-p1fix-t094-v1"
ASSET_SNAPSHOT_ID = "t088-post-t087-v1"
BASE_RUNTIME_ID = "v4-hybrid-fixed20-p1fix-t092-v2"
BASE_T092_MANIFEST_SHA256 = (
    "c948bd3e784d1da48d6dd0e947cc14901e3d660ed4dfb799beb6bed8490cd1d5"
)
BASE_T092_MANIFEST = (
    ROOT
    / "data"
    / "knowledge_base_v4"
    / "runtime_private"
    / "hybrid_fixed20_t092_v2"
    / "runtime_manifest.json"
)
T094_MANIFEST = (
    ROOT
    / "data"
    / "knowledge_base_v4"
    / "runtime_private"
    / "hybrid_fixed20_t094_v1"
    / "runtime_manifest.json"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def resolve_artifact(item: dict[str, Any]) -> Path:
    path = Path(str(item["path"]))
    return path if path.is_absolute() else ROOT / path


def validate_base_manifest() -> tuple[dict[str, Any], str]:
    digest = sha256_file(BASE_T092_MANIFEST)
    if digest != BASE_T092_MANIFEST_SHA256:
        raise RuntimeError("frozen T092 base Manifest changed")
    base = read_json(BASE_T092_MANIFEST)
    if base.get("runtime_id") != BASE_RUNTIME_ID:
        raise RuntimeError("T092 base runtime identity changed")
    if base.get("asset_snapshot_id") != ASSET_SNAPSHOT_ID:
        raise RuntimeError("T092 asset snapshot changed")
    artifacts = base.get("artifacts")
    if not isinstance(artifacts, dict) or len(artifacts) != 7:
        raise RuntimeError("T092 must expose exactly seven accepted runtime assets")
    for label, item in artifacts.items():
        path = resolve_artifact(item)
        if sha256_file(path) != item.get("sha256"):
            raise RuntimeError(f"T092 asset changed: {label}")
    return base, digest


def _database_checks(corpus: Path) -> dict[str, Any]:
    connection = sqlite3.connect(
        f"file:{corpus.resolve()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        integrity = connection.execute("pragma integrity_check").fetchone()[0]
        foreign_keys = connection.execute("pragma foreign_key_check").fetchall()
        counts = {
            "documents": connection.execute("select count(*) from documents").fetchone()[0],
            "pages": connection.execute("select count(*) from pages").fetchone()[0],
            "content_units": connection.execute(
                "select count(*) from content_units"
            ).fetchone()[0],
        }
    finally:
        connection.close()
    return {
        **counts,
        "integrity_check": integrity,
        "foreign_key_violation_count": len(foreign_keys),
    }


def build_manifest(output_path: Path = T094_MANIFEST) -> dict[str, Any]:
    base, base_sha256 = validate_base_manifest()
    closure = collect_t094_runtime_import_closure(ROOT)
    authorization = deepcopy(base.get("authorization") or {})
    authorization.update(
        {
            "cloud_activation_authorized": False,
            "cloud_sync_required": False,
            "deployment_authorized": False,
            "local_runtime_build_authorized": True,
            "local_shadow_activation_authorized": True,
            "service_restart_authorized": False,
        }
    )
    retrieval = deepcopy(base["retrieval"])
    retrieval["final_pool"] = {
        "method": (
            "package_local_t094_clean_fixed20_then_shared_t092_candidate_only_"
            "technical_sufficiency_reservation_then_unchanged_order_fill"
        ),
        "size": 20,
        "candidate_expansion_used": False,
        "candidate_rescoring_used": False,
    }
    artifacts = deepcopy(base["artifacts"])
    runtime = {
        "schema_version": base["schema_version"],
        "created_at": utc_now(),
        "runtime_id": RUNTIME_ID,
        "asset_snapshot_id": ASSET_SNAPSHOT_ID,
        "algorithm_snapshot_id": ALGORITHM_SNAPSHOT_ID,
        "status": "local_shadow_ready",
        "authorization": authorization,
        "artifacts": artifacts,
        "database_checks": deepcopy(base["database_checks"]),
        "retrieval": retrieval,
        "reused_asset_manifest": {
            "path": str(BASE_T092_MANIFEST.relative_to(ROOT)),
            "sha256": base_sha256,
            "assets_reused_byte_for_byte": True,
            "source_metadata_only": True,
        },
        "refresh": {
            "task": TASK_ID,
            "source_corpus_sha256": artifacts["corpus"]["sha256"],
            "algorithm_changed": False,
            "implementation_extracted": True,
            "asset_rebuild": False,
            "retrieval_units_rebuilt": False,
            "fts_rebuilt": False,
            "document_vectors_rebuilt": False,
            "query_vector_artifact_required": False,
            "vector_rows_embedded": 0,
            "vector_rows_reused": artifacts["row_mapping"]["row_count"],
        },
        "runtime_sources": {
            "selection": "repository_static_python_import_closure",
            "gold_or_report_used_for_selection": False,
            "python_import_closure": closure,
        },
    }
    write_atomic(output_path, stable_json(runtime))
    if sha256_file(BASE_T092_MANIFEST) != base_sha256:
        raise RuntimeError("T092 base Manifest changed during T094 build")
    return validate_manifest(output_path, load_store=False)


class _OfflineEmbedder:
    def embed_query(self, _: str) -> Any:
        raise RuntimeError("offline Manifest validation does not embed queries")

    def close(self) -> None:
        return None


def validate_import_closure(
    manifest_path: Path = T094_MANIFEST,
) -> dict[str, Any]:
    runtime = read_json(manifest_path)
    if runtime.get("runtime_id") != RUNTIME_ID:
        raise RuntimeError("T094 runtime identity changed")
    runtime_sources = runtime.get("runtime_sources") or {}
    if runtime_sources.get("selection") != "repository_static_python_import_closure":
        raise RuntimeError("T094 runtime source-selection policy changed")
    if runtime_sources.get("gold_or_report_used_for_selection") is not False:
        raise RuntimeError("T094 source selection must not use Gold or reports")
    expected = runtime_sources.get("python_import_closure")
    if not isinstance(expected, dict):
        raise RuntimeError("T094 Python import closure is missing")
    closure = validate_t094_runtime_import_closure(expected, ROOT)
    return {
        "status": "validated",
        "runtime_id": RUNTIME_ID,
        "file_count": closure["file_count"],
        "bundle_sha256": closure["bundle_sha256"],
        "closure_sha256": closure["closure_sha256"],
        "entrypoints": [item["module"] for item in closure["entrypoints"]],
    }


def validate_manifest(
    manifest_path: Path = T094_MANIFEST,
    *,
    load_store: bool = True,
) -> dict[str, Any]:
    base, base_sha256 = validate_base_manifest()
    runtime = read_json(manifest_path)
    if runtime.get("runtime_id") != RUNTIME_ID:
        raise RuntimeError("T094 runtime identity changed")
    if runtime.get("asset_snapshot_id") != ASSET_SNAPSHOT_ID:
        raise RuntimeError("T094 reused asset snapshot changed")
    if runtime.get("algorithm_snapshot_id") != ALGORITHM_SNAPSHOT_ID:
        raise RuntimeError("T094 Decision algorithm snapshot changed")
    if runtime.get("artifacts") != base.get("artifacts"):
        raise RuntimeError("T094 did not reuse all T092 assets byte-for-byte")
    if "frozen_design_sources" in runtime:
        raise RuntimeError("T094 must not bind private design reports or tests")
    for label, item in runtime["artifacts"].items():
        if sha256_file(resolve_artifact(item)) != item["sha256"]:
            raise RuntimeError(f"T094 asset changed: {label}")
    checks = _database_checks(resolve_artifact(runtime["artifacts"]["corpus"]))
    if checks != runtime.get("database_checks"):
        raise RuntimeError("T094 database checks differ from the Manifest")
    closure = validate_import_closure(manifest_path)

    store_health: dict[str, Any] = {}
    if load_store:
        # Import lazily so ``--collect-import-closure`` remains independent of
        # private runtime files while the T094 Store is being developed.
        from mining_qa.v4_retrieval_store_t094 import T094V4KnowledgeStore

        store = T094V4KnowledgeStore(
            manifest_path,
            query_embedder=_OfflineEmbedder(),
            validate_hashes=True,
        )
        try:
            health = store.health()
        finally:
            store.close()
        store_health = {
            "runtime_id": health["runtime_id"],
            "retrieval_leaf_count": health["retrieval_leaf_count"],
            "vector_count": health["vector_count"],
        }

    if sha256_file(BASE_T092_MANIFEST) != base_sha256:
        raise RuntimeError("T092 base Manifest changed during T094 validation")
    return {
        "status": "validated",
        "task": TASK_ID,
        "runtime_id": RUNTIME_ID,
        "asset_snapshot_id": ASSET_SNAPSHOT_ID,
        "algorithm_snapshot_id": ALGORITHM_SNAPSHOT_ID,
        "base_t092_manifest_sha256": base_sha256,
        "t094_manifest_sha256": sha256_file(manifest_path),
        "artifact_paths_identical": runtime["artifacts"] == base["artifacts"],
        "asset_rebuild": False,
        "cloud_sync_required": False,
        "database_checks": checks,
        "import_closure": closure,
        "store_health": store_health,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    mode = value.add_mutually_exclusive_group(required=True)
    mode.add_argument("--build", action="store_true")
    mode.add_argument("--validate", action="store_true")
    mode.add_argument("--validate-manifest-only", action="store_true")
    mode.add_argument("--collect-import-closure", action="store_true")
    mode.add_argument("--validate-import-closure", action="store_true")
    value.add_argument("--manifest", type=Path, default=T094_MANIFEST)
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.collect_import_closure:
        result = collect_t094_runtime_import_closure(ROOT)
    elif args.validate_import_closure:
        result = validate_import_closure(args.manifest)
    elif args.build:
        result = build_manifest(args.manifest)
    else:
        result = validate_manifest(
            args.manifest,
            load_store=not args.validate_manifest_only,
        )
    print(stable_json(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
