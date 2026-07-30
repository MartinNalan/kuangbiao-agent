"""Private, evidence-preserving corpus preparation for the v4 knowledge base.

This module deliberately stops before FTS, embeddings, graph construction, or
LLM usage.  It turns the currently governed source corpus into an auditable
clean-text and clause-level corpus so later retrieval experiments all start
from the same input.

All generated content lives in ``data/knowledge_base_v4`` which is ignored by
Git.  The tracked code and documentation contain schemas and rules only.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from mining_qa.kb_build_utils import infer_clause_no, is_clause_heading, is_section_heading, stable_id


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ORE_ROOT = Path("/home/nalanmading/My-project/ore_expert")
DEFAULT_REBUILD_ROOT = PROJECT_ROOT / "data" / "knowledge_base_v4"
DEFAULT_LEGACY_DB = PROJECT_ROOT / "data" / "knowledge_base" / "db" / "knowledge_base.sqlite"
STANDARD_SOURCE_DIRS = (
    ("compilation", ORE_ROOT / "knowledge_governance" / "compilation_standards" / "json"),
    ("supplement", ORE_ROOT / "knowledge_governance" / "supplement_ingest" / "processed_documents" / "json"),
)


@dataclass(frozen=True)
class CorpusPaths:
    root: Path

    @property
    def db_path(self) -> Path:
        return self.root / "db" / "corpus.sqlite"

    @property
    def manifest_dir(self) -> Path:
        return self.root / "manifests"

    @property
    def report_dir(self) -> Path:
        return self.root / "reports"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path, *, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def clean_display_text(raw_text: str) -> tuple[str, list[str]]:
    """Clean presentation whitespace without changing factual wording or units."""
    original = raw_text or ""
    text = unicodedata.normalize("NFKC", original).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u3000", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    changes: list[str] = []
    if text != original:
        changes.append("unicode_and_whitespace_normalized")
    return text, changes


def remove_page_artifacts(clean_text: str) -> tuple[str, list[str]]:
    """Remove only isolated OCR page markers; retain all semantic content."""
    lines = clean_text.splitlines()
    kept: list[str] = []
    removed: list[str] = []
    for index, line in enumerate(lines):
        compact = line.strip()
        edge_line = index < 2 or index >= len(lines) - 2
        is_numeric_marker = bool(re.fullmatch(r"[-—–― ]*\d{1,4}[-—–― ]*", compact))
        is_ocr_marker = compact in {"一", "二", "三", "四", "五", "六", "七", "八", "九", "十"}
        if edge_line and (is_numeric_marker or is_ocr_marker):
            removed.append(compact)
            continue
        kept.append(line)
    result = "\n".join(kept).strip()
    return result, removed


@dataclass(frozen=True)
class UnitDefinition:
    key: str
    canonical_label: str
    canonical_symbol: str
    pattern: str


# Patterns are ordered from compound units to simple units.  Only an explicit
# number-plus-unit expression is normalized, so a bare "m" in an identifier or
# a unit such as "mg" cannot be mistaken for metres.
UNIT_DEFINITIONS = (
    UnitDefinition("volume_cubic_metre", "立方米", "m3", r"(?:m[³3]|立方米)"),
    UnitDefinition("area_square_metre", "平方米", "m2", r"(?:m[²2]|平方米|平米)"),
    UnitDefinition("length_kilometre", "千米", "km", r"(?:km|千米|公里)"),
    UnitDefinition("length_metre", "米", "m", r"(?:m|米|公尺)"),
    UnitDefinition("length_centimetre", "厘米", "cm", r"(?:cm|厘米|公分)"),
    UnitDefinition("length_millimetre", "毫米", "mm", r"(?:mm|毫米)"),
    UnitDefinition("mass_tonne", "吨", "t", r"(?:t|吨)"),
    UnitDefinition("time_day", "日", "d", r"(?:d|日|天)"),
    UnitDefinition("time_year", "年", "a", r"(?:a|年)"),
)
NUMBER_PATTERN = r"(?P<value>[+-]?\d+(?:[.．]\d+)?)"


def extract_measurements(text: str) -> list[dict[str, Any]]:
    measurements: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    for definition in UNIT_DEFINITIONS:
        pattern = re.compile(
            rf"{NUMBER_PATTERN}\s*(?P<unit>{definition.pattern})(?![A-Za-z0-9²³])",
            flags=re.IGNORECASE,
        )
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < used_end and end > used_start for used_start, used_end in occupied):
                continue
            raw_value = match.group("value")
            numeric_value = raw_value.replace("．", ".")
            measurements.append(
                {
                    "char_start": start,
                    "char_end": end,
                    "raw_text": match.group(0),
                    "raw_value": raw_value,
                    "numeric_value": numeric_value,
                    "raw_unit": match.group("unit"),
                    "unit_key": definition.key,
                    "canonical_label": definition.canonical_label,
                    "canonical_symbol": definition.canonical_symbol,
                    "canonical_text": f"{numeric_value}{definition.canonical_label}",
                }
            )
            occupied.append((start, end))
    return sorted(measurements, key=lambda item: (item["char_start"], item["char_end"]))


def normalized_search_text(clean_text: str, measurements: list[dict[str, Any]]) -> str:
    """Return a deterministic query/index helper field, never a citation field."""
    result = clean_text
    for item in reversed(measurements):
        result = (
            result[: item["char_start"]]
            + item["canonical_text"]
            + result[item["char_end"] :]
        )
    return result


def unit_query_aliases(query: str) -> list[str]:
    """Produce bounded, equivalent unit spellings for future query rewriting."""
    normalized, _ = clean_display_text(query)
    measurements = extract_measurements(normalized)
    aliases = [normalized]
    canonical = normalized_search_text(normalized, measurements)
    if canonical != normalized:
        aliases.append(canonical)
    return aliases


def document_type_for_standard(standard_no: str | None, title: str) -> str:
    code = (standard_no or "").upper()
    if code.startswith("GB"):
        return "national_standard"
    if code.startswith(("DZ", "MT", "EJ", "SY", "YS")):
        return "industry_standard"
    if "修改单" in title:
        return "amendment"
    if "指南" in title or "问" in title:
        return "guidance"
    return "technical_document"


def corpus_for_document(document_type: str) -> str:
    if document_type in {"national_standard", "industry_standard", "standard", "technical_document", "amendment", "guidance"}:
        return "technical_standards"
    return "administrative_services"


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            pragma foreign_keys = on;

            create table if not exists build_runs (
              run_id text primary key,
              started_at text not null,
              finished_at text,
              status text not null,
              config_json text not null,
              summary_json text
            );

            create table if not exists documents (
              document_id text primary key,
              corpus text not null check (corpus in ('technical_standards', 'administrative_services')),
              document_type text not null,
              title text not null,
              standard_no text,
              status text not null,
              effective_status text not null check (effective_status in ('current', 'repealed', 'unverified', 'governance_conflict')),
              review_status text not null,
              can_answer integer not null check (can_answer in (0, 1)),
              source_url text,
              source_platform text,
              source_metadata_json text not null,
              page_count integer not null default 0,
              unit_count integer not null default 0,
              created_at text not null,
              updated_at text not null
            );

            create table if not exists document_versions (
              version_id text primary key,
              document_id text not null references documents(document_id) on delete cascade,
              version_label text not null,
              source_text_sha256 text,
              schema_version text,
              is_active integer not null check (is_active in (0, 1)),
              provenance_json text not null,
              created_at text not null
            );

            create table if not exists source_artifacts (
              artifact_id text primary key,
              path text not null unique,
              artifact_type text not null,
              sha256 text,
              bytes integer,
              source_url text,
              exists_on_disk integer not null check (exists_on_disk in (0, 1)),
              recorded_at text not null
            );

            create table if not exists document_artifacts (
              document_id text not null references documents(document_id) on delete cascade,
              artifact_id text not null references source_artifacts(artifact_id) on delete cascade,
              artifact_role text not null,
              primary key (document_id, artifact_id, artifact_role)
            );

            create table if not exists pages (
              page_id text primary key,
              document_id text not null references documents(document_id) on delete cascade,
              page_no integer,
              source_page_ref text,
              raw_text text not null,
              clean_text text not null,
              clean_text_sha256 text not null,
              quality_json text not null,
              cleanup_json text not null
            );

            create table if not exists content_units (
              unit_id text primary key,
              document_id text not null references documents(document_id) on delete cascade,
              parent_unit_id text references content_units(unit_id) on delete set null,
              unit_order integer not null,
              unit_type text not null,
              section_path text,
              clause_no text,
              page_start integer,
              page_end integer,
              raw_text text not null,
              clean_text text not null,
              normalized_search_text text not null,
              structure_json text not null,
              source_ref text,
              validation_status text not null,
              created_at text not null
            );

            create table if not exists unit_measurements (
              measurement_id text primary key,
              unit_id text not null references content_units(unit_id) on delete cascade,
              char_start integer not null,
              char_end integer not null,
              raw_text text not null,
              raw_value text not null,
              numeric_value text not null,
              raw_unit text not null,
              unit_key text not null,
              canonical_label text not null,
              canonical_symbol text not null,
              canonical_text text not null
            );

            create table if not exists quality_findings (
              finding_id text primary key,
              document_id text not null references documents(document_id) on delete cascade,
              unit_id text references content_units(unit_id) on delete cascade,
              severity text not null check (severity in ('info', 'warning', 'error')),
              finding_type text not null,
              message text not null,
              evidence_json text not null,
              created_at text not null
            );

            create index if not exists idx_v4_documents_corpus on documents(corpus, document_type, effective_status);
            create index if not exists idx_v4_pages_document on pages(document_id, page_no);
            create index if not exists idx_v4_units_document_order on content_units(document_id, unit_order);
            create index if not exists idx_v4_units_clause on content_units(clause_no);
            create index if not exists idx_v4_measurements_unit on unit_measurements(unit_key, canonical_text);
            create index if not exists idx_v4_findings_document on quality_findings(document_id, severity);
            """
        )


