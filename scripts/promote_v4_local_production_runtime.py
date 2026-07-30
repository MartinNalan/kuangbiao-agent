#!/usr/bin/env python3
"""Promote the accepted v4 retrieval artifacts into a local runtime bundle.

The bundle is private and local-only.  It copies the accepted FTS database out
of the experimental directory, pins every corpus/vector/runtime-source hash,
and writes a portable manifest.  It does not mutate corpus.sqlite, create an
ANN/KG, call a model, connect to the GeoWiki server, or activate cloud traffic.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import types
from typing import Any, Iterable

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

V4_ROOT = ROOT / "data" / "knowledge_base_v4"
DEFAULT_DB = V4_ROOT / "db" / "corpus.sqlite"
DEFAULT_UNITS = V4_ROOT / "retrieval_preprocessing_v1" / "retrieval_units_v1.jsonl"
DEFAULT_SOURCE_FTS = V4_ROOT / "retrieval_indexes_experimental" / "fts_bm25_v2.sqlite"
DEFAULT_DOCUMENT_ROOT = (
    V4_ROOT / "embedding_artifacts_private" / "qwen37_text_embedding_1024_t027_v1"
)
DEFAULT_CONCEPT_FAMILIES = ROOT / "schemas" / "v4_governed_concept_families_v1.json"
DEFAULT_RUNTIME_ROOT = V4_ROOT / "runtime_private" / "hybrid_fixed20_v1"
DEFAULT_RUNTIME_FTS = DEFAULT_RUNTIME_ROOT / "fts.sqlite"
DEFAULT_MANIFEST = DEFAULT_RUNTIME_ROOT / "runtime_manifest.json"
DEFAULT_ADAPTER = ROOT / "src" / "mining_qa" / "v4_retrieval_store.py"
DEFAULT_QUERY_ROUTING = ROOT / "src" / "mining_qa" / "governed_query_routing.py"
DEFAULT_QUERY_UNDERSTANDING = ROOT / "src" / "mining_qa" / "query_understanding.py"

SCHEMA_VERSION = "geowiki-v4-local-production-runtime.v1"
EXPECTED = {
    "corpus": "9f23c72ddc9406f3f526a0635c0b45321cd0d6d4f9ea36168d6ce38e4fcd1d97",
    "retrieval_units": "6d96def07f5846a7f8d264b1fcf8ea76eb7dc493333cb4566b45e5d014899e72",
    "source_fts": "4aa39d2121d5168690e42dbed099e33c5dd888207777b28a3b9b8e4cbeaf6c2f",
    "document_manifest": "736b6aad60e7e326741b151a64464f289b5dcd0d77faa63e8a33cbdde4c8c911",
    "document_vectors": "8a5d0b30858e6e0d586712492089884bbba1cc06bfdc755ea9ed62c84771644f",
    "row_mapping": "862ccce3305ec1f63584cb033434eed6f4c6d180a7604523e99f6c44a936074c",
    "concept_families": "f6c4f67f4bd617f747d3258bfe51696ec3e2b8980f80295b28e7890663dc1d23",
    "t068_runner": "db1e76131d5c4ed4a86b34284b272e6f683ea59b40e85138f7087bb5ff30efc1",
    "runtime_source_bundle": "aa6b1d868bc98b791297e25945c4547b7c97c6f7787c32ba1be9c66bf435fb5c",
}
MODEL = "qwen3.7-text-embedding"
DIMENSION = 1024
VECTOR_ROWS = 23_250
FTS_SCHEMA_VERSION = "v4-fts5-cjk-bigram-trigram.v2"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_pairs(pairs: Iterable[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for left, right in pairs:
        digest.update(left.encode("utf-8"))
        digest.update(b"\0")
        digest.update(right.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def relative(path: Path) -> str:
    return str(path.resolve().relative_to(ROOT))


def runtime_source_hashes() -> dict[str, str]:
    t068 = importlib.import_module("run_v4_generic_fixed20_shadow_replay")
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
        relative(path): sha256_file(path)
        for path in sorted(found)
    }


def validate_frozen_inputs(args: argparse.Namespace) -> dict[str, Any]:
    paths = {
        "corpus": args.db.resolve(),
        "retrieval_units": args.retrieval_units.resolve(),
        "source_fts": args.source_fts.resolve(),
        "document_manifest": args.document_root.resolve() / "manifest.json",
        "document_vectors": args.document_root.resolve() / "document_embeddings.npy",
        "row_mapping": args.document_root.resolve() / "row_mapping.jsonl",
        "concept_families": args.concept_families.resolve(),
        "t068_runner": SCRIPT_DIR / "run_v4_generic_fixed20_shadow_replay.py",
    }
    hashes = {name: sha256_file(path) for name, path in paths.items()}
    for name, expected in EXPECTED.items():
        if name == "runtime_source_bundle":
            continue
        if hashes[name] != expected:
            raise RuntimeError(f"T076 frozen input changed: {name} {hashes[name]}")

    source_hashes = runtime_source_hashes()
    bundle_hash = sha256_pairs(source_hashes.items())
    if bundle_hash != EXPECTED["runtime_source_bundle"]:
        raise RuntimeError(f"T076 runtime source bundle changed: {bundle_hash}")

    with sqlite3.connect(f"file:{paths['corpus']}?mode=ro&immutable=1", uri=True) as connection:
        integrity = connection.execute("pragma integrity_check").fetchone()[0]
        foreign_keys = len(connection.execute("pragma foreign_key_check").fetchall())
        counts = {
            "document_count": connection.execute("select count(*) from documents").fetchone()[0],
            "page_count": connection.execute("select count(*) from pages").fetchone()[0],
            "content_unit_count": connection.execute("select count(*) from content_units").fetchone()[0],
        }
    if integrity != "ok" or foreign_keys:
        raise RuntimeError("T076 v4 corpus integrity failed")
    if counts != {"document_count": 156, "page_count": 3645, "content_unit_count": 20670}:
        raise RuntimeError(f"T076 corpus counts changed: {counts}")

    with sqlite3.connect(f"file:{paths['source_fts']}?mode=ro&immutable=1", uri=True) as connection:
        fts_integrity = connection.execute("pragma integrity_check").fetchone()[0]
        fts_metadata = dict(connection.execute("select key,value from index_metadata"))
        fts_rows = connection.execute("select count(*) from retrieval_fts").fetchone()[0]
    if (
        fts_integrity != "ok"
        or fts_rows != VECTOR_ROWS
        or fts_metadata.get("schema_version") != FTS_SCHEMA_VERSION
        or fts_metadata.get("retrieval_units_sha256") != EXPECTED["retrieval_units"]
    ):
        raise RuntimeError("T076 FTS validation failed")

    document_manifest = json.loads(paths["document_manifest"].read_text(encoding="utf-8"))
    vectors = np.load(paths["document_vectors"], mmap_mode="r", allow_pickle=False)
    if (
        document_manifest.get("model") != MODEL
        or document_manifest.get("dimension") != DIMENSION
        or vectors.shape != (VECTOR_ROWS, DIMENSION)
        or vectors.dtype != np.float32
    ):
        raise RuntimeError("T076 document-vector contract changed")
    return {
        "paths": paths,
        "hashes": hashes,
        "source_hashes": source_hashes,
        "runtime_source_bundle_sha256": bundle_hash,
        "database": {"integrity_check": integrity, "foreign_key_violation_count": foreign_keys, **counts},
        "fts": {"integrity_check": fts_integrity, "row_count": fts_rows, "metadata": fts_metadata},
    }


def promote_fts(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if sha256_file(destination) != EXPECTED["source_fts"]:
            raise RuntimeError("T076 runtime FTS exists with an unexpected hash")
        return
    with tempfile.NamedTemporaryFile(
        prefix=".fts.", suffix=".sqlite", dir=destination.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        shutil.copyfile(source, temporary)
        if sha256_file(temporary) != EXPECTED["source_fts"]:
            raise RuntimeError("T076 copied FTS hash mismatch")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_manifest(args: argparse.Namespace, validation: dict[str, Any]) -> dict[str, Any]:
    runtime_fts = args.runtime_fts.resolve()
    adapter = args.adapter.resolve()
    routing = args.query_routing.resolve()
    understanding = args.query_understanding.resolve()
    if not adapter.exists():
        raise RuntimeError("T076 v4 runtime adapter has not been created")
    promoted_hash = sha256_file(runtime_fts)
    if promoted_hash != EXPECTED["source_fts"]:
        raise RuntimeError("T076 promoted FTS changed")
    runtime_sources = {
        "t068_runner": {"path": "scripts/run_v4_generic_fixed20_shadow_replay.py", "sha256": EXPECTED["t068_runner"]},
        "reachable_script_hashes": validation["source_hashes"],
        "bundle_sha256": validation["runtime_source_bundle_sha256"],
        "adapter": {"path": relative(adapter), "sha256": sha256_file(adapter)},
        "governed_query_routing": {"path": relative(routing), "sha256": sha256_file(routing)},
        "query_understanding": {"path": relative(understanding), "sha256": sha256_file(understanding)},
    }
    if args.base_adapter is not None:
        base_adapter = args.base_adapter.resolve()
        runtime_sources["base_adapter"] = {
            "path": relative(base_adapter),
            "sha256": sha256_file(base_adapter),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "local_shadow_ready",
        "created_at": utc_now(),
        "runtime_id": args.runtime_id,
        "artifacts": {
            "corpus": {"path": relative(validation["paths"]["corpus"]), "sha256": EXPECTED["corpus"]},
            "retrieval_units": {"path": relative(validation["paths"]["retrieval_units"]), "sha256": EXPECTED["retrieval_units"], "eligible_row_count": VECTOR_ROWS},
            "fts": {"path": relative(runtime_fts), "sha256": promoted_hash, "row_count": VECTOR_ROWS, "schema_version": FTS_SCHEMA_VERSION},
            "document_manifest": {"path": relative(validation["paths"]["document_manifest"]), "sha256": EXPECTED["document_manifest"]},
            "document_vectors": {"path": relative(validation["paths"]["document_vectors"]), "sha256": EXPECTED["document_vectors"], "shape": [VECTOR_ROWS, DIMENSION], "dtype": "float32"},
            "row_mapping": {"path": relative(validation["paths"]["row_mapping"]), "sha256": EXPECTED["row_mapping"], "row_count": VECTOR_ROWS},
            "concept_families": {"path": relative(validation["paths"]["concept_families"]), "sha256": EXPECTED["concept_families"]},
        },
        "runtime_sources": runtime_sources,
        "retrieval": {
            "lexical": {"method": "fts_document_router_then_document_local_fullscan", "document_top_n": 30, "leaf_top_k": 50},
            "dense": {"model": MODEL, "dimension": DIMENSION, "method": "exact_cosine", "top_k": 60, "ann_used": False},
            "fusion": {"method": "equal_rrf", "rrf_k": 60, "lexical_head_admission": 1, "dense_head_admission": 4},
            "final_pool": {"size": 20, "method": "governed_structural_reservation_then_unchanged_order_fill"},
            "knowledge_graph_used": False,
        },
        "database_checks": validation["database"],
        "authorization": {
            "local_runtime_build_authorized": True,
            "local_shadow_activation_authorized": True,
            "v3_remains_default": True,
            "cloud_activation_authorized": False,
            "deployment_authorized": False,
            "service_restart_authorized": False,
            "knowledge_graph_build_authorized": False,
            "cloud_sync_required": False,
        },
    }


def write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(stable_json(value), encoding="utf-8")
    os.replace(temporary, path)


def validate_manifest(args: argparse.Namespace, manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("status") != "local_shadow_ready":
        raise RuntimeError("T076 runtime manifest status changed")
    for item in manifest["artifacts"].values():
        path = ROOT / item["path"]
        if sha256_file(path) != item["sha256"]:
            raise RuntimeError(f"T076 runtime artifact changed: {item['path']}")
    for key in ("adapter", "governed_query_routing", "query_understanding"):
        item = manifest["runtime_sources"][key]
        if sha256_file(ROOT / item["path"]) != item["sha256"]:
            raise RuntimeError(f"T076 runtime source changed: {key}")
    base_adapter = manifest["runtime_sources"].get("base_adapter")
    if base_adapter and sha256_file(ROOT / base_adapter["path"]) != base_adapter["sha256"]:
        raise RuntimeError("T076 runtime source changed: base_adapter")
    source_hashes = runtime_source_hashes()
    if source_hashes != manifest["runtime_sources"]["reachable_script_hashes"]:
        raise RuntimeError("T076 reachable runtime source hashes changed")
    if sha256_pairs(source_hashes.items()) != manifest["runtime_sources"]["bundle_sha256"]:
        raise RuntimeError("T076 runtime source bundle changed")
    auth = manifest["authorization"]
    if (
        not auth["v3_remains_default"]
        or auth["cloud_activation_authorized"]
        or auth["deployment_authorized"]
        or auth["service_restart_authorized"]
        or auth["knowledge_graph_build_authorized"]
        or auth["cloud_sync_required"]
    ):
        raise RuntimeError("T076 authorization boundary changed")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    mode = value.add_mutually_exclusive_group(required=True)
    mode.add_argument("--build", action="store_true")
    mode.add_argument("--validate-only", action="store_true")
    value.add_argument("--db", type=Path, default=DEFAULT_DB)
    value.add_argument("--retrieval-units", type=Path, default=DEFAULT_UNITS)
    value.add_argument("--source-fts", type=Path, default=DEFAULT_SOURCE_FTS)
    value.add_argument("--document-root", type=Path, default=DEFAULT_DOCUMENT_ROOT)
    value.add_argument("--concept-families", type=Path, default=DEFAULT_CONCEPT_FAMILIES)
    value.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    value.add_argument("--runtime-fts", type=Path, default=DEFAULT_RUNTIME_FTS)
    value.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    value.add_argument("--adapter", type=Path, default=DEFAULT_ADAPTER)
    value.add_argument("--base-adapter", type=Path)
    value.add_argument("--runtime-id", default="v4-hybrid-fixed20-v1")
    value.add_argument("--query-routing", type=Path, default=DEFAULT_QUERY_ROUTING)
    value.add_argument("--query-understanding", type=Path, default=DEFAULT_QUERY_UNDERSTANDING)
    return value


def main() -> None:
    args = parser().parse_args()
    validation = validate_frozen_inputs(args)
    if args.build:
        promote_fts(args.source_fts.resolve(), args.runtime_fts.resolve())
        manifest = build_manifest(args, validation)
        write_atomic(args.manifest.resolve(), manifest)
    else:
        manifest = json.loads(args.manifest.resolve().read_text(encoding="utf-8"))
    validate_manifest(args, manifest)
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "runtime_id": manifest["runtime_id"],
                "database_checks": manifest["database_checks"],
                "retrieval": manifest["retrieval"],
                "authorization": manifest["authorization"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
