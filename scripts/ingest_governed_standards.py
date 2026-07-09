from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORE_ROOT = Path("/home/nalanmading/My-project/ore_expert")
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.knowledge_store import DEFAULT_DB_PATH, connect, official_source, reset_db  # noqa: E402


SOURCE_DIRS = [
    ("compilation", ORE_ROOT / "knowledge_governance" / "compilation_standards" / "json"),
    ("supplement", ORE_ROOT / "knowledge_governance" / "supplement_ingest" / "processed_documents" / "json"),
]

KB_ROOT = PROJECT_ROOT / "data" / "knowledge_base"
MANIFEST_DIR = KB_ROOT / "manifests"
LOG_DIR = KB_ROOT / "logs"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_id(*parts: Any, prefix: str = "chunk") -> str:
    raw = "\n".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


def normalize_status(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    if value in {"current", "current_replacement", "active"}:
        return "current"
    if value in {"deprecated", "replaced", "superseded"}:
        return "deprecated_or_replaced"
    return "unknown" if not value else value


def document_type(standard_no: str | None, title: str) -> str:
    code = (standard_no or "").upper()
    if code.startswith("GB"):
        return "national_standard"
    if code.startswith(("DZ", "MT", "EJ")):
        return "industry_standard"
    if "修改单" in title:
        return "amendment"
    if "300问" in title:
        return "guidance"
    return "standard"


def source_priority(doc: dict[str, Any]) -> int | None:
    authority = doc.get("authority") or {}
    value = authority.get("source_priority")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def page_confidence(page: dict[str, Any]) -> float | None:
    quality = page.get("quality") or {}
    for key in ("avg_score", "average_score", "confidence"):
        value = quality.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    lines = page.get("lines") or []
    scores = []
    for line in lines:
        value = line.get("score") if isinstance(line, dict) else None
        if value is not None:
            try:
                scores.append(float(value))
            except (TypeError, ValueError):
                pass
    return sum(scores) / len(scores) if scores else None


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_page_text(text: str, target_len: int = 900, max_len: int = 1400) -> list[tuple[int, int, str]]:
    text = clean_text(text)
    if not text:
        return []
    units = [u.strip() for u in re.split(r"\n\s*\n", text) if u.strip()]
    if len(units) <= 1:
        units = [u.strip() for u in text.split("\n") if u.strip()]
    chunks: list[tuple[int, int, str]] = []
    current: list[str] = []
    start = 0
    cursor = 0
    for unit in units:
        pos = text.find(unit, cursor)
        if pos < 0:
            pos = cursor
        candidate = ("\n".join(current + [unit])).strip()
        if current and (len(candidate) > max_len or len("\n".join(current)) >= target_len):
            chunk_text = "\n".join(current).strip()
            end = start + len(chunk_text)
            chunks.append((start, end, chunk_text))
            current = [unit]
            start = pos
        else:
            if not current:
                start = pos
            current.append(unit)
        cursor = pos + len(unit)
    if current:
        chunk_text = "\n".join(current).strip()
        chunks.append((start, start + len(chunk_text), chunk_text))
    return chunks


def clause_no(text: str) -> str | None:
    for line in text.splitlines()[:8]:
        m = re.match(r"\s*((?:[0-9]+|[A-Z])(?:[.．][0-9A-Z]+){0,5})\s+[\u4e00-\u9fffA-Za-z]", line)
        if m:
            return m.group(1).replace("．", ".")
    return None


def section_path(text: str, fallback: str) -> str:
    for line in text.splitlines()[:10]:
        line = line.strip()
        if re.match(r"^(前言|引言|参考文献)$", line):
            return line
        if re.match(r"^附录\s*[A-ZＡ-Ｚ]", line):
            return line[:80]
        if re.match(r"^[0-9]+\s+[\u4e00-\u9fff]", line):
            return line[:80]
    return fallback


def table_to_text(table: dict[str, Any]) -> str:
    lines = []
    caption = table.get("caption") or ""
    if caption:
        lines.append(caption)
    matrix = table.get("matrix") or []
    for row in matrix:
        lines.append("\t".join(re.sub(r"\s+", " ", str(cell)).strip() for cell in row))
    if table.get("merged_cells"):
        lines.append("合并单元格: " + json.dumps(table["merged_cells"], ensure_ascii=False))
    return "\n".join(line for line in lines if line.strip()).strip()


def iter_documents() -> list[tuple[str, Path, dict[str, Any]]]:
    docs: list[tuple[str, Path, dict[str, Any]]] = []
    for collection, source_dir in SOURCE_DIRS:
        if not source_dir.exists():
            continue
        for path in sorted(source_dir.glob("*.json")):
            docs.append((collection, path, json.loads(path.read_text(encoding="utf-8"))))
    return docs


def insert_document(conn, collection: str, path: Path, doc: dict[str, Any], ingestion_time: str) -> tuple[int, int]:
    bib = doc.get("bibliographic") or {}
    title = bib.get("title") or doc.get("title") or path.stem
    standard_no = bib.get("standard_code") or bib.get("standard_no")
    doc_id = doc.get("document_id") or stable_id(path, title, prefix="doc")
    manual_tables = doc.get("manual_table_corrections") or []
    validation_status = "table_verified" if manual_tables else "parsed"
    pages = doc.get("pages") or []
    page_count = len(pages)
    doc_status = normalize_status(bib.get("status") or doc.get("status"))
    if doc_status == "unknown" and collection == "compilation":
        doc_status = "current"
    source_platform, official_url = official_source(standard_no)
    source_ref = str(path)
    priority = source_priority(doc)
    conn.execute(
        """
        insert or replace into documents (
          document_id, title, standard_no, document_type, status, source_type,
          text_access, validation_status, visibility, review_status,
          publish_date, implementation_date, ingestion_time, updated_at,
          source_priority, source_trace_json, bibliographic_json, quality_json,
          page_count, chunk_count, table_count, can_answer, official_url, source_platform
        ) values (?, ?, ?, ?, ?, 'local_kb', 'ocr_text', ?, 'internal',
          'approved_for_service', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, ?, ?)
        """,
        (
            doc_id,
            title,
            standard_no,
            document_type(standard_no, title),
            doc_status,
            validation_status,
            bib.get("publish_date"),
            bib.get("implementation_date"),
            ingestion_time,
            ingestion_time,
            priority,
            json.dumps(doc.get("source_trace") or {"json_path": source_ref, "collection": collection}, ensure_ascii=False),
            json.dumps(bib, ensure_ascii=False),
            json.dumps(doc.get("quality") or {}, ensure_ascii=False),
            page_count,
            len(manual_tables),
            official_url,
            source_platform,
        ),
    )

    chunks = []
    for page in pages:
        page_no = page.get("standard_page") or page.get("page")
        try:
            page_no = int(page_no)
        except (TypeError, ValueError):
            page_no = None
        page_text = page.get("ocr_text") or ""
        confidence = page_confidence(page)
        fallback_section = f"第 {page_no} 页" if page_no else "未分页文本"
        for idx, (start, end, chunk_text) in enumerate(split_page_text(page_text), 1):
            cid = stable_id(doc_id, page_no, idx, chunk_text)
            chunks.append(
                (
                    cid,
                    doc_id,
                    "text",
                    title,
                    standard_no,
                    section_path(chunk_text, fallback_section),
                    clause_no(chunk_text),
                    page_no,
                    page_no,
                    start,
                    end,
                    chunk_text,
                    None,
                    "local_kb",
                    "ocr_text",
                    "ocr_page_chunk",
                    confidence,
                    validation_status,
                    "internal",
                    page.get("source_page_json") or source_ref,
                    ingestion_time,
                )
            )

    for idx, table in enumerate(manual_tables, 1):
        table_text = table_to_text(table)
        if not table_text:
            continue
        cid = stable_id(doc_id, "manual_table", idx, table.get("table_hash"), table_text)
        caption = table.get("caption") or f"人工校核表格 {idx}"
        chunks.append(
            (
                cid,
                doc_id,
                "table",
                title,
                standard_no,
                caption,
                None,
                None,
                None,
                None,
                None,
                table_text,
                json.dumps(table, ensure_ascii=False),
                "local_kb",
                "ocr_text",
                "manual_table_correction",
                None,
                "table_verified",
                "internal",
                table.get("source_file") or source_ref,
                ingestion_time,
            )
        )

    conn.executemany(
        """
        insert or replace into chunks (
          chunk_id, document_id, chunk_type, title, standard_no, section_path,
          clause_no, page_start, page_end, char_start, char_end, text, table_json,
          source_type, text_access, parse_method, confidence, validation_status,
          visibility, source_ref, created_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        chunks,
    )
    conn.executemany(
        """
        insert into chunks_fts(chunk_id, document_id, title, standard_no, section_path, text)
        values (?, ?, ?, ?, ?, ?)
        """,
        [(c[0], c[1], c[3], c[4], c[5], c[11]) for c in chunks],
    )
    conn.execute(
        "update documents set chunk_count = ?, can_answer = ? where document_id = ?",
        (len(chunks), 1 if chunks else 0, doc_id),
    )
    return len(chunks), len(manual_tables)


def main() -> int:
    global ORE_ROOT, SOURCE_DIRS
    parser = argparse.ArgumentParser(description="Ingest governed ore standards into the local KB SQLite/FTS database.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite DB path")
    parser.add_argument("--source-root", default=str(ORE_ROOT), help="Governed ore_expert project root")
    args = parser.parse_args()

    ORE_ROOT = Path(args.source_root)
    SOURCE_DIRS = [
        ("compilation", ORE_ROOT / "knowledge_governance" / "compilation_standards" / "json"),
        ("supplement", ORE_ROOT / "knowledge_governance" / "supplement_ingest" / "processed_documents" / "json"),
    ]

    db_path = Path(args.db)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    reset_db(db_path)

    docs = iter_documents()
    ingestion_time = utc_now()
    run_id = stable_id(ingestion_time, str(ORE_ROOT), prefix="ingest")
    manifest_rows = []
    total_chunks = 0
    total_tables = 0
    with connect(db_path) as conn:
        conn.execute(
            "insert into ingest_runs(run_id, source_root, started_at, status) values (?, ?, ?, 'running')",
            (run_id, str(ORE_ROOT), ingestion_time),
        )
        for collection, path, doc in docs:
            chunks, tables = insert_document(conn, collection, path, doc, ingestion_time)
            total_chunks += chunks
            total_tables += tables
            bib = doc.get("bibliographic") or {}
            manifest_rows.append(
                {
                    "collection": collection,
                    "document_id": doc.get("document_id"),
                    "standard_no": bib.get("standard_code") or "",
                    "title": bib.get("title") or "",
                    "json_path": str(path),
                    "chunk_count": chunks,
                    "manual_table_count": tables,
                }
            )
        finished = utc_now()
        summary = {"document_count": len(docs), "chunk_count": total_chunks, "table_count": total_tables}
        conn.execute(
            """
            update ingest_runs
            set finished_at = ?, document_count = ?, chunk_count = ?, table_count = ?, status = 'completed', summary_json = ?
            where run_id = ?
            """,
            (finished, len(docs), total_chunks, total_tables, json.dumps(summary, ensure_ascii=False), run_id),
        )

    manifest_csv = MANIFEST_DIR / "governed_standards_ingest_manifest.csv"
    with manifest_csv.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(manifest_rows[0].keys()) if manifest_rows else [])
        if manifest_rows:
            writer.writeheader()
            writer.writerows(manifest_rows)
    manifest_json = MANIFEST_DIR / "governed_standards_ingest_manifest.json"
    manifest_json.write_text(json.dumps(manifest_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path = LOG_DIR / "last_ingest_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "db_path": str(db_path),
                "source_root": str(ORE_ROOT),
                "document_count": len(docs),
                "chunk_count": total_chunks,
                "manual_table_count": total_tables,
                "manifest_csv": str(manifest_csv),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"documents: {len(docs)}")
    print(f"chunks: {total_chunks}")
    print(f"manual tables: {total_tables}")
    print(f"db: {db_path}")
    print(f"manifest: {manifest_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
