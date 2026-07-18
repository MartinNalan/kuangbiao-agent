"""Reconcile document effectiveness metadata and amendment clause effects.

The script is intentionally conservative: it only records whole-clause
deletions that are explicit in an amendment. Text-level deletions and
replacements remain visible in the amendment but do not suppress an entire
parent clause.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.knowledge_store import DEFAULT_DB_PATH, connect, init_db, utc_now
from mining_qa.mnr_policy_allowlist import normalize_document_number


STANDARD_CODE = re.compile(r"\b(?:GB(?:/T)?|DZ/T|DZ)\s*\d+(?:\.\d+)*[—-]\d{4}\b", re.I)
CLAUSE_CODE = r"(?:[A-Z]\.)?\d+(?:\.\d+)+"
WHOLE_DELETE = re.compile(
    rf"删除\s*[“\"]?((?:{CLAUSE_CODE})(?:\s*[、，,]\s*{CLAUSE_CODE})*)\s*[”\"]?\s*(?:[。；;]|$)"
)
BODY_DELETE = re.compile(rf"删除(?:正文中?|目次及正文中?)\s*({CLAUSE_CODE})(?:内容)?", re.I)
WHOLE_MARKER = re.compile(rf"删除\s*({CLAUSE_CODE})[^。；;]{{0,16}}整条")


def now_date() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def backup_database(source: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"knowledge_base.sqlite.pre_governance_{stamp}.bak"
    with sqlite3.connect(source) as source_conn, sqlite3.connect(target) as target_conn:
        source_conn.backup(target_conn)
    return target


def document_text(conn: sqlite3.Connection, document_id: str) -> str:
    rows = conn.execute(
        "select text from chunks where document_id = ? order by coalesce(page_start, 99999), chunk_id",
        (document_id,),
    ).fetchall()
    return "\n".join(str(row["text"] or "") for row in rows)


def explicit_deleted_clauses(text: str) -> set[str]:
    values: set[str] = set()
    clean = re.sub(r"\s+", "", text or "")
    for match in WHOLE_DELETE.finditer(clean):
        values.update(re.findall(CLAUSE_CODE, match.group(1)))
    for match in BODY_DELETE.finditer(clean):
        values.add(match.group(1))
    for match in WHOLE_MARKER.finditer(clean):
        values.add(match.group(1))
    return values


def status_from_metadata(metadata: dict[str, object]) -> tuple[str, str | None]:
    status = str(metadata.get("时效状态") or "").strip()
    evidence = str(metadata.get("废止(失效)记录") or "").strip() or None
    if any(marker in status for marker in ("废止", "失效")):
        return "repealed", evidence or status
    if status in {"现行有效", "有效", "现行"}:
        return "current", status
    return "unverified", evidence or status or None


def reconcile(conn: sqlite3.Connection) -> dict[str, int]:
    timestamp = now_date()
    results = {"policies": 0, "repealed": 0, "amendments": 0, "relations": 0, "deleted_clauses": 0}

    for row in conn.execute(
        "select document_id, bibliographic_json from documents where document_type = 'policy_document'"
    ).fetchall():
        try:
            metadata = json.loads(row["bibliographic_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        effective_status, evidence = status_from_metadata(metadata)
        conn.execute(
            """
            update documents
            set effective_status = ?, status_source = 'mnr_policy_metadata',
                status_evidence = ?, status_checked_at = ?
            where document_id = ?
            """,
            (effective_status, evidence, timestamp, row["document_id"]),
        )
        results["policies"] += 1
        results["repealed"] += int(effective_status == "repealed")

    amendments = conn.execute(
        "select document_id, title from documents where document_type = 'amendment'"
    ).fetchall()
    for amendment in amendments:
        text = document_text(conn, amendment["document_id"])
        match = STANDARD_CODE.search(text.replace("—", "-"))
        if not match:
            continue
        standard_no = re.sub(r"\s+", " ", match.group(0).replace("—", "-")).strip()
        parent = conn.execute(
            "select document_id from documents where replace(upper(standard_no), ' ', '') = ? limit 1",
            (standard_no.upper().replace(" ", ""),),
        ).fetchone()
        parent_id = parent["document_id"] if parent else None
        conn.execute(
            """
            update documents
            set standard_no = ?, effective_status = 'current', status_source = 'amendment_approval',
                status_evidence = '修改单正文载明批准并实施', status_checked_at = ?
            where document_id = ?
            """,
            (standard_no, timestamp, amendment["document_id"]),
        )
        relation_id = f"rel_amends_{amendment['document_id']}"
        conn.execute(
            """
            insert or replace into document_relations(
              relation_id,source_document_id,relation_type,target_document_id,target_standard_no,
              effective_date,evidence_chunk_id,details_json,created_at
            ) values (?, ?, 'AMENDS', ?, ?, ?, null, ?, ?)
            """,
            (relation_id, amendment["document_id"], parent_id, standard_no, "2020-04-30", json.dumps({"title": amendment["title"]}, ensure_ascii=False), timestamp),
        )
        results["amendments"] += 1
        results["relations"] += 1
        for clause in sorted(explicit_deleted_clauses(text)):
            evidence = conn.execute(
                "select chunk_id, text from chunks where document_id = ? and text like ? order by length(text) limit 1",
                (amendment["document_id"], f"%{clause}%"),
            ).fetchone()
            effect_id = f"effect_delete_{amendment['document_id']}_{clause}".replace(".", "_")
            conn.execute(
                """
                insert or replace into clause_effects(
                  effect_id,amendment_document_id,target_document_id,target_standard_no,clause_no,
                  effect_type,effective_date,evidence_chunk_id,evidence_text,created_at
                ) values (?, ?, ?, ?, ?, 'delete', ?, ?, ?, ?)
                """,
                (
                    effect_id, amendment["document_id"], parent_id, standard_no, clause,
                    "2020-04-30", evidence["chunk_id"] if evidence else None,
                    evidence["text"][:800] if evidence else None, timestamp,
                ),
            )
            results["deleted_clauses"] += 1
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--apply", action="store_true", help="write the reconciled governance metadata")
    args = parser.parse_args()
    init_db(args.db)
    if not args.apply:
        print("Dry run only. Re-run with --apply to create a backup and update governance metadata.")
        return 0
    backup = backup_database(args.db, args.db.parent / "backups")
    with connect(args.db) as conn:
        result = reconcile(conn)
    print(json.dumps({"backup": str(backup), **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
