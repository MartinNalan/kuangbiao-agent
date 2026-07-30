from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "promote_v4_qwen_document_embeddings.py"
SPEC = importlib.util.spec_from_file_location(
    "promote_v4_qwen_document_embeddings", SCRIPT
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

ARTIFACT_ROOT = (
    ROOT
    / "data"
    / "knowledge_base_v4"
    / "embedding_artifacts_private"
    / "qwen37_text_embedding_1024_t027_v1"
)
MANIFEST_PATH = ARTIFACT_ROOT / "manifest.json"
MAPPING_PATH = ARTIFACT_ROOT / "row_mapping.jsonl"
VECTOR_PATH = ARTIFACT_ROOT / "document_embeddings.npy"


class QwenDocumentEmbeddingPromotionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_artifact_is_private_reusable_and_not_production_or_ann(self) -> None:
        manifest = self.manifest
        self.assertEqual(manifest["schema_version"], MODULE.SCHEMA_VERSION)
        self.assertEqual(manifest["artifact_status"], "frozen_snapshot_reusable")
        self.assertTrue(manifest["private_asset"])
        self.assertFalse(manifest["production_activated"])
        self.assertFalse(manifest["ann_index"])
        self.assertFalse(manifest["knowledge_graph"])
        self.assertFalse(manifest["cloud_sync_required"])

    def test_model_shape_and_hash_are_frozen(self) -> None:
        manifest = self.manifest
        self.assertEqual(manifest["model"], "qwen3.7-text-embedding")
        self.assertEqual(manifest["dimension"], 1024)
        self.assertEqual(manifest["row_count"], 23250)
        self.assertEqual(manifest["vector_dtype"], "float32")
        self.assertEqual(manifest["vector_shape"], [23250, 1024])
        self.assertEqual(
            manifest["vector_sha256"],
            "8a5d0b30858e6e0d586712492089884bbba1cc06bfdc755ea9ed62c84771644f",
        )
        self.assertEqual(MODULE.sha256_file(VECTOR_PATH), manifest["vector_sha256"])
        vectors = np.load(VECTOR_PATH, mmap_mode="r", allow_pickle=False)
        self.assertEqual(vectors.shape, (23250, 1024))
        self.assertEqual(vectors.dtype, np.float32)

    def test_artifact_is_bound_to_current_corpus_and_t027_leaves(self) -> None:
        manifest = self.manifest
        self.assertEqual(manifest["corpus_sha256"], MODULE.EXPECTED_DB_SHA256)
        self.assertEqual(
            manifest["retrieval_units_sha256"],
            MODULE.EXPECTED_RETRIEVAL_UNITS_SHA256,
        )
        self.assertEqual(
            manifest["document_embedding_input_sha256"],
            "de12bfc282b92c3a28cc227216782e9282968fa1b98ce4ae7985823fb38b6fe1",
        )
        checks = manifest["database_checks"]
        self.assertEqual(checks["integrity_check"], "ok")
        self.assertEqual(checks["foreign_key_violation_count"], 0)
        self.assertEqual(checks["document_count"], 156)
        self.assertEqual(checks["page_count"], 3645)
        self.assertEqual(checks["content_unit_count"], 20670)

    def test_row_mapping_has_one_unique_row_per_leaf_and_no_source_text(self) -> None:
        rows = [
            json.loads(line)
            for line in MAPPING_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(rows), 23250)
        self.assertEqual([row["row_index"] for row in rows], list(range(23250)))
        self.assertEqual(len({row["retrieval_unit_id"] for row in rows}), 23250)
        self.assertEqual(
            MODULE.sha256_file(MAPPING_PATH), self.manifest["row_mapping_sha256"]
        )
        forbidden = {"text", "clean_text", "search_text", "citation_text"}
        self.assertTrue(all(not (forbidden & set(row)) for row in rows))
        self.assertFalse(self.manifest["row_mapping_contains_source_text"])

    def test_reuse_contract_requires_reembedding_only_changed_rows(self) -> None:
        contract = self.manifest["reuse_contract"]
        self.assertTrue(contract["snapshot_is_immutable_by_hash"])
        self.assertTrue(contract["active_corpus_may_evolve"])
        self.assertIn("search_text_sha256 unchanged", contract["reuse_unchanged_rows_when"])
        self.assertIn("new retrieval leaf", contract["reembed_when"])
        self.assertIn(
            "leaf text or metadata input changed", contract["reembed_when"]
        )

    def test_validate_only_rechecks_source_and_promoted_artifact(self) -> None:
        args = MODULE.parser().parse_args([])
        validated = MODULE.validate_artifact(
            db_path=args.db.resolve(),
            retrieval_units=args.retrieval_units.resolve(),
            source_dir=args.source_dir.resolve(),
            destination=args.destination.resolve(),
        )
        self.assertEqual(validated["schema_version"], MODULE.SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
