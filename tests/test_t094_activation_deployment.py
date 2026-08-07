from __future__ import annotations

from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy_v4_cloud.sh"
ENV_EXAMPLE = ROOT / ".env.example"
DEPLOY_README = ROOT / "deploy" / "README.md"
PYTHON_COMPAT = ROOT / "src" / "sitecustomize.py"


class T094ActivationDeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        cls.env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
        cls.deploy_readme = DEPLOY_README.read_text(encoding="utf-8")

    def test_deployment_script_has_valid_shell_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(DEPLOY_SCRIPT)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_embedded_python_preflights_are_syntactically_valid(self) -> None:
        snippets = re.findall(r"<<'PY'\n(.*?)\nPY\n", self.script, re.DOTALL)
        self.assertGreaterEqual(len(snippets), 6)
        for position, snippet in enumerate(snippets, start=1):
            try:
                compile(snippet, f"deploy_v4_cloud.sh:python-{position}", "exec")
            except SyntaxError as exc:
                self.fail(f"embedded Python #{position} is invalid: {exc}")

    def test_runtime_and_decision_are_pinned_as_one_pair(self) -> None:
        self.assertIn(
            'V4_MANIFEST_REL="data/knowledge_base_v4/runtime_private/'
            'hybrid_fixed20_t094_v1/runtime_manifest.json"',
            self.script,
        )
        self.assertIn(
            'EXPECTED_RUNTIME_ID="v4-hybrid-fixed20-p1fix-t094-v1"',
            self.script,
        )
        self.assertIn('EXPECTED_DECISION_VERSION="t092"', self.script)
        self.assertIn(
            'TECHNICAL_SUFFICIENCY_DECISION_VERSION="${expected_decision_version}"',
            self.script,
        )

    def test_private_asset_allowlist_is_seven_t092_assets_plus_t094_manifest(self) -> None:
        required = (
            "data/knowledge_base_v4/db/corpus.sqlite",
            "retrieval_preprocessing_t088_v1/retrieval_units_v1.jsonl",
            "runtime_private/hybrid_fixed20_t088_v1/fts.sqlite",
            "runtime_private/hybrid_fixed20_t094_v1/runtime_manifest.json",
            "embedding_artifacts_private/qwen37_text_embedding_1024_t088_v1/manifest.json",
            "embedding_artifacts_private/qwen37_text_embedding_1024_t088_v1/document_embeddings.npy",
            "embedding_artifacts_private/qwen37_text_embedding_1024_t088_v1/row_mapping.jsonl",
            "schemas/v4_governed_concept_families_v1.json",
        )
        for path in required:
            self.assertIn(path, self.script)
        self.assertNotIn("qwen37_text_embedding_1024_t027_v1", self.script)

    def test_import_closure_is_recomputed_and_hash_checked_locally_and_remotely(self) -> None:
        self.assertGreaterEqual(
            self.script.count('runtime_sources.get("python_import_closure")'),
            2,
        )
        self.assertGreaterEqual(
            self.script.count('"repository_static_python_import_closure"'),
            2,
        )
        self.assertGreaterEqual(
            self.script.count(
                'runtime_sources.get("gold_or_report_used_for_selection") is not False'
            ),
            2,
        )
        self.assertGreaterEqual(self.script.count('closure.get("file_count") != len(files)'), 2)
        self.assertGreaterEqual(self.script.count('closure.get("bundle_sha256")'), 2)
        self.assertGreaterEqual(self.script.count('closure_body.pop("closure_sha256", None)'), 2)
        self.assertGreaterEqual(
            self.script.count("validate_t094_runtime_import_closure(closure, root)"),
            2,
        )
        self.assertIn(
            '["git", "ls-files", "--error-unmatch", "--", relative]',
            self.script,
        )
        self.assertIn("Local T094 closure hash mismatch", self.script)
        self.assertIn("Remote T094 closure hash mismatch", self.script)

    def test_import_closure_excludes_scripts_and_historical_stores(self) -> None:
        self.assertGreaterEqual(
            self.script.count('str(relative).startswith("scripts/")'),
            2,
        )
        for forbidden in (
            "src/mining_qa/v4_retrieval_store_t090.py",
            "src/mining_qa/v4_retrieval_store_t092.py",
        ):
            self.assertGreaterEqual(self.script.count(forbidden), 2)
        self.assertIn("production closure must contain scripts=0", self.script)

    def test_rollback_restores_shared_corpus_before_kb_restart(self) -> None:
        restore = self.script.index('install -m 0600 "${backup}/corpus.sqlite"')
        kb_restart = self.script.index(
            'systemctl restart kuangbiao-kb.service', restore
        )
        self.assertLess(restore, kb_restart)
        self.assertIn('"application": app / "data/app/application.sqlite"', self.script)
        self.assertIn(
            '"candidates": app / "data/knowledge_base_v4/runtime_private/candidates.sqlite"',
            self.script,
        )
        self.assertIn("source_connection.backup(destination_connection)", self.script)

    def test_deeptutor_is_observed_but_never_restarted_or_stopped(self) -> None:
        self.assertIn("systemctl is-active --quiet deeptutor-cloud.service", self.script)
        self.assertIn("http://127.0.0.1:8001/", self.script)
        self.assertNotIn("systemctl restart deeptutor-cloud.service", self.script)
        self.assertNotIn("systemctl stop deeptutor-cloud.service", self.script)

    def test_example_configuration_selects_the_same_pair(self) -> None:
        self.assertIn(
            "V4_RUNTIME_MANIFEST=data/knowledge_base_v4/runtime_private/"
            "hybrid_fixed20_t094_v1/runtime_manifest.json",
            self.env_example,
        )
        self.assertIn("TECHNICAL_SUFFICIENCY_DECISION_VERSION=t092", self.env_example)

    def test_python310_compatibility_hook_is_deployed_with_application_source(self) -> None:
        self.assertTrue(PYTHON_COMPAT.is_file())
        self.assertIn("enum.StrEnum = StrEnum", PYTHON_COMPAT.read_text(encoding="utf-8"))

    def test_public_deployment_documentation_describes_t094_boundary(self) -> None:
        for marker in (
            "v4-hybrid-fixed20-p1fix-t094-v1",
            "seven accepted T092",
            "private retrieval assets byte-for-byte",
            "TECHNICAL_SUFFICIENCY_DECISION_VERSION=t092",
            "runtime_sources.python_import_closure",
            "zero `scripts/` files",
            "must not reach the",
            "T090 or T092 Store",
        ):
            self.assertIn(marker, self.deploy_readme)


if __name__ == "__main__":
    unittest.main()
