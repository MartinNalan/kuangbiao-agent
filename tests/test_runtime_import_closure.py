from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mining_qa.runtime_import_closure import (
    EXTERNAL_DYNAMIC_TARGET,
    ImportClosureError,
    collect_repository_import_closure,
    validate_repository_import_closure,
)
from mining_qa.v4_runtime_t094_contract import assert_t094_runtime_source_boundary


class RuntimeImportClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "src" / "demo" / "nested").mkdir(parents=True)
        (self.root / "scripts").mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative: str, value: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    def collect(self, entries=("demo.entry",), declarations=None):
        return collect_repository_import_closure(
            repository_root=self.root,
            entrypoint_modules=entries,
            source_roots=("src", "scripts"),
            dynamic_import_declarations=declarations,
        )

    def test_relative_from_imports_and_package_initializers_are_closed(self) -> None:
        self.write("src/demo/__init__.py", "MARKER = 1\n")
        self.write("src/demo/entry.py", "from .nested import helper\n")
        self.write("src/demo/nested/__init__.py", "from .helper import VALUE\n")
        self.write("src/demo/nested/helper.py", "VALUE = 3\n")

        closure = self.collect()

        self.assertEqual(
            set(closure["files"]),
            {
                "src/demo/__init__.py",
                "src/demo/entry.py",
                "src/demo/nested/__init__.py",
                "src/demo/nested/helper.py",
            },
        )
        self.assertEqual(closure["file_count"], 4)

    def test_literal_dynamic_module_is_included_without_importing_it(self) -> None:
        self.write("src/demo/__init__.py", "\n")
        self.write(
            "src/demo/entry.py",
            "import importlib\nPLUGIN = importlib.import_module('worker')\n",
        )
        self.write("scripts/worker.py", "raise RuntimeError('must not execute')\n")

        closure = self.collect()

        self.assertIn("scripts/worker.py", closure["files"])
        self.assertEqual(
            closure["dynamic_imports_observed"][0]["resolution"],
            "local_module:worker",
        )

    def test_nonliteral_dynamic_module_fails_closed_then_accepts_exact_declaration(self) -> None:
        self.write("src/demo/__init__.py", "\n")
        self.write(
            "src/demo/entry.py",
            "from importlib import import_module\n"
            "name = 'worker'\n"
            "PLUGIN = import_module(name)\n",
        )
        self.write("scripts/worker.py", "VALUE = 1\n")
        callsite = "src/demo/entry.py:3:9:importlib.import_module"

        with self.assertRaisesRegex(ImportClosureError, "unresolved dynamic import"):
            self.collect()

        closure = self.collect(declarations={callsite: ["worker"]})
        self.assertIn("scripts/worker.py", closure["files"])

    def test_explicit_external_declaration_and_stale_declarations_are_distinct(self) -> None:
        self.write("src/demo/__init__.py", "\n")
        self.write(
            "src/demo/entry.py",
            "import importlib\nname = get_name()\nimportlib.import_module(name)\n",
        )
        callsite = "src/demo/entry.py:3:0:importlib.import_module"
        closure = self.collect(
            declarations={callsite: [EXTERNAL_DYNAMIC_TARGET]}
        )
        self.assertEqual(closure["file_count"], 2)

        with self.assertRaisesRegex(ImportClosureError, "stale dynamic"):
            self.collect(
                declarations={
                    callsite: [EXTERNAL_DYNAMIC_TARGET],
                    "src/demo/entry.py:99:0:importlib.import_module": [
                        EXTERNAL_DYNAMIC_TARGET
                    ],
                }
            )

    def test_dynamic_code_execution_fails_closed(self) -> None:
        self.write("src/demo/__init__.py", "\n")
        self.write("src/demo/entry.py", "exec(make_source())\n")

        with self.assertRaisesRegex(ImportClosureError, "dynamic import.*exec"):
            self.collect()

    def test_literal_dynamic_file_loader_is_resolved(self) -> None:
        self.write("src/demo/__init__.py", "\n")
        self.write(
            "src/demo/entry.py",
            "import runpy\nrunpy.run_path('scripts/worker.py')\n",
        )
        self.write("scripts/worker.py", "VALUE = 1\n")

        closure = self.collect()

        self.assertIn("scripts/worker.py", closure["files"])
        self.assertEqual(
            closure["dynamic_imports_observed"][0]["resolution"],
            "local_file:scripts/worker.py",
        )

    def test_unresolved_repository_relative_import_fails_closed(self) -> None:
        self.write("src/demo/__init__.py", "\n")
        self.write("src/demo/entry.py", "from .missing import VALUE\n")

        with self.assertRaisesRegex(ImportClosureError, "unresolved repository-owned"):
            self.collect()

    def test_unresolved_relative_star_and_absolute_owned_imports_fail_closed(self) -> None:
        self.write("src/demo/__init__.py", "\n")
        self.write("src/demo/entry.py", "from .missing import *\n")
        with self.assertRaisesRegex(ImportClosureError, "demo.missing"):
            self.collect()

        self.write("src/demo/entry.py", "import demo.missing\n")
        with self.assertRaisesRegex(ImportClosureError, "demo.missing"):
            self.collect()

    def test_validation_detects_byte_changes_without_private_inputs(self) -> None:
        self.write("src/demo/__init__.py", "\n")
        self.write("src/demo/entry.py", "VALUE = 1\n")
        expected = self.collect()
        self.assertEqual(
            expected["files"]["src/demo/entry.py"],
            hashlib.sha256(b"VALUE = 1\n").hexdigest(),
        )

        self.write("src/demo/entry.py", "VALUE = 2\n")
        with self.assertRaisesRegex(ImportClosureError, "changed=.*entry.py"):
            validate_repository_import_closure(
                expected,
                repository_root=self.root,
                entrypoint_modules=("demo.entry",),
                source_roots=("src", "scripts"),
            )

    def test_t094_boundary_rejects_reachable_experiment_scripts(self) -> None:
        with self.assertRaisesRegex(ImportClosureError, "historical/experimental"):
            assert_t094_runtime_source_boundary(
                {
                    "file_count": 2,
                    "files": {
                        "src/mining_qa/v4_fixed20_core.py": "0" * 64,
                        "scripts/run_private_ab.py": "1" * 64,
                    }
                }
            )

    def test_t094_boundary_rejects_declared_file_count_drift(self) -> None:
        with self.assertRaisesRegex(ImportClosureError, "file_count mismatch"):
            assert_t094_runtime_source_boundary(
                {
                    "file_count": 2,
                    "files": {"src/mining_qa/v4_fixed20_core.py": "0" * 64},
                }
            )


if __name__ == "__main__":
    unittest.main()
