from __future__ import annotations

"""Deterministic, fail-closed Python import closures for production manifests.

The collector is intentionally static.  It starts from named production entry
modules, parses every reachable repository-owned Python source file, and hashes
the resulting closure.  It never imports an entry module and therefore does not
need private data, evaluation fixtures, or network access.

Dynamic import sites must either use a literal module/file target that can be
resolved deterministically or have an explicit call-site declaration.  This is
important for deployment manifests: an unresolvable dynamic loader is an error,
not an invitation to fall back to a hand-maintained partial source list.
"""

import ast
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "geowiki.repository-python-import-closure.v1"
EXTERNAL_DYNAMIC_TARGET = "@external"


class ImportClosureError(RuntimeError):
    """Raised when a repository-owned import closure is not deterministic."""


@dataclass(frozen=True)
class _ModuleRecord:
    name: str
    path: Path
    relative_path: str
    is_package: bool

    @property
    def package(self) -> str:
        if self.is_package:
            return self.name
        return self.name.rpartition(".")[0]


@dataclass(frozen=True)
class _DynamicCall:
    callsite: str
    function: str
    module_literal: str | None = None
    module_package: str | None = None
    file_literal: str | None = None
    unresolved_reason: str | None = None


@dataclass(frozen=True)
class _ScanResult:
    module_names: tuple[str, ...]
    required_module_names: tuple[str, ...]
    dynamic_calls: tuple[_DynamicCall, ...]


