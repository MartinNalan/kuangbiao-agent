from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.v4_governance import (  # noqa: E402
    REMEDIATION_METHODS,
    load_json_object,
    priority_for_quality,
    sha256_text,
)


DEFAULT_ROOT = PROJECT_ROOT / "data" / "knowledge_base_v4"
DEFAULT_DB = DEFAULT_ROOT / "db" / "corpus.sqlite"
QUALITY_TYPES = {
    "attachment_requires_parser_benchmark",
    "empty_source_page",
    "identity_status_governance_conflict",
    "low_ocr_confidence",
    "missing_standard_number",
    "oversized_structural_unit",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def artifacts_for_document(conn: sqlite3.Connection, document_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            select a.artifact_id,a.path,a.artifact_type,a.sha256,a.bytes,a.source_url,a.exists_on_disk,da.artifact_role
            from document_artifacts da join source_artifacts a using(artifact_id)
            where da.document_id=?
            order by case da.artifact_role when 'primary_source' then 0 when 'governed_json' then 1 else 2 end,a.path
            """,
            (document_id,),
        )
    ]


def page_for_finding(
    conn: sqlite3.Connection,
    document_id: str,
    evidence: dict[str, Any],
) -> dict[str, Any] | None:
    page_no = evidence.get("page_no")
    if page_no is None:
        return None
    row = conn.execute(
        "select * from pages where document_id=? and page_no=? order by page_id limit 1",
        (document_id, page_no),
    ).fetchone()
    return dict(row) if row else None


def make_item(conn: sqlite3.Connection, finding: dict[str, Any], generated_at: str) -> dict[str, Any]:
    evidence = load_json_object(finding["evidence_json"])
    priority, priority_order = priority_for_quality(finding["finding_type"], finding["effective_status"])
    page = page_for_finding(conn, finding["document_id"], evidence)
    unit = None
    if finding["unit_id"]:
        row = conn.execute("select * from content_units where unit_id=?", (finding["unit_id"],)).fetchone()
        unit = dict(row) if row else None
    artifacts = artifacts_for_document(conn, finding["document_id"])
    decision = "accepted_no_code_relation" if (
        finding["finding_type"] == "missing_standard_number"
        and load_json_object(finding["source_metadata_json"]).get("t020_governance", {}).get("identity_relation")
    ) else "pending_remediation"
    return {
        "priority": priority,
        "priority_order": priority_order,
        "decision": decision,
        "finding_id": finding["finding_id"],
        "finding_type": finding["finding_type"],
        "severity": finding["severity"],
        "document_id": finding["document_id"],
        "standard_no": finding["standard_no"],
        "title": finding["title"],
        "document_type": finding["document_type"],
        "effective_status": finding["effective_status"],
        "review_status": finding["review_status"],
        "can_answer": finding["can_answer"],
        "unit_id": finding["unit_id"],
        "page_id": page.get("page_id") if page else None,
        "page_no": page.get("page_no") if page else evidence.get("page_no"),
        "source_page_ref": page.get("source_page_ref") if page else None,
        "raw_text_sha256": sha256_text(page["raw_text"]) if page else (sha256_text(unit["raw_text"]) if unit else None),
        "clean_text_sha256": page.get("clean_text_sha256") if page else (sha256_text(unit["clean_text"]) if unit else None),
        "unit_page_start": unit.get("page_start") if unit else None,
        "unit_page_end": unit.get("page_end") if unit else None,
        "source_artifacts": artifacts,
        "original_finding_evidence": evidence,
        "remediation_method": REMEDIATION_METHODS[finding["finding_type"]],
        "preservation_rule": "保留原始文本、清洗文本、页码/单元映射、原始文件哈希、修复前后哈希和人工验收决定；禁止静默覆盖。",
        "generated_at": generated_at,
    }


def missing_number_items(conn: sqlite3.Connection, existing_ids: set[str], generated_at: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select d.*,
               coalesce(q.finding_id,'finding-synthetic-missing-' || d.document_id) finding_id,
               coalesce(q.unit_id,null) unit_id,
               coalesce(q.severity,'warning') severity,
               'missing_standard_number' finding_type,
               coalesce(q.evidence_json,'{}') evidence_json
        from documents d
        left join quality_findings q on q.document_id=d.document_id and q.finding_type='missing_standard_number'
        where d.corpus='technical_standards' and (d.standard_no is null or trim(d.standard_no)='')
        order by d.title
        """
    ).fetchall()
    return [make_item(conn, dict(row), generated_at) for row in rows if row["finding_id"] not in existing_ids]


def write_reports(report_dir: Path, items: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "t021_quality_remediation_manifest.json"
    csv_path = report_dir / "t021_quality_remediation_manifest.csv"
    md_path = report_dir / "t021_quality_remediation_audit.md"
    json_path.write_text(json.dumps({"summary": summary, "items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_fields = [
        "priority",
        "decision",
        "finding_id",
        "finding_type",
        "severity",
        "document_id",
        "standard_no",
        "title",
        "effective_status",
        "review_status",
        "can_answer",
        "unit_id",
        "page_id",
        "page_no",
        "source_page_ref",
        "raw_text_sha256",
        "clean_text_sha256",
        "unit_page_start",
        "unit_page_end",
        "artifact_paths",
        "artifact_sha256s",
        "remediation_method",
        "preservation_rule",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields)
        writer.writeheader()
        for item in items:
            row = {key: item.get(key) for key in csv_fields}
            row["artifact_paths"] = " | ".join(artifact["path"] for artifact in item["source_artifacts"])
            row["artifact_sha256s"] = " | ".join(artifact.get("sha256") or "" for artifact in item["source_artifacts"])
            writer.writerow(row)
    priority_counts = Counter(item["priority"] for item in items)
    type_counts = Counter(item["finding_type"] for item in items)
    decision_counts = Counter(item["decision"] for item in items)
    p0_documents = sorted({f"`{item['standard_no'] or 'NO-CODE'}` {item['title']}" for item in items if item["priority"] == "P0"})
    lines = [
        "# T021 v4 来源质量修复审计",
        "",
        f"- 生成时间：`{summary['generated_at']}`",
        f"- 修复清单项：{len(items)}",
        f"- 优先级：{dict(priority_counts)}",
        f"- 问题类型：{dict(type_counts)}",
        f"- 决策状态：{dict(decision_counts)}",
        f"- SQLite 完整性：`{summary['integrity_check']}`",
        f"- 正文变更：{summary['body_text_mutations']}",
        f"- FTS/向量/KG 表：{summary['retrieval_tables_present']}",
        "",
        "## P0 文档",
        "",
    ]
    lines.extend(f"- {value}" for value in p0_documents)
    lines.extend(
        [
            "",
            "## 执行规则",
            "",
            "- P0：空页、结构化附件解析基准或身份/状态冲突，进入任何检索实验前处理或明确排除。",
            "- P1：现行文档低置信度 OCR 页和超大结构单元，按原件受控重识别或确定性重切分。",
            "- P2：废止文档低置信度问题及已明确母标准关系的无编号修改单，仅保全或排期，不进入正向证据。",
            "- 本任务未更改 `pages`、`content_units` 正文，未构建 FTS、向量或知识图谱。",
            "- 任何后续修复必须保存原文本、页码映射、源文件哈希、修复前后哈希和审核决定。",
        ]
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the metadata-only T021 v4 quality remediation manifest.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    generated_at = utc_now()
    uri = f"file:{args.db.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        raw_rows = conn.execute(
            """
            select q.*,d.standard_no,d.title,d.document_type,d.effective_status,d.review_status,d.can_answer,d.source_metadata_json
            from quality_findings q join documents d using(document_id)
            where q.finding_type in ({})
            order by q.finding_type,d.title,q.finding_id
            """.format(",".join("?" for _ in QUALITY_TYPES)),
            tuple(sorted(QUALITY_TYPES)),
        ).fetchall()
        items = [make_item(conn, dict(row), generated_at) for row in raw_rows]
        existing_ids = {item["finding_id"] for item in items}
        items.extend(missing_number_items(conn, existing_ids, generated_at))
        items.sort(
            key=lambda item: (
                item["priority_order"],
                0 if item["effective_status"] == "current" else 1,
                item["finding_type"],
                item["standard_no"] or "",
                item["title"],
                item["page_no"] if item["page_no"] is not None else -1,
            )
        )
        integrity = conn.execute("pragma integrity_check").fetchone()[0]
        tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
        retrieval_tables = sorted(tables & {"chunks_fts", "chunk_vectors", "chunk_embeddings", "kg_entities", "kg_relations"})
        type_counts = Counter(item["finding_type"] for item in items)
        missing_hashes = sum(
            1
            for item in items
            if item["finding_type"] in {"low_ocr_confidence", "empty_source_page", "oversized_structural_unit"}
            and (not item["raw_text_sha256"] or not item["clean_text_sha256"])
        )
        missing_artifacts = sum(1 for item in items if not item["source_artifacts"])
    summary = {
        "task": "T021",
        "generated_at": generated_at,
        "database": str(args.db.resolve()),
        "manifest_items": len(items),
        "priority_counts": dict(Counter(item["priority"] for item in items)),
        "finding_type_counts": dict(type_counts),
        "decision_counts": dict(Counter(item["decision"] for item in items)),
        "documents_affected": len({item["document_id"] for item in items}),
        "missing_trace_hashes": missing_hashes,
        "items_without_source_artifacts": missing_artifacts,
        "integrity_check": integrity,
        "body_text_mutations": 0,
        "retrieval_tables_present": retrieval_tables,
    }
    write_reports(args.root / "reports", items, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    expected = {
        "low_ocr_confidence": 151,
        "empty_source_page": 11,
        "missing_standard_number": 9,
        "oversized_structural_unit": 3,
        "attachment_requires_parser_benchmark": 1,
    }
    count_errors = {key: (type_counts.get(key, 0), value) for key, value in expected.items() if type_counts.get(key, 0) != value}
    if integrity != "ok" or retrieval_tables or missing_hashes or missing_artifacts or count_errors:
        if count_errors:
            print(json.dumps({"count_errors": count_errors}, ensure_ascii=False, indent=2))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
