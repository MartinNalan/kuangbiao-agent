from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class V4CandidateStore:
    """Mutable v4-only staging store, isolated from the immutable corpus.

    Candidate discovery is an administrative workflow and must not mutate the
    accepted corpus or reopen v3 as an active runtime dependency.  This small
    database preserves the existing candidate endpoint contract without
    exposing any candidate to answer retrieval before a governed ingest.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma journal_mode = wal")
        connection.execute("pragma foreign_keys = on")
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                create table if not exists candidates (
                  candidate_id text primary key,
                  triggering_question text,
                  standard_no text,
                  title text,
                  source_url text,
                  source_type text,
                  text_access text,
                  page_range text,
                  extracted_text text,
                  ocr_confidence real,
                  ocr_engine text,
                  ocr_engine_version text,
                  review_status text not null default 'candidate_found',
                  copyright_note text,
                  created_at text not null,
                  updated_at text not null
                );
                create index if not exists idx_v4_candidates_review_created
                  on candidates(review_status, created_at desc);
                """
            )

    def health(self) -> dict[str, Any]:
        with self._connect() as connection:
            count = int(connection.execute("select count(*) from candidates").fetchone()[0])
        return {
            "status": "ok",
            "candidate_count": count,
            "storage": "v4_candidate_staging_sqlite",
        }

    def create_candidate(self, payload: dict[str, Any]) -> dict[str, Any]:
        candidate_id = str(
            payload.get("candidate_id") or f"candidate-{uuid.uuid4().hex[:12]}"
        )
        review_status = str(payload.get("review_status") or "candidate_found")
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                insert into candidates (
                  candidate_id, triggering_question, standard_no, title, source_url,
                  source_type, text_access, page_range, extracted_text, ocr_confidence,
                  ocr_engine, ocr_engine_version, review_status, copyright_note,
                  created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    payload.get("triggering_question"),
                    payload.get("standard_no"),
                    payload.get("title"),
                    payload.get("source_url"),
                    payload.get("source_type"),
                    payload.get("text_access"),
                    payload.get("page_range"),
                    payload.get("extracted_text"),
                    payload.get("ocr_confidence"),
                    payload.get("ocr_engine"),
                    payload.get("ocr_engine_version"),
                    review_status,
                    payload.get("copyright_note"),
                    now,
                    now,
                ),
            )
        return {
            "ok": True,
            "candidate_id": candidate_id,
            "review_status": review_status,
        }

    def candidates(self, page: int = 1, page_size: int = 50) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))
        offset = (page - 1) * page_size
        with self._connect() as connection:
            total = int(connection.execute("select count(*) from candidates").fetchone()[0])
            rows = connection.execute(
                """
                select * from candidates
                order by created_at desc, candidate_id desc
                limit ? offset ?
                """,
                (page_size, offset),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "pagination": {"page": page, "page_size": page_size, "total": total},
        }

    def close(self) -> None:
        return None