_DYNAMIC_MODULE_FUNCTIONS = {
    "__import__",
    "importlib.import_module",
    "runpy.run_module",
}
_DYNAMIC_FILE_FUNCTIONS = {
    "importlib.util.spec_from_file_location",
    "importlib.machinery.SourceFileLoader",
    "runpy.run_path",
}
_DYNAMIC_DISCOVERY_FUNCTIONS = {
    "pkgutil.iter_modules",
    "pkgutil.walk_packages",
    "importlib.metadata.entry_points",
    "pkg_resources.iter_entry_points",
    "exec",
    "eval",
    "compile",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _repository_relative(repository_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repository_root).as_posix()
    except ValueError as exc:
        raise ImportClosureError(
            f"source escapes repository root: {path}"
        ) from exc


def _module_name(source_root: Path, source: Path) -> tuple[str, bool]:
    relative = source.relative_to(source_root)
    parts = list(relative.with_suffix("").parts)
    is_package = bool(parts and parts[-1] == "__init__")
    if is_package:
        parts.pop()
    if not parts:
        raise ImportClosureError(
            f"source root itself cannot be a package initializer: {source}"
        )
    if any(not part.isidentifier() for part in parts):
        raise ImportClosureError(f"invalid Python module path: {source}")
    return ".".join(parts), is_package


def _build_module_index(
    repository_root: Path,
    source_roots: Sequence[str],
) -> tuple[dict[str, _ModuleRecord], dict[str, _ModuleRecord]]:
    by_name: dict[str, _ModuleRecord] = {}
    by_path: dict[str, _ModuleRecord] = {}
    for root_text in source_roots:
        source_root = (repository_root / root_text).resolve()
        try:
            source_root.relative_to(repository_root)
        except ValueError as exc:
            raise ImportClosureError(
                f"source root escapes repository: {root_text}"
            ) from exc
        if not source_root.is_dir():
            raise ImportClosureError(f"source root does not exist: {root_text}")
        for source in sorted(source_root.rglob("*.py")):
            if "__pycache__" in source.parts:
                continue
            if source.is_symlink() and not source.resolve().is_relative_to(repository_root):
                raise ImportClosureError(f"source symlink escapes repository: {source}")
            name, is_package = _module_name(source_root, source)
            relative_path = _repository_relative(repository_root, source)
            record = _ModuleRecord(
                name=name,
                path=source.resolve(),
                relative_path=relative_path,
                is_package=is_package,
            )
            previous_name = by_name.get(name)
            if previous_name is not None and previous_name.path != record.path:
                raise ImportClosureError(
                    f"ambiguous local module {name}: "
                    f"{previous_name.relative_path}, {relative_path}"
                )
            previous_path = by_path.get(relative_path)
            if previous_path is not None and previous_path.name != name:
                raise ImportClosureError(
                    f"ambiguous source path {relative_path}: "
                    f"{previous_path.name}, {name}"
                )
            by_name[name] = record
            by_path[relative_path] = record
    return by_name, by_path


def _resolve_relative_name(
    *,
    package: str,
    level: int,
    module: str | None,
) -> str:
    if not package:
        raise ImportClosureError("relative import used outside a package")
    package_parts = package.split(".")
    trim = level - 1
    if trim >= len(package_parts):
        raise ImportClosureError(
            f"relative import escapes package {package}: level={level}"
        )
    anchor = package_parts[: len(package_parts) - trim]
    if module:
        anchor.extend(module.split("."))
    return ".".join(anchor)


def _attribute_name(node: ast.AST, aliases: Mapping[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _attribute_name(node.value, aliases)
        return f"{base}.{node.attr}" if base else None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        base = _attribute_name(node.args[0], aliases)
        return f"{base}.{node.args[1].value}" if base else None
    return None


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                aliases[bound] = alias.name if alias.asname else bound
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name == "*":
                    continue
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    # Resolve straightforward aliases such as ``loader = importlib.import_module``.
    # Iterate because one alias can refer to another alias declared just above it.
    for _ in range(4):
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None:
                continue
            qualified = _attribute_name(value, aliases)
            if not qualified:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and aliases.get(target.id) != qualified:
                    aliases[target.id] = qualified
                    changed = True
        if not changed:
            break
    return aliases


def _scan_source(record: _ModuleRecord) -> _ScanResult:
    try:
        tree = ast.parse(record.path.read_text(encoding="utf-8"), filename=record.relative_path)
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ImportClosureError(f"cannot parse {record.relative_path}: {exc}") from exc

    aliases = _import_aliases(tree)
    imports: set[str] = set()
    required_imports: set[str] = set()
    dynamic_calls: list[_DynamicCall] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {alias.name for alias in node.names}
            imports.update(names)
            required_imports.update(names)
            continue
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = _resolve_relative_name(
                    package=record.package,
                    level=node.level,
                    module=node.module,
                )
            else:
                base = node.module or ""
            if base:
                imports.add(base)
                required_imports.add(base)
            for alias in node.names:
                if alias.name != "*" and base:
                    imports.add(f"{base}.{alias.name}")
            continue
        if not isinstance(node, ast.Call):
            continue
        function = _attribute_name(node.func, aliases)
        if function and function.endswith((".exec_module", ".load_module")):
            dynamic_calls.append(
                _DynamicCall(
                    callsite=(
                        f"{record.relative_path}:{node.lineno}:{node.col_offset}:"
                        f"{function}"
                    ),
                    function=function,
                    unresolved_reason="dynamic loader execution cannot be inferred statically",
                )
            )
            continue
        if function not in (
            _DYNAMIC_MODULE_FUNCTIONS
            | _DYNAMIC_FILE_FUNCTIONS
            | _DYNAMIC_DISCOVERY_FUNCTIONS
        ):
            continue
        callsite = f"{record.relative_path}:{node.lineno}:{node.col_offset}:{function}"
        if function in _DYNAMIC_DISCOVERY_FUNCTIONS:
            dynamic_calls.append(
                _DynamicCall(
                    callsite=callsite,
                    function=function,
                    unresolved_reason="runtime module discovery cannot be inferred statically",
                )
            )
            continue
        target_index = 1 if function in {
            "importlib.util.spec_from_file_location",
            "importlib.machinery.SourceFileLoader",
        } else 0
        target_node = node.args[target_index] if len(node.args) > target_index else None
        target_literal = _literal_string(target_node)
        if function in _DYNAMIC_FILE_FUNCTIONS:
            dynamic_calls.append(
                _DynamicCall(
                    callsite=callsite,
                    function=function,
                    file_literal=target_literal,
                    unresolved_reason=(
                        None if target_literal is not None else "dynamic file path is not literal"
                    ),
                )
            )
            continue

        package_literal: str | None = None
        if function == "importlib.import_module" and len(node.args) >= 2:
            package_literal = _literal_string(node.args[1])
            if (
                package_literal is None
                and isinstance(node.args[1], ast.Name)
                and node.args[1].id == "__package__"
            ):
                package_literal = record.package
        dynamic_calls.append(
            _DynamicCall(
                callsite=callsite,
                function=function,
                module_literal=target_literal,
                module_package=package_literal,
                unresolved_reason=(
                    None if target_literal is not None else "dynamic module name is not literal"
                ),
            )
        )
    return _ScanResult(
        module_names=tuple(sorted(imports)),
        required_module_names=tuple(sorted(required_imports)),
        dynamic_calls=tuple(sorted(dynamic_calls, key=lambda item: item.callsite)),
    )


def _package_parents(
    record: _ModuleRecord,
    modules: Mapping[str, _ModuleRecord],
) -> tuple[_ModuleRecord, ...]:
    parts = record.name.split(".")
    parents: list[_ModuleRecord] = []
    for index in range(1, len(parts)):
        candidate = modules.get(".".join(parts[:index]))
        if candidate is not None and candidate.is_package:
            parents.append(candidate)
    return tuple(parents)


def _resolve_dynamic_file(
    literal: str,
    *,
    importer: _ModuleRecord,
    repository_root: Path,
    modules_by_path: Mapping[str, _ModuleRecord],
) -> _ModuleRecord | None:
    value = Path(literal)
    candidates = [value] if value.is_absolute() else [
        repository_root / value,
        importer.path.parent / value,
    ]
    matches: dict[str, _ModuleRecord] = {}
    for candidate in candidates:
        try:
            relative = candidate.resolve().relative_to(repository_root).as_posix()
        except ValueError:
            continue
        record = modules_by_path.get(relative)
        if record is not None:
            matches[record.relative_path] = record
    if len(matches) > 1:
        raise ImportClosureError(
            f"ambiguous dynamic file target {literal!r} in {importer.relative_path}: "
            f"{', '.join(sorted(matches))}"
        )
    return next(iter(matches.values()), None)


def _normalize_declarations(
    declarations: Mapping[str, Sequence[str]] | None,
) -> dict[str, tuple[str, ...]]:
    normalized: dict[str, tuple[str, ...]] = {}
    for callsite, targets in (declarations or {}).items():
        values = tuple(sorted({str(item) for item in targets}))
        if not values:
            raise ImportClosureError(
                f"dynamic declaration must name a target or {EXTERNAL_DYNAMIC_TARGET}: {callsite}"
            )
        normalized[str(callsite)] = values
    return dict(sorted(normalized.items()))


def collect_repository_import_closure(
    *,
    repository_root: Path,
    entrypoint_modules: Sequence[str],
    source_roots: Sequence[str] = ("src", "scripts"),
    dynamic_import_declarations: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Collect and hash all repository Python modules reachable from entries.

    Declaration keys are exact call-site IDs emitted in errors and snapshots,
    for example ``src/pkg/plugin.py:12:4:importlib.import_module``.  Declaration
    values are local module names, ``path:relative/source.py``, or ``@external``
    when a loader is intentionally provided by the Python environment.
    """

    root = repository_root.resolve()
    normalized_roots = tuple(dict.fromkeys(str(item) for item in source_roots))
    if not normalized_roots:
        raise ImportClosureError("at least one source root is required")
    modules, modules_by_path = _build_module_index(root, normalized_roots)
    declarations = _normalize_declarations(dynamic_import_declarations)
    entry_names = tuple(dict.fromkeys(str(item) for item in entrypoint_modules))
    if not entry_names:
        raise ImportClosureError("at least one entrypoint module is required")
    missing_entries = [name for name in entry_names if name not in modules]
    if missing_entries:
        raise ImportClosureError(
            f"entrypoint module not found: {', '.join(missing_entries)}"
        )

    pending = list(entry_names)
    reached: dict[str, _ModuleRecord] = {}
    observed_dynamic: list[dict[str, Any]] = []
    used_declarations: set[str] = set()

    def enqueue(record: _ModuleRecord) -> None:
        if record.name not in reached and record.name not in pending:
            pending.append(record.name)
        for parent in _package_parents(record, modules):
            if parent.name not in reached and parent.name not in pending:
                pending.append(parent.name)

    def declared_targets(call: _DynamicCall) -> list[_ModuleRecord]:
        values = declarations.get(call.callsite)
        if values is None:
            return []
        used_declarations.add(call.callsite)
        found: list[_ModuleRecord] = []
        for target in values:
            if target == EXTERNAL_DYNAMIC_TARGET:
                continue
            if target.startswith("path:"):
                relative = Path(target[5:]).as_posix()
                record = modules_by_path.get(relative)
            else:
                record = modules.get(target)
            if record is None:
                raise ImportClosureError(
                    f"dynamic declaration target is not repository-owned: "
                    f"{call.callsite} -> {target}"
                )
            found.append(record)
        return found

    while pending:
        module_name = pending.pop(0)
        if module_name in reached:
            continue
        record = modules[module_name]
        reached[module_name] = record
        for parent in _package_parents(record, modules):
            enqueue(parent)
        scan = _scan_source(record)
        for imported_name in scan.module_names:
            imported = modules.get(imported_name)
            if imported is not None:
                enqueue(imported)
                continue
            # ``from package import value`` also produces package.value.  The
            # value may be an attribute, so an unresolved child is safe only
            # when its repository-owned base package was resolved.
            base_name = imported_name.rpartition(".")[0]
            if (
                imported_name not in scan.required_module_names
                and base_name
                and base_name in modules
            ):
                continue
            top_level = imported_name.split(".", 1)[0]
            top_level_record = modules.get(top_level)
            if (
                imported_name in scan.required_module_names
                and top_level_record is not None
                and top_level_record.is_package
            ):
                raise ImportClosureError(
                    f"unresolved repository-owned import in "
                    f"{record.relative_path}: {imported_name}"
                )

        for call in scan.dynamic_calls:
            inferred: _ModuleRecord | None = None
            resolution = ""
            if call.module_literal is not None:
                dynamic_name = call.module_literal
                if dynamic_name.startswith("."):
                    if not call.module_package:
                        call = _DynamicCall(
                            **{
                                **call.__dict__,
                                "unresolved_reason": "relative dynamic import has no literal package",
                            }
                        )
                    else:
                        level = len(dynamic_name) - len(dynamic_name.lstrip("."))
                        dynamic_name = _resolve_relative_name(
                            package=call.module_package,
                            level=level,
                            module=dynamic_name[level:] or None,
                        )
                inferred = modules.get(dynamic_name)
                resolution = (
                    f"local_module:{inferred.name}" if inferred else "external_literal_module"
                )
            elif call.file_literal is not None:
                inferred = _resolve_dynamic_file(
                    call.file_literal,
                    importer=record,
                    repository_root=root,
                    modules_by_path=modules_by_path,
                )
                resolution = (
                    f"local_file:{inferred.relative_path}" if inferred else "unresolved_literal_file"
                )

            explicit = declared_targets(call)
            has_external_declaration = EXTERNAL_DYNAMIC_TARGET in declarations.get(
                call.callsite, ()
            )
            if inferred is not None:
                enqueue(inferred)
            for target in explicit:
                enqueue(target)
            unresolved = call.unresolved_reason is not None or resolution == "unresolved_literal_file"
            if unresolved and not explicit and not has_external_declaration:
                raise ImportClosureError(
                    f"unresolved dynamic import at {call.callsite}: "
                    f"{call.unresolved_reason or resolution}; add an explicit declaration"
                )
            observed_dynamic.append(
                {
                    "callsite": call.callsite,
                    "function": call.function,
                    "resolution": resolution or "explicit_declaration",
                    "declared_targets": list(declarations.get(call.callsite, ())),
                }
            )

    unused = sorted(set(declarations) - used_declarations)
    if unused:
        raise ImportClosureError(
            f"stale dynamic import declarations: {', '.join(unused)}"
        )

    files = {
        record.relative_path: sha256_file(record.path)
        for record in sorted(reached.values(), key=lambda value: value.relative_path)
    }
    entrypoints = [
        {"module": name, "path": modules[name].relative_path}
        for name in entry_names
    ]
    bundle_sha256 = _sha256_json(files)
    snapshot: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_roots": list(normalized_roots),
        "entrypoints": entrypoints,
        "dynamic_import_declarations": {
            key: list(value) for key, value in declarations.items()
        },
        "dynamic_imports_observed": sorted(
            observed_dynamic, key=lambda item: item["callsite"]
        ),
        "files": files,
        "file_count": len(files),
        "bundle_sha256": bundle_sha256,
    }
    snapshot["closure_sha256"] = _sha256_json(snapshot)
    return snapshot


def validate_repository_import_closure(
    expected: Mapping[str, Any],
    *,
    repository_root: Path,
    entrypoint_modules: Sequence[str],
    source_roots: Sequence[str] = ("src", "scripts"),
    dynamic_import_declarations: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Recompute a closure and reject any source or topology drift."""

    actual = collect_repository_import_closure(
        repository_root=repository_root,
        entrypoint_modules=entrypoint_modules,
        source_roots=source_roots,
        dynamic_import_declarations=dynamic_import_declarations,
    )
    if dict(expected) != actual:
        expected_files = dict(expected.get("files") or {})
        actual_files = actual["files"]
        missing = sorted(set(expected_files) - set(actual_files))
        added = sorted(set(actual_files) - set(expected_files))
        changed = sorted(
            path
            for path in set(expected_files) & set(actual_files)
            if expected_files[path] != actual_files[path]
        )
        raise ImportClosureError(
            "repository import closure changed"
            f"; missing={missing}; added={added}; changed={changed}"
        )
    return actual
