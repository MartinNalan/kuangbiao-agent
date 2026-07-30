from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.v4_source_audit import (  # noqa: E402
    compact_text,
    identity_present,
    image_metrics,
    json_dump,
    load_object,
    open_sqlite_readonly,
    pdf_info,
    physical_page_class,
    render_pdf_page,
    sha256_file,
    sha256_text,
    sqlite_snapshot,
)


DEFAULT_ROOT = PROJECT_ROOT / "data" / "knowledge_base_v4"
DEFAULT_DB = DEFAULT_ROOT / "db" / "corpus.sqlite"
DEFAULT_ORE_ROOT = Path("/home/nalanmading/My-project/ore_expert")
SOURCE_DIR = Path("standard_specification")
OCR_DIR = Path("knowledge_governance/compilation_paddleocr/pages_json")

SOURCE_SLUGS = {
    "矿产资源技术标准汇编2020上册0419.pdf": "upper",
    "矿产资源技术标准汇编2020下册0419.pdf": "lower",
}

CONFIRMED_BOUNDARY_DECISIONS = {
    "DZ/T 0208-2020": {
        "proposed_start": 355,
        "proposed_end": 387,
        "anomaly_types": ["trailing_category_divider", "trailing_blank_page"],
        "removed_pages": [388, 389],
        "decision_ref": "coordination/decisions.md#2026-07-24",
    },
    "DZ/T 0331-2020": {
        "proposed_start": 1151,
        "proposed_end": 1198,
        "anomaly_types": ["trailing_blank_page"],
        "removed_pages": [1199],
        "decision_ref": "coordination/decisions.md#2026-07-24",
    },
    "DZ/T 0347-2020": {
        "proposed_start": 575,
        "proposed_end": 583,
        "anomaly_types": ["trailing_category_divider", "trailing_blank_page"],
        "removed_pages": [584, 585],
        "decision_ref": "coordination/decisions.md#2026-07-24",
    },
}

