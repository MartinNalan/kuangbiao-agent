from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.mnr_policy_allowlist import (  # noqa: E402
    DEFAULT_ALLOWLIST_ARTIFACT,
    DEFAULT_POLICY_CUTOFF,
    allowlist_numbers,
    load_allowlist_artifact,
    normalize_document_number,
    parse_document_date,
)


DEFAULT_WORKBOOK = Path("/home/nalanmading/下载/0. 继续有效文件.xls")
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "knowledge_base" / "db" / "knowledge_base.sqlite"
MANIFEST_DIR = PROJECT_ROOT / "data" / "knowledge_base" / "manifests"
GOVERNANCE_DIR = PROJECT_ROOT / "data" / "knowledge_base" / "governance"
BACKUP_DIR = PROJECT_ROOT / "data" / "private_backups" / "knowledge_base"
POLICY_MANIFEST_JSON = MANIFEST_DIR / "mnr_mineral_policy_manifest.json"
POLICY_MANIFEST_CSV = MANIFEST_DIR / "mnr_mineral_policy_manifest.csv"
POLICY_SUMMARY = PROJECT_ROOT / "data" / "knowledge_base" / "logs" / "mnr_policy_ingest_summary.json"


# These records have blank 文号 fields on their official MNR detail pages. Each
# entry records either a number recoverable from the official body/attachment or
# an explicit conclusion that the page exposes no independent public number.
MISSING_NUMBER_RESOLUTIONS: dict[str, dict[str, str | None]] = {
    "policy-836f29b65f8e9b8f": {
        "document_number": None,
        "status": "official_page_has_no_public_document_number",
        "evidence": "MNR detail-page 文号 field and published body are both blank for document number.",
    },
    "policy-211aad53bfb0259a": {
        "document_number": None,
        "status": "official_page_has_no_public_document_number",
        "evidence": "MNR detail-page 文号 field is blank and the signed body contains no document number.",
    },
    "policy-16c7bdf32c7ffc9c": {
        "document_number": "国土资源部令第23号",
        "status": "recovered_from_official_body",
        "evidence": "Official body states: 2004年1月9日国土资源部令第23号公布.",
    },
    "policy-0e7c7e042b0a934a": {
        "document_number": "国土资源部令第55号",
        "status": "recovered_from_consolidated_rule_identity",
        "evidence": "Consolidated rule is the amended text of the rule promulgated by 国土资源部令第55号.",
    },
    "policy-9823063966875b3f": {
        "document_number": "财综〔2017〕35号",
        "status": "recovered_from_official_attachment_identity",
        "evidence": "Official attachment is the 财综〔2017〕35号 temporary collection measure.",
    },
    "policy-633954c25b9bc83a": {
        "document_number": "法释〔2016〕25号",
        "status": "recovered_from_judicial_interpretation_identity",
        "evidence": "Official published judicial interpretation number is 法释〔2016〕25号.",
    },
    "policy-a83e0f7866611f73": {
        "document_number": "国务院令第242号",
        "status": "recovered_from_official_body",
        "evidence": "Official body identifies the regulation promulgated by 国务院令第242号.",
    },
    "policy-4e60c78e043464c5": {
        "document_number": None,
        "status": "news_release_has_no_independent_document_number",
        "evidence": "Page is a news release; referenced policy numbers are not the page's own document number.",
    },
    "policy-e5b5b944886f6a86": {
        "document_number": None,
        "status": "official_page_has_no_public_document_number",
        "evidence": "Joint notice body and MNR metadata expose no public document number.",
    },
    "policy-d432283c43500b59": {
        "document_number": None,
        "status": "consolidated_law_text_has_no_independent_document_number",
        "evidence": "Page is a consolidated 2009-amended law text and exposes no independent 文号.",
    },
    "policy-6870b51e18768fdf": {
        "document_number": None,
        "status": "official_page_has_no_public_document_number",
        "evidence": "MNR detail-page 文号 field and standalone body expose no public document number.",
    },
    "policy-05f1eb5c3cd51a7b": {
        "document_number": None,
        "status": "comparison_attachment_has_no_independent_document_number",
        "evidence": "Page is a modification comparison table and exposes no independent 文号.",
    },
    "policy-3a439117a7c27e55": {
        "document_number": None,
        "status": "official_page_has_no_public_document_number",
        "evidence": "MNR metadata and the 1987 State Council published body expose no document number.",
    },
    "policy-9c73c643ac84a7a0": {
        "document_number": None,
        "status": "official_page_has_no_public_document_number",
        "evidence": "MNR metadata exposes no original 文号; 国务院令第516号 is the later repeal order.",
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def json_object(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_title(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"[\s《》〈〉“”‘’'\"，,。:：；;（）()\[\]【】]", "", text)
    for prefix in ("自然资源部", "国土资源部", "地质矿产部"):
        text = text.removeprefix(prefix)
    return text


def read_workbook(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        import xlrd  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "xlrd is required. Run this script with /home/nalanmading/.venvs/codex/bin/python."
        ) from exc

    workbook = xlrd.open_workbook(path)
    sheet = workbook.sheet_by_name("Sheet1")
    headers = [re.sub(r"\s+", "", str(sheet.cell_value(2, column))) for column in range(sheet.ncols)]
    title_column = headers.index("文件名称")
    number_column = headers.index("文号")
    entries = []
    for row_index in range(3, sheet.nrows):
        title = str(sheet.cell_value(row_index, title_column)).strip()
        number = str(sheet.cell_value(row_index, number_column)).strip()
        if not title and not number:
            continue
        entries.append(
            {
                "workbook_row": row_index + 1,
                "title": title,
                "document_number": number,
                "normalized_document_number": normalize_document_number(number),
            }
        )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        grouped[entry["normalized_document_number"]].append(entry)
    duplicates = [
        {"normalized_document_number": number, "entries": rows}
        for number, rows in grouped.items()
        if len(rows) > 1
    ]
    return entries, duplicates


def write_allowlist_artifact(workbook: Path, entries: list[dict[str, Any]], duplicates: list[dict[str, Any]]) -> Path:
    DEFAULT_ALLOWLIST_ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "schema_version": 1,
        "source_workbook": str(workbook),
        "source_workbook_sha256": sha256_file(workbook),
        "sheet": "Sheet1",
        "header_row": 3,
        "generated_at": utc_now(),
        "cutoff": DEFAULT_POLICY_CUTOFF.isoformat(),
        "row_count": len(entries),
        "normalized_allowlist_count": len({entry["normalized_document_number"] for entry in entries}),
        "duplicates": duplicates,
        "entries": entries,
    }
    DEFAULT_ALLOWLIST_ARTIFACT.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return DEFAULT_ALLOWLIST_ARTIFACT


def database_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in (
        "documents",
        "chunks",
        "chunks_fts",
        "chunk_vectors",
        "chunk_embeddings",
        "kg_entities",
        "kg_relations",
    ):
        exists = conn.execute(
            "select 1 from sqlite_master where type in ('table', 'virtual table') and name = ?", (table,)
        ).fetchone()
        counts[table] = int(conn.execute(f"select count(*) from {table}").fetchone()[0]) if exists else 0
    return counts


def policy_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        select * from documents
        where source_type = 'official_fulltext'
          and source_platform = '自然资源部政策法规库'
        order by publish_date desc, document_id
        """
    ).fetchall()


def resolve_row_metadata(row: sqlite3.Row) -> dict[str, Any]:
    bibliographic = json_object(row["bibliographic_json"])
    source_trace = json_object(row["source_trace_json"])
    raw_number = row["standard_no"] or bibliographic.get("文号") or ""
    resolution = {
        "document_number": str(raw_number).strip(),
        "document_number_status": "stored_metadata",
        "document_number_evidence": "documents.standard_no or bibliographic_json.文号",
    }
    if not resolution["document_number"]:
        explicit = MISSING_NUMBER_RESOLUTIONS.get(row["document_id"])
        if explicit is None:
            raise RuntimeError(f"Missing document-number resolution for {row['document_id']} {row['title']}")
        resolution = {
            "document_number": explicit["document_number"] or "",
            "document_number_status": explicit["status"],
            "document_number_evidence": explicit["evidence"],
        }
    raw_date = row["publish_date"] or bibliographic.get("成文时间") or bibliographic.get("发布日期") or ""
    published = parse_document_date(raw_date)
    if published is None:
        raise RuntimeError(f"Missing publication-date resolution for {row['document_id']} {row['title']}")
    resolution.update(
        {
            "publication_date": published.isoformat(),
            "publication_date_raw": str(raw_date),
            "source_url": row["official_url"] or source_trace.get("source_url") or "",
            "source_trace": source_trace,
            "bibliographic": bibliographic,
        }
    )
    return resolution


def dependent_counts(conn: sqlite3.Connection, document_id: str) -> dict[str, int]:
    counts = {
        "chunks": conn.execute("select count(*) from chunks where document_id = ?", (document_id,)).fetchone()[0],
        "fts": conn.execute("select count(*) from chunks_fts where document_id = ?", (document_id,)).fetchone()[0],
        "local_vectors": conn.execute(
            "select count(*) from chunk_vectors where chunk_id in (select chunk_id from chunks where document_id = ?)",
            (document_id,),
        ).fetchone()[0],
        "dense_embeddings": conn.execute(
            "select count(*) from chunk_embeddings where chunk_id in (select chunk_id from chunks where document_id = ?)",
            (document_id,),
        ).fetchone()[0],
        "kg_evidence_relations": conn.execute(
            "select count(*) from kg_relations where evidence_chunk_id in (select chunk_id from chunks where document_id = ?)",
            (document_id,),
        ).fetchone()[0],
        "kg_source_entities": conn.execute(
            """
            select count(*) from kg_entities
            where source_id = ? or source_id in (select chunk_id from chunks where document_id = ?)
            """,
            (document_id, document_id),
        ).fetchone()[0],
    }
    return {key: int(value) for key, value in counts.items()}


def referenced_files(row: sqlite3.Row, metadata: dict[str, Any]) -> list[str]:
    trace = metadata["source_trace"]
    paths: list[str] = []
    if trace.get("raw_html"):
        paths.append(str(trace["raw_html"]))
    for attachment in trace.get("attachments") or []:
        if isinstance(attachment, dict) and attachment.get("path"):
            paths.append(str(attachment["path"]))
    return list(dict.fromkeys(paths))


def analyze(conn: sqlite3.Connection, artifact: dict[str, Any]) -> dict[str, Any]:
    allow_numbers = allowlist_numbers(artifact)
    allow_by_number = {
        entry["normalized_document_number"]: entry for entry in artifact.get("entries") or []
    }
    rows = policy_rows(conn)
    analyses = []
    number_to_documents: dict[str, list[str]] = defaultdict(list)
    path_owners: dict[str, set[str]] = defaultdict(set)
    post_cutoff_fingerprints: dict[str, str] = {}

    for row in rows:
        metadata = resolve_row_metadata(row)
        number = metadata["document_number"]
        normalized = normalize_document_number(number)
        published = date.fromisoformat(metadata["publication_date"])
        if normalized:
            number_to_documents[normalized].append(row["document_id"])
        for path in referenced_files(row, metadata):
            path_owners[path].add(row["document_id"])

        if published >= DEFAULT_POLICY_CUTOFF:
            decision = "retain_post_cutoff_untouched"
            reason = "publication_date_on_or_after_2026-01-01"
            post_cutoff_fingerprints[row["document_id"]] = hashlib.sha256(
                json.dumps(dict(row), ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
        elif normalized and normalized in allow_numbers:
            decision = "retain_allowlisted"
            reason = "normalized_document_number_present_in_workbook"
        else:
            decision = "delete"
            reason = (
                "pre_2026_no_public_document_number"
                if not normalized
                else "pre_2026_normalized_document_number_absent_from_workbook"
            )

        workbook_entry = allow_by_number.get(normalized)
        title_conflict = bool(
            workbook_entry and normalize_title(workbook_entry["title"]) != normalize_title(row["title"])
        )
        canonical_number = workbook_entry["document_number"] if workbook_entry else number
        metadata_repair = bool(
            decision == "retain_allowlisted" and canonical_number and str(row["standard_no"] or "") != canonical_number
        )
        analyses.append(
            {
                "document_id": row["document_id"],
                "title": row["title"],
                "document_number": number,
                "normalized_document_number": normalized,
                "canonical_allowlist_document_number": canonical_number if workbook_entry else "",
                "publication_date": metadata["publication_date"],
                "source_url": metadata["source_url"],
                "decision": decision,
                "deletion_reason": reason if decision == "delete" else "",
                "decision_reason": reason,
                "document_number_status": metadata["document_number_status"],
                "document_number_evidence": metadata["document_number_evidence"],
                "title_conflict": title_conflict,
                "workbook_title": workbook_entry["title"] if workbook_entry else "",
                "metadata_repair": metadata_repair,
                "dependent_counts": dependent_counts(conn, row["document_id"]),
                "referenced_files": referenced_files(row, metadata),
            }
        )

    duplicates = [
        {"normalized_document_number": number, "document_ids": ids}
        for number, ids in number_to_documents.items()
        if len(ids) > 1
    ]
    deleted_ids = {item["document_id"] for item in analyses if item["decision"] == "delete"}
    exclusive_files = []
    shared_files = []
    for path, owners in path_owners.items():
        deleted_owners = owners & deleted_ids
        if not deleted_owners:
            continue
        if owners <= deleted_ids:
            exclusive_files.append(path)
        else:
            shared_files.append({"path": path, "owners": sorted(owners)})

    return {
        "rows": analyses,
        "in_scope_count": len(analyses),
        "retained_allowlisted_count": sum(item["decision"] == "retain_allowlisted" for item in analyses),
        "deleted_count": sum(item["decision"] == "delete" for item in analyses),
        "post_cutoff_untouched_count": sum(item["decision"] == "retain_post_cutoff_untouched" for item in analyses),
        "metadata_repaired_count": sum(item["metadata_repair"] for item in analyses),
        "resolved_missing_number_count": sum(item["document_number_status"] != "stored_metadata" for item in analyses),
        "no_public_number_count": sum(
            item["document_number_status"] != "stored_metadata" and not item["document_number"] for item in analyses
        ),
        "ambiguous_count": 0,
        "duplicate_document_numbers": duplicates,
        "title_conflicts": [item for item in analyses if item["title_conflict"]],
        "exclusive_files": sorted(exclusive_files),
        "shared_files": shared_files,
        "post_cutoff_fingerprints": post_cutoff_fingerprints,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "document_id",
        "title",
        "document_number",
        "normalized_document_number",
        "publication_date",
        "source_url",
        "deletion_reason",
        "document_number_status",
        "chunks",
        "fts",
        "local_vectors",
        "dense_embeddings",
        "kg_evidence_relations",
        "kg_source_entities",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            output = {key: row.get(key, "") for key in fields}
            output.update(row.get("dependent_counts") or {})
            writer.writerow(output)


def write_dry_run(analysis: dict[str, Any], artifact: dict[str, Any], before: dict[str, int], run_stamp: str) -> dict[str, Path]:
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    deleted = [row for row in analysis["rows"] if row["decision"] == "delete"]
    json_path = MANIFEST_DIR / f"t016_policy_cleanup_dry_run_{run_stamp}.json"
    csv_path = MANIFEST_DIR / f"t016_deleted_documents_{run_stamp}.csv"
    validation_ids_path = GOVERNANCE_DIR / f"t016_deleted_ids_{run_stamp}.json"
    payload = {
        "task": "T016",
        "mode": "dry_run",
        "generated_at": utc_now(),
        "workbook_row_count": artifact["row_count"],
        "normalized_allowlist_count": artifact["normalized_allowlist_count"],
        "before_counts": before,
        "summary": {key: value for key, value in analysis.items() if key not in {"rows", "post_cutoff_fingerprints"}},
        "deleted_documents": deleted,
        "retained_documents": [row for row in analysis["rows"] if row["decision"] != "delete"],
        "cloud_sync_required": True,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(csv_path, deleted)
    validation_ids_path.parent.mkdir(parents=True, exist_ok=True)
    validation_ids_path.write_text(
        json.dumps(
            {
                "document_ids": [row["document_id"] for row in deleted],
                "chunk_ids": [],
                "populated_during_apply": True,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"json": json_path, "csv": csv_path, "validation_ids": validation_ids_path}


def create_backup(db_path: Path, run_stamp: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"knowledge_base.sqlite.pre_t016_{run_stamp}.bak"
    source = sqlite3.connect(db_path)
    destination = sqlite3.connect(backup)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return backup


def update_policy_manifests(retained_ids: set[str]) -> dict[str, int]:
    result = {"manifest_rows_before": 0, "manifest_rows_after": 0}
    if not POLICY_MANIFEST_JSON.exists():
        return result
    rows = json.loads(POLICY_MANIFEST_JSON.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        return result
    result["manifest_rows_before"] = len(rows)
    retained = [row for row in rows if row.get("document_id") in retained_ids]
    result["manifest_rows_after"] = len(retained)
    POLICY_MANIFEST_JSON.write_text(json.dumps(retained, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if retained:
        fields = list(retained[0].keys())
        with POLICY_MANIFEST_CSV.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(retained)
    elif POLICY_MANIFEST_CSV.exists():
        POLICY_MANIFEST_CSV.write_text("", encoding="utf-8")
    if POLICY_SUMMARY.exists():
        summary = json.loads(POLICY_SUMMARY.read_text(encoding="utf-8"))
        summary["document_count"] = len(retained)
        summary["governed_by_allowlist"] = str(DEFAULT_ALLOWLIST_ARTIFACT)
        summary["updated_at"] = utc_now()
        POLICY_SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def apply_cleanup(
    db_path: Path,
    analysis: dict[str, Any],
    dry_run_paths: dict[str, Path],
    run_stamp: str,
) -> dict[str, Any]:
    deleted_rows = [row for row in analysis["rows"] if row["decision"] == "delete"]
    deleted_ids = [row["document_id"] for row in deleted_rows]
    retained_ids = {row["document_id"] for row in analysis["rows"] if row["decision"] != "delete"}
    backup = create_backup(db_path, run_stamp)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        chunk_ids = [
            row[0]
            for row in conn.execute(
                "select chunk_id from chunks where document_id in ({})".format(
                    ",".join("?" for _ in deleted_ids)
                ),
                deleted_ids,
            ).fetchall()
        ]
        validation_payload = {
            "document_ids": deleted_ids,
            "chunk_ids": chunk_ids,
            "generated_at": utc_now(),
        }
        dry_run_paths["validation_ids"].write_text(
            json.dumps(validation_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        conn.execute("begin immediate")
        conn.execute("create temp table t016_deleted_docs(document_id text primary key)")
        conn.execute("create temp table t016_deleted_chunks(chunk_id text primary key)")
        conn.executemany("insert into t016_deleted_docs values (?)", [(value,) for value in deleted_ids])
        conn.executemany("insert into t016_deleted_chunks values (?)", [(value,) for value in chunk_ids])

        conn.execute(
            """
            delete from kg_relations
            where evidence_chunk_id in (select chunk_id from t016_deleted_chunks)
               or source_entity_id in (
                    select entity_id from kg_entities
                    where source_id in (select document_id from t016_deleted_docs)
                       or source_id in (select chunk_id from t016_deleted_chunks)
               )
               or target_entity_id in (
                    select entity_id from kg_entities
                    where source_id in (select document_id from t016_deleted_docs)
                       or source_id in (select chunk_id from t016_deleted_chunks)
               )
            """
        )
        conn.execute(
            """
            delete from kg_entities
            where source_id in (select document_id from t016_deleted_docs)
               or source_id in (select chunk_id from t016_deleted_chunks)
            """
        )
        conn.execute(
            """
            delete from kg_entities
            where entity_id not in (select source_entity_id from kg_relations)
              and entity_id not in (select target_entity_id from kg_relations)
            """
        )
        conn.execute("delete from chunk_vectors where chunk_id in (select chunk_id from t016_deleted_chunks)")
        conn.execute("delete from chunk_embeddings where chunk_id in (select chunk_id from t016_deleted_chunks)")
        conn.execute("delete from chunks_fts where document_id in (select document_id from t016_deleted_docs)")
        conn.execute("delete from chunks where document_id in (select document_id from t016_deleted_docs)")
        conn.execute("delete from documents where document_id in (select document_id from t016_deleted_docs)")

        for row in analysis["rows"]:
            if not row["metadata_repair"]:
                continue
            current = conn.execute(
                "select bibliographic_json from documents where document_id = ?", (row["document_id"],)
            ).fetchone()
            if not current:
                continue
            bibliographic = json_object(current["bibliographic_json"])
            bibliographic["文号"] = row["canonical_allowlist_document_number"]
            conn.execute(
                "update documents set standard_no = ?, bibliographic_json = ?, updated_at = ? where document_id = ?",
                (
                    row["canonical_allowlist_document_number"],
                    json.dumps(bibliographic, ensure_ascii=False),
                    utc_now(),
                    row["document_id"],
                ),
            )
            conn.execute(
                "update chunks set standard_no = ? where document_id = ?",
                (row["canonical_allowlist_document_number"], row["document_id"]),
            )
            conn.execute(
                "update chunks_fts set standard_no = ? where document_id = ?",
                (row["canonical_allowlist_document_number"], row["document_id"]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    deleted_files = []
    missing_files = []
    failed_files = []
    for relative in analysis["exclusive_files"]:
        path = Path(relative)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.exists():
            missing_files.append(str(path))
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            deleted_files.append(str(path))
            parent = path.parent
            if parent.name.startswith("policy-") and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError as exc:
            failed_files.append({"path": str(path), "error": str(exc)})

    manifest_update = update_policy_manifests(retained_ids)
    with sqlite3.connect(db_path) as check_conn:
        after_counts = database_counts(check_conn)
        integrity = check_conn.execute("pragma integrity_check").fetchone()[0]
    return {
        "backup_path": str(backup),
        "deleted_files": deleted_files,
        "missing_files": missing_files,
        "failed_files": failed_files,
        "shared_reference_files_retained": analysis["shared_files"],
        "shared_list_pages_retained": len(list((PROJECT_ROOT / "data/knowledge_base/raw/mnr_policy").glob("list_*.html"))),
        "manifest_update": manifest_update,
        "after_counts": after_counts,
        "integrity_check": integrity,
    }


def validate(db_path: Path, artifact_path: Path, validation_ids_path: Path | None = None) -> dict[str, Any]:
    artifact = load_allowlist_artifact(artifact_path)
    allowed = allowlist_numbers(artifact)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = policy_rows(conn)
        invalid_remaining = []
        post_cutoff = []
        for row in rows:
            metadata = resolve_row_metadata(row)
            published = date.fromisoformat(metadata["publication_date"])
            normalized = normalize_document_number(metadata["document_number"])
            if published >= DEFAULT_POLICY_CUTOFF:
                post_cutoff.append(row["document_id"])
            elif normalized not in allowed:
                invalid_remaining.append(
                    {
                        "document_id": row["document_id"],
                        "title": row["title"],
                        "document_number": metadata["document_number"],
                    }
                )
        residuals = {
            "documents": 0,
            "chunks": 0,
            "fts": 0,
            "local_vectors": 0,
            "dense_embeddings": 0,
            "kg_evidence_relations": 0,
            "kg_source_entities": 0,
        }
        if validation_ids_path:
            ids = json.loads(validation_ids_path.read_text(encoding="utf-8"))
            document_ids = ids.get("document_ids") or []
            chunk_ids = ids.get("chunk_ids") or []
            conn.execute("create temp table validation_docs(id text primary key)")
            conn.execute("create temp table validation_chunks(id text primary key)")
            conn.executemany("insert into validation_docs values (?)", [(value,) for value in document_ids])
            conn.executemany("insert into validation_chunks values (?)", [(value,) for value in chunk_ids])
            residuals = {
                "documents": conn.execute(
                    "select count(*) from documents where document_id in (select id from validation_docs)"
                ).fetchone()[0],
                "chunks": conn.execute(
                    "select count(*) from chunks where chunk_id in (select id from validation_chunks)"
                ).fetchone()[0],
                "fts": conn.execute(
                    "select count(*) from chunks_fts where chunk_id in (select id from validation_chunks)"
                ).fetchone()[0],
                "local_vectors": conn.execute(
                    "select count(*) from chunk_vectors where chunk_id in (select id from validation_chunks)"
                ).fetchone()[0],
                "dense_embeddings": conn.execute(
                    "select count(*) from chunk_embeddings where chunk_id in (select id from validation_chunks)"
                ).fetchone()[0],
                "kg_evidence_relations": conn.execute(
                    "select count(*) from kg_relations where evidence_chunk_id in (select id from validation_chunks)"
                ).fetchone()[0],
                "kg_source_entities": conn.execute(
                    """
                    select count(*) from kg_entities
                    where source_id in (select id from validation_docs)
                       or source_id in (select id from validation_chunks)
                    """
                ).fetchone()[0],
            }
        authority = conn.execute(
            "select count(*) from documents where standard_no = '自然资规〔2023〕6号'"
        ).fetchone()[0]
        integrity = conn.execute("pragma integrity_check").fetchone()[0]
        result = {
            "ok": not invalid_remaining
            and not any(int(value) for value in residuals.values())
            and authority == 1
            and integrity == "ok",
            "remaining_in_scope_policy_documents": len(rows),
            "remaining_pre_cutoff_invalid": invalid_remaining,
            "post_cutoff_document_ids": post_cutoff,
            "residuals": residuals,
            "authority_policy_count": authority,
            "integrity_check": integrity,
        }
        return result
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Govern pre-2026 MNR policy corpus with the valid-document workbook.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--apply", action="store_true", help="Apply the destructive cleanup after writing a dry-run manifest.")
    parser.add_argument("--validate", action="store_true", help="Validate an already-cleaned database.")
    parser.add_argument("--validation-ids", type=Path, default=None)
    args = parser.parse_args()

    if args.validate:
        result = validate(args.db, DEFAULT_ALLOWLIST_ARTIFACT, args.validation_ids)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    entries, duplicates = read_workbook(args.workbook)
    artifact_path = write_allowlist_artifact(args.workbook, entries, duplicates)
    artifact = load_allowlist_artifact(artifact_path)
    run_stamp = timestamp()
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        before_counts = database_counts(conn)
        analysis = analyze(conn, artifact)
    finally:
        conn.close()
    dry_run_paths = write_dry_run(analysis, artifact, before_counts, run_stamp)
    summary = {
        "task": "T016",
        "mode": "dry_run",
        "allowlist_artifact": str(artifact_path),
        "dry_run_manifest": str(dry_run_paths["json"]),
        "deletion_manifest_csv": str(dry_run_paths["csv"]),
        "validation_ids": str(dry_run_paths["validation_ids"]),
        "workbook_rows": artifact["row_count"],
        "normalized_allowlist_count": artifact["normalized_allowlist_count"],
        "in_scope_count": analysis["in_scope_count"],
        "retained_allowlisted_count": analysis["retained_allowlisted_count"],
        "deleted_count": analysis["deleted_count"],
        "post_cutoff_untouched_count": analysis["post_cutoff_untouched_count"],
        "metadata_repaired_count": analysis["metadata_repaired_count"],
        "resolved_missing_number_count": analysis["resolved_missing_number_count"],
        "ambiguous_count": analysis["ambiguous_count"],
        "before_counts": before_counts,
        "cloud_sync_required": True,
    }
    if args.apply:
        applied = apply_cleanup(args.db, analysis, dry_run_paths, run_stamp)
        validation = validate(args.db, artifact_path, dry_run_paths["validation_ids"])
        report_path = MANIFEST_DIR / f"t016_policy_cleanup_report_{run_stamp}.json"
        summary.update({"mode": "applied", **applied, "validation": validation})
        report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary["completion_report"] = str(report_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
