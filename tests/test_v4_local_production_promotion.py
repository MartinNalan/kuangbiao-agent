from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "promote_v4_local_production_runtime.py"
SPEC = importlib.util.spec_from_file_location(
    "promote_v4_local_production_runtime", SCRIPT
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class V4LocalProductionPromotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        runtime_root = (
            ROOT
            / "data"
            / "knowledge_base_v4"
            / "runtime_private"
            / "hybrid_fixed20_v4"
        )
        cls.args = MODULE.parser().parse_args(
            [
                "--validate-only",
                "--runtime-root",
                str(runtime_root),
                "--runtime-fts",
                str(runtime_root / "fts.sqlite"),
                "--manifest",
                str(runtime_root / "runtime_manifest.json"),
                "--adapter",
                str(ROOT / "src" / "mining_qa" / "v4_retrieval_store_v2.py"),
                "--base-adapter",
                str(ROOT / "src" / "mining_qa" / "v4_retrieval_store.py"),
                "--runtime-id",
                "v4-hybrid-fixed20-p1fix-v4",
            ]
        )
        cls.validation = MODULE.validate_frozen_inputs(cls.args)
        cls.manifest = json.loads(
            cls.args.manifest.resolve().read_text(encoding="utf-8")
        )
        MODULE.validate_manifest(cls.args, cls.manifest)

    def test_runtime_artifacts_are_hash_pinned(self) -> None:
        self.assertEqual(self.manifest["status"], "local_shadow_ready")
        self.assertEqual(
            self.manifest["artifacts"]["corpus"]["sha256"],
            MODULE.EXPECTED["corpus"],
        )
        self.assertEqual(
            self.manifest["artifacts"]["retrieval_units"]["eligible_row_count"],
            23250,
        )
        self.assertEqual(
            self.manifest["artifacts"]["document_vectors"]["shape"],
            [23250, 1024],
        )
        self.assertEqual(
            self.manifest["runtime_sources"]["bundle_sha256"],
            MODULE.EXPECTED["runtime_source_bundle"],
        )

    def test_promoted_fts_is_private_runtime_copy_with_same_hash(self) -> None:
        promoted = ROOT / self.manifest["artifacts"]["fts"]["path"]
        self.assertNotEqual(promoted.resolve(), self.args.source_fts.resolve())
        self.assertEqual(MODULE.sha256_file(promoted), MODULE.EXPECTED["source_fts"])
        with sqlite3.connect(
            f"file:{promoted.resolve()}?mode=ro&immutable=1", uri=True
        ) as connection:
            self.assertEqual(connection.execute("pragma integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                connection.execute("select count(*) from retrieval_fts").fetchone()[0],
                23250,
            )

    def test_corpus_remains_read_only_and_has_no_graph_tables(self) -> None:
        db = ROOT / self.manifest["artifacts"]["corpus"]["path"]
        with sqlite3.connect(
            f"file:{db.resolve()}?mode=ro&immutable=1", uri=True
        ) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "select name from sqlite_master where type='table'"
                )
            }
            self.assertEqual(connection.execute("pragma integrity_check").fetchone()[0], "ok")
        self.assertTrue(
            {"kg_entities", "kg_relations", "document_relations", "clause_effects"}.isdisjoint(tables)
        )
        self.assertEqual(self.validation["database"]["document_count"], 156)
        self.assertEqual(self.validation["database"]["content_unit_count"], 20670)

    def test_v3_default_and_cloud_boundaries_are_frozen(self) -> None:
        authorization = self.manifest["authorization"]
        self.assertTrue(authorization["v3_remains_default"])
        self.assertTrue(authorization["local_shadow_activation_authorized"])
        self.assertFalse(authorization["cloud_activation_authorized"])
        self.assertFalse(authorization["deployment_authorized"])
        self.assertFalse(authorization["service_restart_authorized"])
        self.assertFalse(authorization["knowledge_graph_build_authorized"])
        self.assertFalse(authorization["cloud_sync_required"])
        with patch.dict(os.environ, {}, clear=True):
            from mining_qa.knowledge_service import runtime_version_from_env

            self.assertEqual(runtime_version_from_env(), "v3")


if __name__ == "__main__":
    unittest.main()