# These catalog entries were deliberately removed before v4. They classify compilation gaps only;
# this audit does not restore or alter them.
EXCLUDED_CATALOG_RANGES = [
    ("矿产资源技术标准汇编2020上册0419.pdf", 170, 208, "GB/T 25283-2010", "矿产资源综合勘查评价规范"),
    ("矿产资源技术标准汇编2020上册0419.pdf", 209, 209, None, "《矿产资源综合勘查评价规范》国家标准第1号修改单"),
    ("矿产资源技术标准汇编2020上册0419.pdf", 358, 386, "GB 12719-91", "矿区水文地质工程地质勘探规范"),
    ("矿产资源技术标准汇编2020下册0419.pdf", 311, 354, "DZ/T 0204-2002", "稀土矿产地质勘查规范"),
    ("矿产资源技术标准汇编2020下册0419.pdf", 776, 800, "DZ/T 0326-2018", "石墨、碎云母矿产地质勘查规范"),
    ("矿产资源技术标准汇编2020下册0419.pdf", 801, 801, None, "《石墨、碎云母矿产地质勘查规范》修改单"),
    ("矿产资源技术标准汇编2020下册0419.pdf", 913, 943, "DZ/T 0291-2015", "饰面石材矿产地质勘查规范"),
    ("矿产资源技术标准汇编2020下册0419.pdf", 944, 947, None, "《饰面石材矿产地质勘查规范》修改单"),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_ocr_pages(ore_root: Path) -> dict[tuple[str, int], dict[str, Any]]:
    pages: dict[tuple[str, int], dict[str, Any]] = {}
    for path in sorted((ore_root / OCR_DIR).glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        meta = value.get("meta") or {}
        source = str(meta.get("source_file") or "")
        if source not in SOURCE_SLUGS:
            continue
        page_no = int(meta["global_page"])
        text = "\n".join(str(line.get("text") or "") for line in value.get("lines") or [])
        pages[(source, page_no)] = {
            "path": path,
            "sha256": sha256_file(path),
            "text": text,
            "text_sha256": sha256_text(text),
            "quality": value.get("quality") or {},
        }
    return pages


def load_documents(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    rows = connection.execute(
        """
        select * from documents
        where document_id like 'compilation_%'
        order by document_id
        """
    ).fetchall()
    for row in rows:
        document = dict(row)
        metadata = json.loads(document["source_metadata_json"] or "{}")
        trace = metadata.get("source_trace") or {}
        source = trace.get("source_pdf")
        if source not in SOURCE_SLUGS:
            raise ValueError(f"unexpected compilation source for {document['document_id']}: {source}")
        document["source_trace"] = trace
        document["current_start"] = int(trace["source_global_page_start"])
        document["current_end"] = int(trace["source_global_page_end"])
        document["db_pages"] = [
            dict(page)
            for page in connection.execute(
                "select * from pages where document_id=? order by page_no",
                (document["document_id"],),
            )
        ]
        documents.append(document)
    return documents


def needed_pages(documents: list[dict[str, Any]], source_page_counts: dict[str, int]) -> dict[str, set[int]]:
    result = {source: set() for source in SOURCE_SLUGS}
    for document in documents:
        source = document["source_trace"]["source_pdf"]
        maximum = source_page_counts[source]
        candidates = {
            document["current_start"] - 1,
            document["current_start"],
            document["current_end"],
            document["current_end"] + 1,
        }
        decision = CONFIRMED_BOUNDARY_DECISIONS.get(document["standard_no"])
        if decision:
            candidates.add(decision["proposed_end"])
            candidates.update(decision["removed_pages"])
        result[source].update(page for page in candidates if 1 <= page <= maximum)
    return result


def render_evidence(
    ore_root: Path,
    evidence_dir: Path,
    source_info: dict[str, dict[str, Any]],
    ocr_pages: dict[tuple[str, int], dict[str, Any]],
    page_numbers: dict[str, set[int]],
) -> dict[tuple[str, int], dict[str, Any]]:
    evidence: dict[tuple[str, int], dict[str, Any]] = {}
    for source, numbers in page_numbers.items():
        source_path = ore_root / SOURCE_DIR / source
        slug = SOURCE_SLUGS[source]
        for page_no in sorted(numbers):
            render_path = evidence_dir / "renders" / f"{slug}_p{page_no:04d}.jpg"
            render_pdf_page(source_path, page_no, render_path)
            metrics = image_metrics(render_path)
            ocr = ocr_pages.get((source, page_no))
            if ocr is None:
                raise ValueError(f"missing compilation OCR evidence: {source} page {page_no}")
            page_class = physical_page_class(ocr["text"], metrics)
            evidence[(source, page_no)] = {
                "source_pdf": source,
                "source_pdf_path": source_info[source]["path"],
                "source_pdf_sha256": source_info[source]["sha256"],
                "global_page": page_no,
                "render_path": str(render_path.resolve()),
                "render_sha256": sha256_file(render_path),
                "render_metrics": metrics,
                "ocr_json_path": str(ocr["path"].resolve()),
                "ocr_json_sha256": ocr["sha256"],
                "ocr_text_sha256": ocr["text_sha256"],
                "ocr_quality": ocr["quality"],
                "ocr_excerpt": compact_text(ocr["text"])[:400],
                "physical_class": page_class,
            }
    return evidence


def make_contact_sheets(evidence_dir: Path, page_evidence: dict[tuple[str, int], dict[str, Any]]) -> list[dict[str, Any]]:
    from PIL import Image, ImageDraw

    records: list[dict[str, Any]] = []
    ordered = sorted(page_evidence.items(), key=lambda item: (SOURCE_SLUGS[item[0][0]], item[0][1]))
    per_sheet = 20
    cell_width, cell_height = 220, 332
    for sheet_index in range(0, len(ordered), per_sheet):
        batch = ordered[sheet_index : sheet_index + per_sheet]
        canvas = Image.new("RGB", (cell_width * 5, cell_height * 4), "white")
        draw = ImageDraw.Draw(canvas)
        labels: list[str] = []
        for offset, ((source, page_no), item) in enumerate(batch):
            row, column = divmod(offset, 5)
            with Image.open(item["render_path"]) as image:
                thumb = image.convert("RGB")
                thumb.thumbnail((200, 280))
                x = column * cell_width + (cell_width - thumb.width) // 2
                y = row * cell_height + 28
                canvas.paste(thumb, (x, y))
            label = f"{SOURCE_SLUGS[source]} p{page_no:04d} {item['physical_class']}"
            labels.append(label)
            draw.text((column * cell_width + 5, row * cell_height + 5), label, fill="black")
        number = sheet_index // per_sheet + 1
        path = evidence_dir / "contact_sheets" / f"boundary_pages_{number:02d}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(path, "JPEG", quality=88)
        records.append({"path": str(path.resolve()), "sha256": sha256_file(path), "labels": labels})
    return records


def owner_for_page(
    source: str,
    page_no: int,
    proposed_ranges: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for item in proposed_ranges:
        if item["source_pdf"] == source and item["proposed_start"] <= page_no <= item["proposed_end"]:
            return {"kind": "v4_document", **item}
    for excluded_source, start, end, standard_no, title in EXCLUDED_CATALOG_RANGES:
        if excluded_source == source and start <= page_no <= end:
            return {
                "kind": "excluded_catalog_document",
                "source_pdf": source,
                "start": start,
                "end": end,
                "standard_no": standard_no,
                "title": title,
            }
    return None


def contextual_classification(
    source: str,
    page_no: int,
    page_counts: dict[str, int],
    evidence: dict[tuple[str, int], dict[str, Any]],
    proposed_ranges: list[dict[str, Any]],
) -> str:
    if page_no < 1:
        return "start_of_pdf"
    if page_no > page_counts[source]:
        return "end_of_pdf"
    item = evidence[(source, page_no)]
    physical = item["physical_class"]
    if physical in {"blank_page", "scan_artifact_only", "category_divider", "table_of_contents"}:
        return physical
    owner = owner_for_page(source, page_no, proposed_ranges)
    if owner is None:
        if page_no <= 14:
            return "compilation_front_matter"
        return "unassigned_material"
    code = owner.get("standard_no") or "NO-CODE"
    return f"{owner['kind']}:{code}:{physical}"


def document_rows(
    documents: list[dict[str, Any]],
    page_counts: dict[str, int],
    evidence: dict[tuple[str, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    preliminary: list[dict[str, Any]] = []
    for document in documents:
        decision = CONFIRMED_BOUNDARY_DECISIONS.get(document["standard_no"])
        proposed_start = int(decision["proposed_start"]) if decision else document["current_start"]
        proposed_end = int(decision["proposed_end"]) if decision else document["current_end"]
        preliminary.append(
            {
                "document_id": document["document_id"],
                "standard_no": document["standard_no"],
                "title": document["title"],
                "source_pdf": document["source_trace"]["source_pdf"],
                "current_start": document["current_start"],
                "current_end": document["current_end"],
                "proposed_start": proposed_start,
                "proposed_end": proposed_end,
            }
        )
    preliminary.sort(key=lambda row: (row["source_pdf"], row["proposed_start"], row["document_id"]))

    rows: list[dict[str, Any]] = []
    for document in documents:
        source = document["source_trace"]["source_pdf"]
        start, end = document["current_start"], document["current_end"]
        decision = CONFIRMED_BOUNDARY_DECISIONS.get(document["standard_no"])
        proposed_start = int(decision["proposed_start"]) if decision else start
        proposed_end = int(decision["proposed_end"]) if decision else end
        first = evidence[(source, start)]
        last = evidence[(source, end)]
        previous_class = contextual_classification(source, start - 1, page_counts, evidence, preliminary)
        next_class = contextual_classification(source, end + 1, page_counts, evidence, preliminary)
        first_own_identity = identity_present(first["ocr_excerpt"], document["standard_no"], document["title"])
        first_class = "own_cover_or_amendment" if first_own_identity else first["physical_class"]
        last_class = last["physical_class"]
        anomaly_types: list[str] = list(decision["anomaly_types"]) if decision else []
        result = "boundary_anomaly" if anomaly_types else "pass"

        if not first_own_identity and first["physical_class"] not in {"substantive_content", "amendment_content"}:
            result = "review_needed"
            anomaly_types.append("source_ambiguity")
        if not decision and last["physical_class"] == "blank_page":
            result = "review_needed"
            anomaly_types.append("trailing_blank_page")
        if not decision and last["physical_class"] == "category_divider":
            result = "review_needed"
            anomaly_types.append("trailing_category_divider")

        if first["render_sha256"] == evidence.get((source, start - 1), {}).get("render_sha256"):
            result = "review_needed"
            anomaly_types.append("duplicate_page")
        if last["render_sha256"] == evidence.get((source, end + 1), {}).get("render_sha256"):
            result = "review_needed"
            anomaly_types.append("duplicate_page")

        page_refs = sorted(
            {
                page
                for page in [start - 1, start, proposed_end, end, end + 1, *(decision.get("removed_pages", []) if decision else [])]
                if 1 <= page <= page_counts[source]
            }
        )
        evidence_refs = [
            {
                "global_page": page,
                "physical_class": evidence[(source, page)]["physical_class"],
                "render_path": evidence[(source, page)]["render_path"],
                "render_sha256": evidence[(source, page)]["render_sha256"],
                "ocr_json_path": evidence[(source, page)]["ocr_json_path"],
                "ocr_json_sha256": evidence[(source, page)]["ocr_json_sha256"],
                "ocr_text_sha256": evidence[(source, page)]["ocr_text_sha256"],
                "ocr_excerpt": evidence[(source, page)]["ocr_excerpt"],
            }
            for page in page_refs
        ]
        rows.append(
            {
                "document_id": document["document_id"],
                "standard_no": document["standard_no"],
                "title": document["title"],
                "source_pdf": source,
                "source_pdf_sha256": first["source_pdf_sha256"],
                "current_global_start": start,
                "current_global_end": end,
                "proposed_global_start": proposed_start,
                "proposed_global_end": proposed_end,
                "first_included_page_classification": first_class,
                "last_included_page_classification": last_class,
                "preceding_page_classification": previous_class,
                "following_page_classification": next_class,
                "result": result,
                "anomaly_types": sorted(set(anomaly_types)),
                "evidence_pages": evidence_refs,
                "decision_reference": decision.get("decision_ref") if decision else None,
                "recommended_non_mutating_disposition": (
                    f"Record corrected inclusive range {proposed_start}-{proposed_end} for the later unified T022 run; do not mutate v4 in T023."
                    if result == "boundary_anomaly"
                    else "Retain current inclusive range; no v4 mutation in T023."
                    if result == "pass"
                    else "Hold for explicit review; do not mutate v4."
                ),
            }
        )
    return sorted(rows, key=lambda row: (row["source_pdf"], row["current_global_start"], row["document_id"]))


def range_reconciliation(rows: list[dict[str, Any]], source_info: dict[str, dict[str, Any]], evidence: dict[tuple[str, int], dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for source in SOURCE_SLUGS:
        ranges = [
            (row["proposed_global_start"], row["proposed_global_end"], row["document_id"])
            for row in rows
            if row["source_pdf"] == source
        ]
        ranges.extend((start, end, f"excluded:{standard_no or title}") for item_source, start, end, standard_no, title in EXCLUDED_CATALOG_RANGES if item_source == source)
        ranges.sort()
        overlaps: list[dict[str, Any]] = []
        for left, right in zip(ranges, ranges[1:]):
            if right[0] <= left[1]:
                overlaps.append({"left": left, "right": right})
        assigned: dict[int, str] = {}
        for start, end, owner in ranges:
            for page in range(start, end + 1):
                assigned[page] = owner
        gaps: list[dict[str, Any]] = []
        page = 1
        maximum = int(source_info[source]["page_count"])
        while page <= maximum:
            if page in assigned:
                page += 1
                continue
            start = page
            classes: list[str] = []
            while page <= maximum and page not in assigned:
                if (source, page) in evidence:
                    classes.append(evidence[(source, page)]["physical_class"])
                elif page <= 14:
                    classes.append("compilation_front_matter")
                else:
                    classes.append("unrendered_unassigned")
                page += 1
            end = page - 1
            unique = sorted(set(classes))
            if start == 1 and end == 14:
                classification = "compilation_front_matter"
            elif set(unique) <= {"blank_page", "category_divider", "scan_artifact_only"}:
                classification = "+".join(unique)
            else:
                classification = "unassigned_material"
            gaps.append({"start": start, "end": end, "classification": classification, "observed_classes": unique})
        output.append(
            {
                "source_pdf": source,
                "source_pdf_sha256": source_info[source]["sha256"],
                "page_count": maximum,
                "proposed_or_excluded_ranges": [
                    {"start": start, "end": end, "owner": owner} for start, end, owner in ranges
                ],
                "overlaps": overlaps,
                "gaps": gaps,
            }
        )
    return output


def validate_visual_review(review_path: Path, contact_sheets: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not review_path.exists():
        return None
    review = load_object(review_path)
    reviewed = {item["path"]: item["sha256"] for item in review.get("reviewed_contact_sheets") or []}
    expected = {item["path"]: item["sha256"] for item in contact_sheets}
    if reviewed != expected:
        raise ValueError("T023 visual-review record does not cover the current contact-sheet hashes")
    return review


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "document_id",
        "standard_no",
        "title",
        "source_pdf",
        "source_pdf_sha256",
        "current_global_start",
        "current_global_end",
        "proposed_global_start",
        "proposed_global_end",
        "first_included_page_classification",
        "last_included_page_classification",
        "preceding_page_classification",
        "following_page_classification",
        "result",
        "anomaly_types",
        "evidence_global_pages",
        "decision_reference",
        "recommended_non_mutating_disposition",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in rows:
            row = {field: item.get(field) for field in fields}
            row["anomaly_types"] = "|".join(item["anomaly_types"])
            row["evidence_global_pages"] = "|".join(str(page["global_page"]) for page in item["evidence_pages"])
            writer.writerow(row)


def write_markdown(path: Path, manifest: dict[str, Any]) -> None:
    summary = manifest["summary"]
    lines = [
        "# T023 汇编来源标准边界审计",
        "",
        f"- 审计时间：`{summary['audited_at']}`",
        f"- 汇编来源文档：{summary['in_scope_documents']}（覆盖 {summary['coverage_documents']}/{summary['in_scope_documents']}）",
        f"- 结果：{summary['result_counts']}",
        f"- 异常类型：{summary['anomaly_counts']}",
        f"- 原 PDF：{summary['source_pdf_count']} 份；边界证据页：{summary['evidence_page_count']} 页",
        f"- 可视复核：`{summary['visual_review_complete']}`",
        f"- SQLite 完整性：`{summary['integrity_check']}`；数据库未变：`{summary['database_unchanged']}`",
        f"- 检索表计数未变：`{summary['retrieval_artifacts_unchanged']}`",
        "- 本任务仅产出私有审计证据；未修改 v4 文档、页面、单元、状态或索引。T022 仍是唯一允许应用修复的任务。",
        "",
        "## 边界异常",
        "",
    ]
    anomalies = [row for row in manifest["documents"] if row["result"] != "pass"]
    for row in anomalies:
        lines.append(
            f"- `{row['standard_no'] or 'NO-CODE'}`《{row['title']}》：当前 {row['current_global_start']}-{row['current_global_end']}，"
            f"建议 {row['proposed_global_start']}-{row['proposed_global_end']}；{', '.join(row['anomaly_types']) or row['result']}。"
        )
        lines.append(
            "  证据页："
            + "；".join(
                f"原 PDF p.{item['global_page']} ({item['physical_class']}, render `{item['render_sha256']}`, OCR `{item['ocr_json_sha256']}`)"
                for item in row["evidence_pages"]
            )
        )
    if not anomalies:
        lines.append("- 无。")
    lines.extend(["", "## 全量清单", "", "| 标准号 | 标题 | 来源页（当前→建议） | 首页 | 末页 | 前页 | 后页 | 结论 |", "| --- | --- | --- | --- | --- | --- | --- | --- |"])
    for row in manifest["documents"]:
        current = f"{row['current_global_start']}-{row['current_global_end']}"
        proposed = f"{row['proposed_global_start']}-{row['proposed_global_end']}"
        lines.append(
            f"| {row['standard_no'] or 'NO-CODE'} | {row['title']} | {current} → {proposed} | "
            f"{row['first_included_page_classification']} | {row['last_included_page_classification']} | "
            f"{row['preceding_page_classification']} | {row['following_page_classification']} | {row['result']} |"
        )
    lines.extend(["", "## 卷级范围核对", ""])
    for item in manifest["range_reconciliation"]:
        lines.append(f"### {item['source_pdf']}")
        lines.append("")
        lines.append(f"- 页数：{item['page_count']}；重叠：{len(item['overlaps'])}。")
        for gap in item["gaps"]:
            lines.append(f"- 未分配范围 {gap['start']}-{gap['end']}：`{gap['classification']}`（{gap['observed_classes']}）。")
        lines.append("")
    lines.extend(
        [
            "## 非变更声明",
            "",
            "- 数据库以 SQLite `mode=ro&immutable=1` 打开。",
            "- 审计前后数据库文件 SHA-256、字节数、逐表记录数及检索表计数完全一致。",
            "- `cloud_sync_required=false`。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit all v4 compilation-derived document boundaries without mutating v4.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--ore-root", type=Path, default=DEFAULT_ORE_ROOT)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()

    audited_at = utc_now()
    evidence_dir = args.root / "source_evidence" / "t023"
    report_dir = args.root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    before = sqlite_snapshot(args.db)
    source_info = {
        source: pdf_info(args.ore_root / SOURCE_DIR / source)
        for source in SOURCE_SLUGS
    }
    with open_sqlite_readonly(args.db) as connection:
        documents = load_documents(connection)
    if len(documents) != 73:
        raise ValueError(f"expected 73 compilation-derived v4 documents, found {len(documents)}")
    ocr_pages = load_ocr_pages(args.ore_root)
    page_counts = {source: int(info["page_count"]) for source, info in source_info.items()}
    pages = needed_pages(documents, page_counts)
    evidence = render_evidence(args.ore_root, evidence_dir, source_info, ocr_pages, pages)
    contact_sheets = make_contact_sheets(evidence_dir, evidence)
    visual_review_path = evidence_dir / "t023_visual_review.json"
    visual_review = validate_visual_review(visual_review_path, contact_sheets)
    rows = document_rows(documents, page_counts, evidence)
    reconciliation = range_reconciliation(rows, source_info, evidence)
    after = sqlite_snapshot(args.db)
    result_counts = Counter(row["result"] for row in rows)
    anomaly_counts = Counter(anomaly for row in rows for anomaly in row["anomaly_types"])
    summary = {
        "task": "T023",
        "audited_at": audited_at,
        "in_scope_documents": len(documents),
        "coverage_documents": len(rows),
        "source_pdf_count": len(source_info),
        "evidence_page_count": len(evidence),
        "contact_sheet_count": len(contact_sheets),
        "visual_review_complete": visual_review is not None,
        "result_counts": dict(result_counts),
        "anomaly_counts": dict(anomaly_counts),
        "range_overlap_count": sum(len(item["overlaps"]) for item in reconciliation),
        "unclassified_gap_count": sum(
            gap["classification"] == "unassigned_material"
            for item in reconciliation
            for gap in item["gaps"]
        ),
        "integrity_check": after["integrity_check"],
        "database_unchanged": before == after,
        "retrieval_artifacts_unchanged": before["retrieval_artifact_counts"] == after["retrieval_artifact_counts"],
        "body_text_mutations": 0,
        "cloud_sync_required": False,
    }
    manifest = {
        "summary": summary,
        "sources": source_info,
        "database_before": before,
        "database_after": after,
        "contact_sheets": contact_sheets,
        "visual_review": visual_review,
        "documents": rows,
        "range_reconciliation": reconciliation,
    }
    json_path = report_dir / "t023_compilation_boundary_audit.json"
    csv_path = report_dir / "t023_compilation_boundary_audit.csv"
    md_path = report_dir / "t023_compilation_boundary_audit.md"
    json_dump(json_path, manifest)
    json_dump(evidence_dir / "t023_page_evidence.json", {"pages": list(evidence.values())})
    write_csv(csv_path, rows)
    write_markdown(md_path, manifest)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.prepare_only:
        return 0
    failures = [
        summary["coverage_documents"] != summary["in_scope_documents"],
        summary["result_counts"].get("review_needed", 0) != 0,
        summary["range_overlap_count"] != 0,
        summary["unclassified_gap_count"] != 0,
        not summary["visual_review_complete"],
        summary["integrity_check"] != "ok",
        not summary["database_unchanged"],
        not summary["retrieval_artifacts_unchanged"],
    ]
    return 1 if any(failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
