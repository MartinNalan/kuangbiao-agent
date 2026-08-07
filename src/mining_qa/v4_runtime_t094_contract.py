from __future__ import annotations

"""Shared source-integrity contract for the T094 production runtime."""

from pathlib import Path
from typing import Any, Mapping

from .runtime_import_closure import (
    ImportClosureError,
    collect_repository_import_closure,
    validate_repository_import_closure,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Explicit process entrypoints are listed even when one currently imports
# another.  This keeps the manifest contract stable if API wiring is refactored
# without changing the set of deployed production processes.
T094_RUNTIME_ENTRYPOINT_MODULES = (
    "sitecustomize",
    "mining_qa.knowledge_service",
    "mining_qa.api",
    "mining_qa.agent",
    "mining_qa.v4_retrieval_store_t094",
    "mining_qa.v4_fixed20_core",
    "mining_qa.v4_retrieval_primitives",
)
T094_RUNTIME_SOURCE_ROOTS = ("src", "scripts")

# T094 has no accepted non-literal dynamic import.  Any future dynamic loader
# therefore fails closed until an exact call-site declaration is reviewed and
# added here.  Keeping this mapping in production source prevents a Manifest
# from weakening the rule by declaring its own unchecked exceptions.
T094_DYNAMIC_IMPORT_DECLARATIONS: Mapping[str, tuple[str, ...]] = {}
T094_FORBIDDEN_HISTORICAL_RUNTIME_PATHS = {
    "src/mining_qa/v4_retrieval_store_t090.py",
    "src/mining_qa/v4_retrieval_store_t092.py",
}


def assert_t094_runtime_source_boundary(closure: Mapping[str, Any]) -> None:
    raw_files = closure.get("files")
    if not isinstance(raw_files, Mapping):
        raise ImportClosureError("T094 production closure files are missing")
    declared_count = closure.get("file_count")
    if declared_count != len(raw_files):
        raise ImportClosureError(
            "T094 production closure file_count mismatch"
            f"; declared={declared_count}; actual={len(raw_files)}"
        )
    files = set(raw_files.keys())
    historical_scripts = sorted(path for path in files if path.startswith("scripts/"))
    historical_stores = sorted(files & T094_FORBIDDEN_HISTORICAL_RUNTIME_PATHS)
    if historical_scripts or historical_stores:
        raise ImportClosureError(
            "T094 production closure reached historical/experimental sources"
            f"; scripts={historical_scripts}; stores={historical_stores}"
        )


def collect_t094_runtime_import_closure(
    repository_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    closure = collect_repository_import_closure(
        repository_root=repository_root,
        entrypoint_modules=T094_RUNTIME_ENTRYPOINT_MODULES,
        source_roots=T094_RUNTIME_SOURCE_ROOTS,
        dynamic_import_declarations=T094_DYNAMIC_IMPORT_DECLARATIONS,
    )
    assert_t094_runtime_source_boundary(closure)
    return closure


def validate_t094_runtime_import_closure(
    expected: Mapping[str, Any],
    repository_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    closure = validate_repository_import_closure(
        expected,
        repository_root=repository_root,
        entrypoint_modules=T094_RUNTIME_ENTRYPOINT_MODULES,
        source_roots=T094_RUNTIME_SOURCE_ROOTS,
        dynamic_import_declarations=T094_DYNAMIC_IMPORT_DECLARATIONS,
    )
    assert_t094_runtime_source_boundary(closure)
    return closure