def reset_db(db_path: Path) -> None:
    if db_path.exists():
        db_path.unlink()
    init_db(db_path)


def normalize_effective_status(value: str | None) -> str:
    value = (value or "").strip().lower()
    if value in {"current", "active", "现行", "现行有效", "有效", "current_replacement"}:
        return "current"
    if value in {"repealed", "deprecated", "replaced", "superseded", "废止", "废止/失效", "失效"}:
        return "repealed"
    if value == "governance_conflict":
        return "governance_conflict"
    return "unverified"


def can_answer_from_governance(effective_status: str, review_status: str = "approved_for_service") -> int:
    return int(effective_status == "current" and review_status == "approved_for_service")


def path_from_project(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def parse_governed_service_guide(path: Path) -> dict[str, Any]:
    """Load the existing deterministic guide parser without importing a runtime service."""
    parser_path = PROJECT_ROOT / "scripts" / "ingest_mnr_service_guides.py"
    spec = importlib.util.spec_from_file_location("geowiki_service_guide_parser", parser_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load service-guide parser: {parser_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.parse_guide(path)


def text_from_table(table: dict[str, Any]) -> str:
    caption = str(table.get("caption") or "表格")
    matrix = table.get("matrix") or []
    rows = ["\t".join(str(cell).strip() for cell in row) for row in matrix if row]
    return "\n".join([caption, *rows]).strip()


def append_artifact(
    conn: sqlite3.Connection,
    document_id: str,
    path: Path | None,
    artifact_role: str,
    artifact_type: str,
    source_url: str | None,
    now: str,
    fingerprint_cache: dict[str, tuple[str | None, int | None, bool]],
) -> None:
    if path is None:
        return
    resolved = path.resolve(strict=False)
    key = str(resolved)
    if key not in fingerprint_cache:
        exists = resolved.is_file()
        fingerprint_cache[key] = (
            sha256_file(resolved) if exists else None,
            resolved.stat().st_size if exists else None,
            exists,
        )
    digest, byte_count, exists = fingerprint_cache[key]
    artifact_id = stable_id(key, prefix="artifact")
    conn.execute(
        """
        insert into source_artifacts(artifact_id,path,artifact_type,sha256,bytes,source_url,exists_on_disk,recorded_at)
        values (?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(path) do update set sha256=excluded.sha256, bytes=excluded.bytes,
          source_url=coalesce(excluded.source_url, source_artifacts.source_url),
          exists_on_disk=excluded.exists_on_disk, recorded_at=excluded.recorded_at
        """,
        (artifact_id, key, artifact_type, digest, byte_count, source_url, int(exists), now),
    )
    conn.execute(
        "insert or ignore into document_artifacts(document_id,artifact_id,artifact_role) values (?, ?, ?)",
        (document_id, artifact_id, artifact_role),
    )


def make_unit(
    *,
    document_id: str,
    order: int,
    unit_type: str,
    raw_text: str,
    section_path: str | None,
    clause_no: str | None,
    page_start: int | None,
    page_end: int | None,
    source_ref: str | None,
    validation_status: str,
    structure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_text, changes = clean_display_text(raw_text)
    measurements = extract_measurements(clean_text)
    return {
        "unit_id": stable_id(document_id, order, unit_type, raw_text, prefix="unit"),
        "document_id": document_id,
        "unit_order": order,
        "unit_type": unit_type,
        "section_path": section_path,
        "clause_no": clause_no,
        "page_start": page_start,
        "page_end": page_end,
        "raw_text": raw_text,
        "clean_text": clean_text,
        "normalized_search_text": normalized_search_text(clean_text, measurements),
        "structure": {"cleanup": changes, **(structure or {})},
        "source_ref": source_ref,
        "validation_status": validation_status,
        "measurements": measurements,
    }


def structural_units_from_pages(
    document_id: str,
    pages: list[dict[str, Any]],
    validation_status: str,
) -> list[dict[str, Any]]:
    """Derive conservative section/clause units without an arbitrary token size."""
    units: list[dict[str, Any]] = []
    current: list[str] = []
    current_clause: str | None = None
    current_section: str | None = None
    active_section: str | None = None
    start_page: int | None = None
    end_page: int | None = None
    source_refs: list[str] = []

    def unit_kind(line: str, clause_no: str | None) -> str:
        if is_section_heading(line):
            return "section"
        if clause_no:
            return "clause"
        return "front_matter"

    def flush() -> None:
        nonlocal current, current_clause, current_section, start_page, end_page, source_refs
        if not current:
            return
        text = "\n".join(current).strip()
        if text:
            unit = make_unit(
                document_id=document_id,
                order=len(units) + 1,
                unit_type=unit_kind(current[0], current_clause),
                raw_text=text,
                section_path=current_section,
                clause_no=current_clause,
                page_start=start_page,
                page_end=end_page,
                source_ref=source_refs[0] if len(source_refs) == 1 else None,
                validation_status=validation_status,
                structure={"source_page_refs": source_refs},
            )
            units.append(unit)
        current = []
        current_clause = None
        current_section = None
        start_page = None
        end_page = None
        source_refs = []

    for page in pages:
        page_no = page.get("page_no")
        page_text = page.get("clean_text") or ""
        source_ref = page.get("source_ref")
        for line in (line.strip() for line in page_text.splitlines() if line.strip()):
            clause_no = infer_clause_no(line)
            starts_unit = bool(is_section_heading(line) or is_clause_heading(line))
            if current and starts_unit:
                flush()
            if not current:
                current_clause = clause_no
                if is_section_heading(line):
                    active_section = line[:160]
                    current_section = active_section
                else:
                    current_section = active_section
                start_page = page_no
            current.append(line)
            end_page = page_no
            if source_ref and source_ref not in source_refs:
                source_refs.append(source_ref)
    flush()
    return units


def iter_standard_inputs() -> Iterator[dict[str, Any]]:
    for collection, source_dir in STANDARD_SOURCE_DIRS:
        for path in sorted(source_dir.glob("*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            bib = doc.get("bibliographic") or {}
            title = str(bib.get("title") or doc.get("title") or path.stem)
            standard_no = bib.get("standard_code") or bib.get("standard_no")
            document_id = str(doc.get("document_id") or stable_id(path, title, prefix="document"))
            pages: list[dict[str, Any]] = []
            for index, page in enumerate(doc.get("pages") or [], 1):
                raw = str(page.get("ocr_text") or "")
                clean, cleanup = clean_display_text(raw)
                cleaned, removed = remove_page_artifacts(clean)
                page_no = page.get("standard_page") or page.get("page") or index
                try:
                    page_no = int(page_no)
                except (TypeError, ValueError):
                    page_no = index
                pages.append(
                    {
                        "page_no": page_no,
                        "raw_text": raw,
                        "clean_text": cleaned,
                        "source_ref": page.get("source_page_json"),
                        "quality": page.get("quality") or {},
                        "cleanup": {"changes": cleanup, "removed_page_artifacts": removed},
                    }
                )
            source_pdf = (doc.get("source_trace") or {}).get("source_pdf")
            pdf_path = ORE_ROOT / "standard_specification" / source_pdf if source_pdf else None
            effective_status = normalize_effective_status(bib.get("status") or doc.get("status"))
            if collection == "compilation" and effective_status == "unverified":
                # This remains a corpus state only.  It is not a permission to answer.
                effective_status = "unverified"
            yield {
                "document_id": document_id,
                "corpus": "technical_standards",
                "document_type": document_type_for_standard(standard_no, title),
                "title": title,
                "standard_no": standard_no,
                "status": str(bib.get("status") or doc.get("status") or "unknown"),
                "effective_status": effective_status,
                "review_status": "approved_for_service" if collection == "supplement" else "pending_status_verification",
                "source_url": None,
                "source_platform": "矿产资源技术标准汇编治理成果",
                "source_metadata": {
                    "collection": collection,
                    "bibliographic": bib,
                    "authority": doc.get("authority") or {},
                    "source_trace": doc.get("source_trace") or {},
                    "quality": doc.get("quality") or {},
                },
                "schema_version": str(doc.get("schema_version") or "unknown"),
                "source_text_sha256": (doc.get("text_access") or {}).get("ocr_text_sha256"),
                "pages": pages,
                "artifacts": [
                    (path, "governed_document_json", "governed_source_json", None),
                    (pdf_path, "source_pdf", "original_pdf", None),
                ],
                "tables": doc.get("manual_table_corrections") or [],
            }


def legacy_document_rows(legacy_db: Path) -> Iterator[sqlite3.Row]:
    conn = sqlite3.connect(legacy_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            select document_id,title,standard_no,document_type,status,effective_status,review_status,
                   official_url,source_platform,source_trace_json,bibliographic_json,quality_json,
                   validation_status
            from documents
            where source_type = 'official_fulltext'
            order by document_type, document_id
            """
        ).fetchall()
        yield from rows
    finally:
        conn.close()


def legacy_chunks(legacy_db: Path, document_id: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(legacy_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            select rowid,chunk_type,section_path,clause_no,page_start,page_end,text,table_json,
                   source_ref,validation_status
            from chunks where document_id = ? order by rowid
            """,
            (document_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def iter_legacy_official_inputs(legacy_db: Path) -> Iterator[dict[str, Any]]:
    for row in legacy_document_rows(legacy_db):
        trace = json.loads(row["source_trace_json"] or "{}")
        bib = json.loads(row["bibliographic_json"] or "{}")
        quality = json.loads(row["quality_json"] or "{}")
        document_id = str(row["document_id"])
        doc_type = str(row["document_type"])
        raw_path = path_from_project(trace.get("raw_markdown") or trace.get("raw_html") or trace.get("raw_doc"))
        artifacts: list[tuple[Path | None, str, str, str | None]] = [
            (raw_path, "official_source", "raw_source", row["official_url"]),
        ]
        converted_docx = path_from_project(trace.get("converted_docx"))
        if converted_docx:
            artifacts.append((converted_docx, "converted_office_document", "processed_source", row["official_url"]))

        legacy_units = legacy_chunks(legacy_db, document_id)
        pages: list[dict[str, Any]] = []
        tables: list[dict[str, Any]] = []
        if doc_type == "service_guide" and raw_path and raw_path.exists():
            # The Markdown source is already a governed snapshot with stable section order.
            guide = parse_governed_service_guide(raw_path)
            for section in guide["sections"]:
                raw = str(section["text"])
                pages.append(
                    {
                        "page_no": None,
                        "raw_text": raw,
                        "clean_text": clean_display_text(raw)[0],
                        "source_ref": str(raw_path),
                        "quality": {"empty": bool(section.get("empty"))},
                        "cleanup": {"changes": [], "removed_page_artifacts": []},
                        "section_name": section["name"],
                        "unit_type": "service_section",
                    }
                )
            tables = [guide["table"]] if guide.get("table") else []
        else:
            # Policy HTML and source attachments are preserved as legacy governed units
            # until their dedicated parsers are separately benchmarked.
            for index, item in enumerate(legacy_units, 1):
                raw = str(item["text"] or "")
                pages.append(
                    {
                        "page_no": item["page_start"],
                        "raw_text": raw,
                        "clean_text": clean_display_text(raw)[0],
                        "source_ref": item["source_ref"],
                        "quality": {},
                        "cleanup": {"changes": [], "removed_page_artifacts": []},
                        "section_name": item["section_path"],
                        "clause_no": item["clause_no"],
                        "unit_type": item["chunk_type"] or "legacy_governed_unit",
                        "legacy_order": index,
                        "table_json": item["table_json"],
                        "validation_status": item["validation_status"] or row["validation_status"],
                    }
                )
        yield {
            "document_id": document_id,
            "corpus": "administrative_services",
            "document_type": doc_type,
            "title": str(row["title"]),
            "standard_no": row["standard_no"],
            "status": str(row["status"] or "unknown"),
            "effective_status": normalize_effective_status(row["effective_status"]),
            "review_status": str(row["review_status"] or "pending_review"),
            "source_url": row["official_url"],
            "source_platform": row["source_platform"],
            "source_metadata": {"source_trace": trace, "bibliographic": bib, "quality": quality},
            "schema_version": "legacy_governed_source.v1",
            "source_text_sha256": quality.get("source_sha256") or bib.get("normalized_content_sha256"),
            "pages": pages,
            "artifacts": artifacts,
            "tables": tables,
        }


def persist_document(
    conn: sqlite3.Connection,
    source: dict[str, Any],
    now: str,
    fingerprint_cache: dict[str, tuple[str | None, int | None, bool]],
) -> dict[str, int]:
    document_id = source["document_id"]
    effective_status = source["effective_status"]
    review_status = source["review_status"]
    conn.execute("delete from documents where document_id = ?", (document_id,))
    conn.execute(
        """
        insert into documents(
          document_id,corpus,document_type,title,standard_no,status,effective_status,review_status,
          can_answer,source_url,source_platform,source_metadata_json,page_count,unit_count,created_at,updated_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            document_id,
            source["corpus"],
            source["document_type"],
            source["title"],
            source["standard_no"],
            source["status"],
            effective_status,
            review_status,
            can_answer_from_governance(effective_status, review_status),
            source["source_url"],
            source["source_platform"],
            json_dumps(source["source_metadata"]),
            len(source["pages"]),
            now,
            now,
        ),
    )
    version_id = stable_id(document_id, source["source_text_sha256"], prefix="version")
    conn.execute(
        """
        insert into document_versions(version_id,document_id,version_label,source_text_sha256,schema_version,is_active,provenance_json,created_at)
        values (?, ?, 'initial_rebuild', ?, ?, 1, ?, ?)
        """,
        (version_id, document_id, source["source_text_sha256"], source["schema_version"], json_dumps(source["source_metadata"]), now),
    )
    for path, artifact_type, artifact_role, source_url in source["artifacts"]:
        append_artifact(conn, document_id, path, artifact_role, artifact_type, source_url, now, fingerprint_cache)

    unit_count = 0
    measurement_count = 0
    finding_count = 0
    if effective_status == "unverified":
        finding_count += add_finding(
            conn,
            document_id=document_id,
            unit_id=None,
            severity="warning",
            finding_type="effective_status_unverified",
            message="文档缺少可核验的现行状态来源，不能进入正向问答。",
            evidence={"status": source["status"], "review_status": review_status},
            now=now,
        )
    if source["corpus"] == "technical_standards" and not source["standard_no"]:
        finding_count += add_finding(
            conn,
            document_id=document_id,
            unit_id=None,
            severity="warning",
            finding_type="missing_standard_number",
            message="技术标准类文档缺少标准号，需人工核验文献身份和版本关系。",
            evidence={"title": source["title"]},
            now=now,
        )
    pages_for_structure: list[dict[str, Any]] = []
    for index, page in enumerate(source["pages"], 1):
        page_id = stable_id(document_id, "page", index, page.get("page_no"), page["raw_text"], prefix="page")
        conn.execute(
            """
            insert into pages(page_id,document_id,page_no,source_page_ref,raw_text,clean_text,clean_text_sha256,quality_json,cleanup_json)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                page_id,
                document_id,
                page.get("page_no"),
                page.get("source_ref"),
                page["raw_text"],
                page["clean_text"],
                sha256_text(page["clean_text"]),
                json_dumps(page.get("quality") or {}),
                json_dumps(page.get("cleanup") or {}),
            ),
        )
        pages_for_structure.append(page)
        if not page["clean_text"].strip():
            finding_count += add_finding(
                conn,
                document_id=document_id,
                unit_id=None,
                severity="warning",
                finding_type="empty_source_page",
                message="来源页面没有可用文本；原始页面记录已保留，需检查 OCR 或原始文件。",
                evidence={"page_no": page.get("page_no"), "source_ref": page.get("source_ref")},
                now=now,
            )
        page_score = (page.get("quality") or {}).get("avg_score")
        try:
            page_score_value = float(page_score)
        except (TypeError, ValueError):
            page_score_value = None
        if page_score_value is not None and page_score_value < 0.85:
            finding_count += add_finding(
                conn,
                document_id=document_id,
                unit_id=None,
                severity="warning",
                finding_type="low_ocr_confidence",
                message="OCR 置信度低于 0.85；后续引用或入索引前需要抽样复核。",
                evidence={"page_no": page.get("page_no"), "avg_score": page_score_value},
                now=now,
            )
        if page.get("cleanup", {}).get("removed_page_artifacts"):
            finding_count += add_finding(
                conn,
                document_id=document_id,
                unit_id=None,
                severity="info",
                finding_type="removed_page_artifact",
                message="清洗文本移除了孤立页码或 OCR 页眉标记；原文仍保留。",
                evidence={"page_no": page.get("page_no"), "values": page["cleanup"]["removed_page_artifacts"]},
                now=now,
            )

    if source["corpus"] == "technical_standards":
        units = structural_units_from_pages(document_id, pages_for_structure, "parsed_from_ocr")
    else:
        units = []
        for page in pages_for_structure:
            units.append(
                make_unit(
                    document_id=document_id,
                    order=len(units) + 1,
                    unit_type=str(page.get("unit_type") or "official_section"),
                    raw_text=str(page["raw_text"]),
                    section_path=page.get("section_name"),
                    clause_no=page.get("clause_no"),
                    page_start=page.get("page_no"),
                    page_end=page.get("page_no"),
                    source_ref=page.get("source_ref"),
                    validation_status=str(page.get("validation_status") or "governed_source"),
                    structure={"legacy_order": page.get("legacy_order")},
                )
            )

    for table in source["tables"]:
        table_text = text_from_table(table)
        if not table_text:
            continue
        units.append(
            make_unit(
                document_id=document_id,
                order=len(units) + 1,
                unit_type="table",
                raw_text=table_text,
                section_path=str(table.get("caption") or "表格"),
                clause_no=None,
                page_start=None,
                page_end=None,
                source_ref=None,
                validation_status="manually_curated_table",
                structure={"table": table},
            )
        )

    for unit in units:
        conn.execute(
            """
            insert into content_units(
              unit_id,document_id,parent_unit_id,unit_order,unit_type,section_path,clause_no,page_start,page_end,
              raw_text,clean_text,normalized_search_text,structure_json,source_ref,validation_status,created_at
            ) values (?, ?, null, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                unit["unit_id"],
                document_id,
                unit["unit_order"],
                unit["unit_type"],
                unit["section_path"],
                unit["clause_no"],
                unit["page_start"],
                unit["page_end"],
                unit["raw_text"],
                unit["clean_text"],
                unit["normalized_search_text"],
                json_dumps(unit["structure"]),
                unit["source_ref"],
                unit["validation_status"],
                now,
            ),
        )
        unit_count += 1
        if len(unit["clean_text"]) > 6000:
            finding_count += add_finding(
                conn,
                document_id=document_id,
                unit_id=unit["unit_id"],
                severity="warning",
                finding_type="oversized_structural_unit",
                message="结构单元超过 6000 个字符；后续检索阶段需基于该结构单元进行受控子切分。",
                evidence={"characters": len(unit["clean_text"])},
                now=now,
            )
        for measurement in unit["measurements"]:
            measurement_id = stable_id(unit["unit_id"], measurement["char_start"], measurement["raw_text"], prefix="measurement")
            conn.execute(
                """
                insert into unit_measurements(
                  measurement_id,unit_id,char_start,char_end,raw_text,raw_value,numeric_value,raw_unit,
                  unit_key,canonical_label,canonical_symbol,canonical_text
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    measurement_id,
                    unit["unit_id"],
                    measurement["char_start"],
                    measurement["char_end"],
                    measurement["raw_text"],
                    measurement["raw_value"],
                    measurement["numeric_value"],
                    measurement["raw_unit"],
                    measurement["unit_key"],
                    measurement["canonical_label"],
                    measurement["canonical_symbol"],
                    measurement["canonical_text"],
                ),
            )
            measurement_count += 1

    if source["corpus"] == "administrative_services" and source["document_type"] == "policy_attachment":
        finding_count += add_finding(
            conn,
            document_id=document_id,
            unit_id=None,
            severity="warning",
            finding_type="attachment_requires_parser_benchmark",
            message="附件暂以已治理的结构化单元保全；需在独立基准测试后替换为源文件解析器。",
            evidence={"document_type": source["document_type"]},
            now=now,
        )
    conn.execute("update documents set unit_count = ? where document_id = ?", (unit_count, document_id))
    return {"documents": 1, "pages": len(source["pages"]), "units": unit_count, "measurements": measurement_count, "findings": finding_count}


def add_finding(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    unit_id: str | None,
    severity: str,
    finding_type: str,
    message: str,
    evidence: dict[str, Any],
    now: str,
) -> int:
    finding_id = stable_id(document_id, unit_id, severity, finding_type, evidence, prefix="finding")
    conn.execute(
        """
        insert or replace into quality_findings(finding_id,document_id,unit_id,severity,finding_type,message,evidence_json,created_at)
        values (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (finding_id, document_id, unit_id, severity, finding_type, message, json_dumps(evidence), now),
    )
    return 1


def iter_all_inputs(legacy_db: Path) -> Iterable[dict[str, Any]]:
    yield from iter_standard_inputs()
    yield from iter_legacy_official_inputs(legacy_db)


def build_corpus(
    *,
    root: Path = DEFAULT_REBUILD_ROOT,
    legacy_db: Path = DEFAULT_LEGACY_DB,
) -> dict[str, Any]:
    if not legacy_db.exists():
        raise FileNotFoundError(f"Legacy governed database is unavailable: {legacy_db}")
    paths = CorpusPaths(root)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.manifest_dir.mkdir(parents=True, exist_ok=True)
    paths.report_dir.mkdir(parents=True, exist_ok=True)
    reset_db(paths.db_path)
    now = utc_now()
    run_id = stable_id(now, root, prefix="run")
    fingerprint_cache: dict[str, tuple[str | None, int | None, bool]] = {}
    totals: Counter[str] = Counter()
    by_corpus: Counter[str] = Counter()
    by_document_type: Counter[str] = Counter()
    with sqlite3.connect(paths.db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        conn.execute(
            "insert into build_runs(run_id,started_at,status,config_json) values (?, ?, 'running', ?)",
            (run_id, now, json_dumps({"legacy_db": str(legacy_db), "pipeline": "kb_rebuild.v4"})),
        )
        for source in iter_all_inputs(legacy_db):
            result = persist_document(conn, source, now, fingerprint_cache)
            totals.update(result)
            by_corpus[source["corpus"]] += 1
            by_document_type[source["document_type"]] += 1
        integrity = conn.execute("pragma integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"v4 corpus integrity check failed: {integrity}")
        completed_at = utc_now()
        summary = {
            "run_id": run_id,
            "status": "completed",
            "generated_at": completed_at,
            "db_path": str(paths.db_path),
            "integrity_check": integrity,
            "counts": dict(totals),
            "documents_by_corpus": dict(sorted(by_corpus.items())),
            "documents_by_type": dict(sorted(by_document_type.items())),
            "artifact_count": conn.execute("select count(*) from source_artifacts").fetchone()[0],
            "missing_artifact_count": conn.execute("select count(*) from source_artifacts where exists_on_disk = 0").fetchone()[0],
            "governance": [
                dict(row)
                for row in conn.execute(
                    "select corpus,effective_status,review_status,can_answer,count(*) as documents from documents group by 1,2,3,4 order by 1,2,3"
                )
            ],
            "quality_findings": [
                dict(row)
                for row in conn.execute(
                    "select severity,finding_type,count(*) as findings from quality_findings group by 1,2 order by 1,2"
                )
            ],
        }
        conn.execute(
            "update build_runs set finished_at=?, status='completed', summary_json=? where run_id=?",
            (completed_at, json_dumps(summary), run_id),
        )
    (paths.manifest_dir / "corpus_build_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_document_inventory(paths)
    return summary


def write_document_inventory(paths: CorpusPaths) -> None:
    """Write metadata-only inventories; no standard body text leaves the private DB."""
    columns = [
        "document_id",
        "corpus",
        "document_type",
        "title",
        "standard_no",
        "status",
        "effective_status",
        "review_status",
        "can_answer",
        "source_url",
        "source_platform",
        "page_count",
        "unit_count",
    ]
    with sqlite3.connect(paths.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(f"select {','.join(columns)} from documents order by corpus,document_type,title")]
    (paths.manifest_dir / "document_inventory.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (paths.manifest_dir / "document_inventory.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def validate_corpus(root: Path = DEFAULT_REBUILD_ROOT) -> dict[str, Any]:
    paths = CorpusPaths(root)
    if not paths.db_path.exists():
        raise FileNotFoundError(f"Corpus database not found: {paths.db_path}")
    with sqlite3.connect(paths.db_path) as conn:
        conn.row_factory = sqlite3.Row
        integrity = conn.execute("pragma integrity_check").fetchone()[0]
        missing_raw = conn.execute(
            """
            select count(*) from pages
            where raw_text is null or clean_text is null or clean_text_sha256 is null
            """
        ).fetchone()[0]
        invalid_answerability = conn.execute(
            """
            select count(*) from documents
            where can_answer = 1 and (effective_status != 'current' or review_status != 'approved_for_service')
            """
        ).fetchone()[0]
        units_without_text = conn.execute(
            "select count(*) from content_units where trim(raw_text) = '' or trim(clean_text) = ''"
        ).fetchone()[0]
        duplicate_units = conn.execute(
            "select count(*) from (select document_id,unit_order,count(*) n from content_units group by 1,2 having n > 1)"
        ).fetchone()[0]
        report = {
            "integrity_check": integrity,
            "missing_raw_or_clean_pages": missing_raw,
            "invalid_positive_answer_documents": invalid_answerability,
            "empty_content_units": units_without_text,
            "duplicate_document_unit_orders": duplicate_units,
            "document_count": conn.execute("select count(*) from documents").fetchone()[0],
            "unit_count": conn.execute("select count(*) from content_units").fetchone()[0],
            "measurement_count": conn.execute("select count(*) from unit_measurements").fetchone()[0],
            "artifact_count": conn.execute("select count(*) from source_artifacts").fetchone()[0],
            "quality_findings": [
                dict(row)
                for row in conn.execute(
                    "select severity,finding_type,count(*) as findings from quality_findings group by 1,2 order by 1,2"
                )
            ],
        }
    failures = [key for key, value in report.items() if key != "integrity_check" and key in {
        "missing_raw_or_clean_pages", "invalid_positive_answer_documents", "empty_content_units", "duplicate_document_unit_orders"
    } and value]
    if integrity != "ok" or failures:
        raise RuntimeError(f"v4 corpus validation failed: {report}")
    paths.report_dir.mkdir(parents=True, exist_ok=True)
    (paths.report_dir / "latest_validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report
