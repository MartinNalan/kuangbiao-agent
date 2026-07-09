from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.kb_build_utils import split_clause_like_text, stable_id  # noqa: E402
from mining_qa.knowledge_store import DEFAULT_DB_PATH, connect, utc_now  # noqa: E402


STANDARD_SOURCE_TYPES = ("local_kb",)
SOURCE_CHUNK_TYPES = ("text",)


def rebuild_clause_chunks(db_path: Path) -> dict[str, int]:
    now = utc_now()
    inserted = 0
    docs = 0
    with connect(db_path) as conn:
        doc_rows = conn.execute(
            """
            select document_id, title, standard_no, validation_status
            from documents
            where source_type in ({}) and document_type in (
              'standard', 'national_standard', 'industry_standard', 'amendment', 'guidance'
            )
            order by document_id
            """.format(",".join("?" for _ in STANDARD_SOURCE_TYPES)),
            STANDARD_SOURCE_TYPES,
        ).fetchall()
        conn.execute("delete from chunks_fts where chunk_id in (select chunk_id from chunks where chunk_type = 'clause')")
        conn.execute("delete from chunks where chunk_type = 'clause'")
        for doc in doc_rows:
            page_rows = conn.execute(
                """
                select page_start, page_end, text, source_ref, confidence
                from chunks
                where document_id = ? and chunk_type in ({})
                order by coalesce(page_start, 999999), chunk_id
                """.format(",".join("?" for _ in SOURCE_CHUNK_TYPES)),
                (doc["document_id"], *SOURCE_CHUNK_TYPES),
            ).fetchall()
            if not page_rows:
                continue
            docs += 1
            current_page = None
            buffer: list[str] = []
            page_start = None
            page_end = None
            source_refs = []
            confidence_values = []
            rows_to_insert = []
            for row in page_rows:
                page = row["page_start"]
                if current_page is not None and page != current_page:
                    rows_to_insert.extend(
                        build_clause_rows(doc, buffer, page_start, page_end, source_refs, confidence_values, now)
                    )
                    buffer = []
                    source_refs = []
                    confidence_values = []
                    page_start = None
                    page_end = None
                current_page = page
                if page_start is None:
                    page_start = row["page_start"]
                page_end = row["page_end"] or row["page_start"]
                buffer.append(row["text"])
                if row["source_ref"]:
                    source_refs.append(row["source_ref"])
                if row["confidence"] is not None:
                    confidence_values.append(float(row["confidence"]))
            if buffer:
                rows_to_insert.extend(build_clause_rows(doc, buffer, page_start, page_end, source_refs, confidence_values, now))
            conn.executemany(
                """
                insert or replace into chunks (
                  chunk_id,document_id,chunk_type,title,standard_no,section_path,clause_no,
                  page_start,page_end,char_start,char_end,text,table_json,source_type,text_access,
                  parse_method,confidence,validation_status,visibility,source_ref,created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows_to_insert,
            )
            conn.executemany(
                "insert into chunks_fts(chunk_id,document_id,title,standard_no,section_path,text) values (?, ?, ?, ?, ?, ?)",
                [(row[0], row[1], row[3], row[4], row[5], row[11]) for row in rows_to_insert],
            )
            inserted += len(rows_to_insert)
        conn.execute(
            """
            update documents
            set chunk_count = (
              select count(*) from chunks where chunks.document_id = documents.document_id
            ),
            updated_at = ?
            where source_type in ({})
            """.format(",".join("?" for _ in STANDARD_SOURCE_TYPES)),
            (now, *STANDARD_SOURCE_TYPES),
        )
    return {"documents_processed": docs, "clause_chunks_inserted": inserted}


def build_clause_rows(doc, page_texts, page_start, page_end, source_refs, confidence_values, now):
    text = "\n".join(page_texts)
    clauses = split_clause_like_text(text, max_len=1800)
    rows = []
    confidence = sum(confidence_values) / len(confidence_values) if confidence_values else None
    source_ref = source_refs[0] if source_refs else None
    for idx, clause in enumerate(clauses, 1):
        cid = stable_id(doc["document_id"], "clause", page_start, idx, clause["text"], prefix="chunk")
        rows.append(
            (
                cid,
                doc["document_id"],
                "clause",
                doc["title"],
                doc["standard_no"],
                clause.get("section_path"),
                clause.get("clause_no"),
                page_start,
                page_end,
                None,
                None,
                clause["text"],
                None,
                "local_kb",
                "ocr_text",
                "standard_clause_rule",
                confidence,
                doc["validation_status"],
                "internal",
                source_ref,
                now,
            )
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Add clause-level chunks for governed standards/specifications.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args()
    summary = rebuild_clause_chunks(Path(args.db))
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
