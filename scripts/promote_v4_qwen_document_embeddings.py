#!/usr/bin/env python3
"""Promote T036 document vectors into a durable private snapshot artifact.

The promoted artifact contains the normalized float32 matrix, a row-to-leaf
mapping without source text, and a manifest bound to corpus, retrieval-leaf,
embedding-input, model, and vector hashes.  It is reusable and versioned, but
is neither a production index nor an ANN structure.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
V4_ROOT = PROJECT_ROOT / "data" / "knowledge_base_v4"
DEFAULT_DB = V4_ROOT / "db" / "corpus.sqlite"
DEFAULT_RETRIEVAL_UNITS = (
    V4_ROOT / "retrieval_preprocessing_v1" / "retrieval_units_v1.jsonl"
)
DEFAULT_SOURCE_DIR = (
    V4_ROOT
    / "retrieval_indexes_experimental"
    / "qwen37_text_embedding_1024_v1"
)
DEFAULT_DESTINATION = (
    V4_ROOT
    / "embedding_artifacts_private"
    / "qwen37_text_embedding_1024_t027_v1"
)
EXPECTED_DB_SHA256 = (
    "9f23c72ddc9406f3f526a0635c0b45321cd0d6d4f9ea36168d6ce38e4fcd1d97"
)
EXPECTED_RETRIEVAL_UNITS_SHA256 = (
    "6d96def07f5846a7f8d264b1fcf8ea76eb7dc493333cb4566b45e5d014899e72"
)
EXPECTED_VECTOR_SHA256 = (
    "8a5d0b30858e6e0d586712492089884bbba1cc06bfdc755ea9ed62c84771644f"
)
MODEL = "qwen3.7-text-embedding"
DIMENSION = 1024
ROW_COUNT = 23250
SCHEMA_VERSION = "geowiki-v4-durable-document-embedding-artifact.v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_paths(source_dir: Path) -> dict[str, Path]:
    return {
        "vector": source_dir / "document_embeddings.npy",
        "manifest": source_dir / "document_embedding_manifest.json",
    }


def artifact_paths(destination: Path) -> dict[str, Path]:
    return {
        "vector": destination / "document_embeddings.npy",
        "mapping": destination / "row_mapping.jsonl",
        "manifest": destination / "manifest.json",
    }


def database_checks(db_path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    try:
        integrity = connection.execute("pragma integrity_check").fetchone()[0]
        foreign_keys = len(connection.execute("pragma foreign_key_check").fetchall())
        document_count = connection.execute("select count(*) from documents").fetchone()[0]
        page_count = connection.execute("select count(*) from pages").fetchone()[0]
        unit_count = connection.execute("select count(*) from content_units").fetchone()[0]
    finally:
        connection.close()
    return {
        "integrity_check": integrity,
        "foreign_key_violation_count": foreign_keys,
        "document_count": document_count,
        "page_count": page_count,
        "content_unit_count": unit_count,
    }


def load_mapping_rows(retrieval_units: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with retrieval_units.open(encoding="utf-8") as handle:
        for source_line, line in enumerate(handle, 1):
            if not line.strip():
                continue
            payload = json.loads(line)
            if not payload.get("search_eligible", False):
                continue
            rows.append(
                {
                    "row_index": len(rows),
                    "source_line": source_line,
                    "retrieval_unit_id": payload["retrieval_unit_id"],
                    "document_id": payload["document_id"],
                    "search_text_sha256": payload["search_text_sha256"],
                    "citation_text_sha256": payload["citation_text_sha256"],
                    "citation_eligible": bool(payload["citation_eligible"]),
                    "source_unit_ids": payload.get("source_unit_ids") or [],
                }
            )
    if len(rows) != ROW_COUNT:
        raise RuntimeError(f"expected {ROW_COUNT} mapping rows, found {len(rows)}")
    if len({row["retrieval_unit_id"] for row in rows}) != len(rows):
        raise RuntimeError("duplicate retrieval unit in embedding mapping")
    return rows


def write_mapping(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def validate_source(
    *,
    db_path: Path,
    retrieval_units: Path,
    source_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if sha256_file(db_path) != EXPECTED_DB_SHA256:
        raise RuntimeError("v4 corpus hash changed")
    if sha256_file(retrieval_units) != EXPECTED_RETRIEVAL_UNITS_SHA256:
        raise RuntimeError("T027 retrieval leaves changed")
    paths = source_paths(source_dir)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    if manifest["model"] != MODEL or manifest["dimension"] != DIMENSION:
        raise RuntimeError("source embedding model contract changed")
    if manifest["row_count"] != ROW_COUNT or manifest["completed_count"] != ROW_COUNT:
        raise RuntimeError("source embedding row count changed")
    if manifest["vector_sha256"] != EXPECTED_VECTOR_SHA256:
        raise RuntimeError("source embedding manifest hash changed")
    if sha256_file(paths["vector"]) != EXPECTED_VECTOR_SHA256:
        raise RuntimeError("source embedding vector hash changed")
    vectors = np.load(paths["vector"], mmap_mode="r", allow_pickle=False)
    if vectors.shape != (ROW_COUNT, DIMENSION) or vectors.dtype != np.float32:
        raise RuntimeError("source embedding vector shape or dtype changed")
    norms = np.linalg.norm(vectors, axis=1)
    if float(np.max(np.abs(norms - 1.0))) > 1e-4:
        raise RuntimeError("source vectors are not normalized")
    checks = database_checks(db_path)
    if checks["integrity_check"] != "ok" or checks["foreign_key_violation_count"]:
        raise RuntimeError("v4 database integrity check failed")
    return manifest, checks


def validate_artifact(
    *,
    db_path: Path,
    retrieval_units: Path,
    source_dir: Path,
    destination: Path,
) -> dict[str, Any]:
    source_manifest, checks = validate_source(
        db_path=db_path,
        retrieval_units=retrieval_units,
        source_dir=source_dir,
    )
    paths = artifact_paths(destination)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise RuntimeError("durable embedding artifact schema changed")
    expected = {
        "corpus_sha256": EXPECTED_DB_SHA256,
        "retrieval_units_sha256": EXPECTED_RETRIEVAL_UNITS_SHA256,
        "model": MODEL,
        "dimension": DIMENSION,
        "row_count": ROW_COUNT,
        "vector_sha256": EXPECTED_VECTOR_SHA256,
        "document_embedding_input_sha256": source_manifest["input_sha256"],
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise RuntimeError(f"durable embedding artifact changed: {key}")
    if sha256_file(paths["vector"]) != manifest["vector_sha256"]:
        raise RuntimeError("durable vector hash changed")
    if sha256_file(paths["mapping"]) != manifest["row_mapping_sha256"]:
        raise RuntimeError("durable row mapping hash changed")
    with paths["mapping"].open(encoding="utf-8") as handle:
        mapping_count = sum(1 for line in handle if line.strip())
    if mapping_count != ROW_COUNT:
        raise RuntimeError("durable row mapping count changed")
    if manifest["database_checks"] != checks:
        raise RuntimeError("durable database checks changed")
    vectors = np.load(paths["vector"], mmap_mode="r", allow_pickle=False)
    if vectors.shape != (ROW_COUNT, DIMENSION):
        raise RuntimeError("durable vector shape changed")
    return manifest


def promote(args: argparse.Namespace) -> dict[str, Any]:
    db_path = args.db.resolve()
    retrieval_units = args.retrieval_units.resolve()
    source_dir = args.source_dir.resolve()
    destination = args.destination.resolve()
    if destination.exists():
        return validate_artifact(
            db_path=db_path,
            retrieval_units=retrieval_units,
            source_dir=source_dir,
            destination=destination,
        )
    source_manifest, checks = validate_source(
        db_path=db_path,
        retrieval_units=retrieval_units,
        source_dir=source_dir,
    )
    mapping_rows = load_mapping_rows(retrieval_units)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        stage_paths = artifact_paths(staging)
        source_vector = source_paths(source_dir)["vector"]
        try:
            os.link(source_vector, stage_paths["vector"])
            storage_mode = "hardlink_same_filesystem"
        except OSError:
            shutil.copy2(source_vector, stage_paths["vector"])
            storage_mode = "independent_copy"
        write_mapping(stage_paths["mapping"], mapping_rows)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "created_at": utc_now(),
            "artifact_type": "durable_private_document_embeddings",
            "artifact_status": "frozen_snapshot_reusable",
            "private_asset": True,
            "production_activated": False,
            "ann_index": False,
            "knowledge_graph": False,
            "model": MODEL,
            "dimension": DIMENSION,
            "output_type": "dense",
            "text_type": "document",
            "row_count": ROW_COUNT,
            "vector_dtype": "float32",
            "vector_shape": [ROW_COUNT, DIMENSION],
            "vector_sha256": EXPECTED_VECTOR_SHA256,
            "vector_size_bytes": stage_paths["vector"].stat().st_size,
            "storage_mode": storage_mode,
            "row_mapping_sha256": sha256_file(stage_paths["mapping"]),
            "row_mapping_count": len(mapping_rows),
            "row_mapping_contains_source_text": False,
            "corpus_sha256": EXPECTED_DB_SHA256,
            "retrieval_units_sha256": EXPECTED_RETRIEVAL_UNITS_SHA256,
            "document_embedding_input_sha256": source_manifest["input_sha256"],
            "embedding_text_contract": (
                "document title + standard/document number + section/clause location + "
                "T027 search_text"
            ),
            "source_manifest_sha256": sha256_file(
                source_paths(source_dir)["manifest"]
            ),
            "database_checks": checks,
            "reuse_contract": {
                "snapshot_is_immutable_by_hash": True,
                "active_corpus_may_evolve": True,
                "reuse_unchanged_rows_when": [
                    "search_text_sha256 unchanged",
                    "embedding_text_contract unchanged",
                    "model and dimension unchanged",
                ],
                "reembed_when": [
                    "new retrieval leaf",
                    "leaf text or metadata input changed",
                    "leaf removed or superseded",
                    "embedding model, dimension, or text contract changed",
                ],
            },
            "cloud_sync_required": False,
        }
        stage_paths["manifest"].write_text(stable_json(manifest), encoding="utf-8")
        os.replace(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return validate_artifact(
        db_path=db_path,
        retrieval_units=retrieval_units,
        source_dir=source_dir,
        destination=destination,
    )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--db", type=Path, default=DEFAULT_DB)
    value.add_argument("--retrieval-units", type=Path, default=DEFAULT_RETRIEVAL_UNITS)
    value.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    value.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    value.add_argument("--validate-only", action="store_true")
    return value


def main() -> None:
    args = parser().parse_args()
    result = (
        validate_artifact(
            db_path=args.db.resolve(),
            retrieval_units=args.retrieval_units.resolve(),
            source_dir=args.source_dir.resolve(),
            destination=args.destination.resolve(),
        )
        if args.validate_only
        else promote(args)
    )
    print(
        json.dumps(
            {
                "status": "validated" if args.validate_only else "promoted",
                "artifact_status": result["artifact_status"],
                "model": result["model"],
                "dimension": result["dimension"],
                "row_count": result["row_count"],
                "vector_sha256": result["vector_sha256"],
                "storage_mode": result["storage_mode"],
                "production_activated": result["production_activated"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
