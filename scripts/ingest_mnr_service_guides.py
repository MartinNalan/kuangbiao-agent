from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.kb_build_utils import stable_id  # noqa: E402
from mining_qa.knowledge_store import DEFAULT_DB_PATH, connect, utc_now  # noqa: E402


DEFAULT_SOURCE_DIR = Path("/home/nalanmading/下载/2. geowiki")
KB_ROOT = PROJECT_ROOT / "data" / "knowledge_base"
RAW_DIR = KB_ROOT / "raw" / "mnr_service_guides"
MANIFEST_DIR = KB_ROOT / "manifests"
LOG_DIR = KB_ROOT / "logs"
SOURCE_PLATFORM = "自然资源部政务服务办事指南"

OFFICIAL_SECTIONS = (
    "适用范围",
    "项目信息",
    "事项审查类型",
    "审批依据",
    "受理机构",
    "决定机构",
    "审批数量",
    "申请条件",
    "申请材料",
    "申请材料提交",
    "办理基本流程",
    "办理方式",
    "办结时限",
    "收费依据及标准",
    "审批结果",
    "结果送达",
    "申请人权利和义务",
    "咨询途径",
    "监督投诉渠道",
    "办公地址和时间",
    "公开查询",
    "申请材料示范文本",
    "办理流程图",
)
AUXILIARY_SECTIONS = {"官方附件", "链接校正记录"}
EMPTY_MARKER = "原网页未提供内容"


def scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value.startswith(('"', "'")):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value.strip('"\'')
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("Markdown file is missing YAML front matter")
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as exc:
        raise ValueError("Markdown front matter is not terminated") from exc
    metadata: dict[str, Any] = {}
    active_list: str | None = None
    for line in lines[1:end]:
        list_match = re.match(r"^\s+-\s+(.*)$", line)
        if list_match and active_list:
            metadata.setdefault(active_list, []).append(scalar(list_match.group(1)))
            continue
        match = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", line)
        if not match:
            continue
        key, raw_value = match.groups()
        if raw_value.strip():
            metadata[key] = scalar(raw_value)
            active_list = None
        else:
            metadata[key] = []
            active_list = key
    return metadata, "\n".join(lines[end + 1 :]).strip()


def extract_sections(body: str) -> tuple[str, list[dict[str, Any]]]:
    lines = body.splitlines()
    title = ""
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in lines:
        title_match = re.match(r"^#\s+(.+?)\s*$", line)
        if title_match and not title:
            title = title_match.group(1).strip()
            continue
        section_match = re.match(r"^##\s+(.+?)\s*$", line)
        if section_match:
            if current:
                current["raw"] = "\n".join(current.pop("lines")).strip()
                sections.append(current)
            current = {"name": section_match.group(1).strip(), "lines": []}
            continue
        if current is not None:
            current["lines"].append(line)
    if current:
        current["raw"] = "\n".join(current.pop("lines")).strip()
        sections.append(current)
    return title, sections


def markdown_links(text: str) -> list[dict[str, str]]:
    links = []
    pattern = re.compile(r"!?\[([^\]]*)\]\((?:<([^>]+)>|([^\s)]+))\)")
    for match in pattern.finditer(text):
        links.append({"label": match.group(1).strip(), "url": (match.group(2) or match.group(3) or "").strip()})
    deduped = []
    seen = set()
    for link in links:
        key = (link["label"], link["url"])
        if link["url"] and key not in seen:
            deduped.append(link)
            seen.add(key)
    return deduped


def clean_markdown(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(
        r"!\[([^\]]*)\]\((?:<([^>]+)>|([^\s)]+))\)",
        lambda match: f"{match.group(1) or '图片'}：{match.group(2) or match.group(3)}",
        text,
    )
    text = re.sub(
        r"\[([^\]]+)\]\((?:<([^>]+)>|([^\s)]+))\)",
        lambda match: f"{match.group(1)}（{match.group(2) or match.group(3)}）",
        text,
    )
    text = re.sub(r"(?m)^>\s?", "", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"(?m)^\s*[-*]\s+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_gfm_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    cells = re.split(r"(?<!\\)\|", stripped)
    return [clean_markdown(cell.replace("\\|", "|")).strip() for cell in cells]


def parse_material_table(raw: str) -> tuple[dict[str, Any], str]:
    lines = raw.splitlines()
    table_indices = [index for index, line in enumerate(lines) if line.strip().startswith("|")]
    if len(table_indices) < 3:
        raise ValueError("Application-material section does not contain a GFM table")
    start, end = min(table_indices), max(table_indices)
    table_lines = [line for line in lines[start : end + 1] if line.strip().startswith("|")]
    headers = split_gfm_row(table_lines[0])
    separator = split_gfm_row(table_lines[1])
    if len(headers) != len(separator) or not all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in separator):
        raise ValueError("Application-material table separator is invalid")
    rows = [split_gfm_row(line) for line in table_lines[2:]]
    if any(len(row) != len(headers) for row in rows):
        raise ValueError("Application-material table has inconsistent column counts")
    row_objects = [{headers[index]: value for index, value in enumerate(row)} for row in rows]
    non_table_lines = lines[:start] + lines[end + 1 :]
    surrounding = clean_markdown("\n".join(non_table_lines))
    notes = [line.strip() for line in surrounding.splitlines() if line.strip() and "申请材料目录" not in line]
    table = {
        "caption": "申请材料目录",
        "headers": headers,
        "rows": row_objects,
        "matrix": [headers, *rows],
        "notes": notes,
        "links": markdown_links(raw),
    }
    return table, surrounding


def table_search_text(table: dict[str, Any]) -> str:
    lines = [str(table.get("caption") or "申请材料目录")]
    headers = table.get("headers") or []
    for row in table.get("rows") or []:
        values = [f"{header}：{row.get(header, '')}" for header in headers if row.get(header, "")]
        lines.append("；".join(values))
    for note in table.get("notes") or []:
        lines.append(f"说明：{note}")
    return "\n".join(lines).strip()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_guide(path: Path) -> dict[str, Any]:
    source_text = path.read_text(encoding="utf-8")
    metadata, body = parse_frontmatter(source_text)
    title, parsed_sections = extract_sections(body)
    if not title:
        raise ValueError(f"Missing document title in {path}")
    if metadata.get("title") and metadata["title"] != title:
        raise ValueError(f"YAML/body title mismatch in {path}")
    by_name: dict[str, dict[str, Any]] = {}
    for section in parsed_sections:
        if section["name"] in by_name:
            raise ValueError(f"Duplicate section {section['name']} in {path}")
        by_name[section["name"]] = section
    missing = [name for name in OFFICIAL_SECTIONS if name not in by_name]
    if missing:
        raise ValueError(f"Missing official sections in {path}: {missing}")
    official_order = [section["name"] for section in parsed_sections if section["name"] in OFFICIAL_SECTIONS]
    if official_order != list(OFFICIAL_SECTIONS):
        raise ValueError(f"Official section order mismatch in {path}")
    declared_count = int(metadata.get("section_count") or 0)
    if declared_count != len(OFFICIAL_SECTIONS):
        raise ValueError(f"Declared section count mismatch in {path}: {declared_count}")

    table, application_surrounding = parse_material_table(by_name["申请材料"]["raw"])
    sections = []
    for index, name in enumerate(OFFICIAL_SECTIONS, 1):
        raw = by_name[name]["raw"]
        text = application_surrounding if name == "申请材料" else clean_markdown(raw)
        empty = EMPTY_MARKER in text and clean_markdown(text.replace(EMPTY_MARKER, "").replace("。", "")) == ""
        sections.append(
            {
                "order": index,
                "name": name,
                "text": EMPTY_MARKER if empty else text,
                "empty": empty,
                "links": markdown_links(raw),
            }
        )

    attachment_section = by_name.get("官方附件", {"raw": ""})["raw"]
    correction_section = by_name.get("链接校正记录", {"raw": ""})["raw"]
    attachments = markdown_links(attachment_section)
    flowcharts = [link for link in markdown_links(by_name["办理流程图"]["raw"]) if re.search(r"\.(png|jpg|jpeg)(?:$|\?)", link["url"], re.I)]
    flowcharts_by_url = {link["url"]: link for link in flowcharts}
    declared_attachments = int(metadata.get("attachment_count") or 0)
    if declared_attachments != len(attachments):
        raise ValueError(f"Attachment count mismatch in {path}: {declared_attachments} != {len(attachments)}")
    if len(flowcharts_by_url) != 1:
        raise ValueError(f"Expected one flowchart URL in {path}, found {len(flowcharts_by_url)}")
    declared_hash = str(metadata.get("normalized_content_sha256") or "")
    return {
        "path": path,
        "filename": path.name,
        "source_text": source_text,
        "source_sha256": sha256_text(source_text),
        "declared_content_sha256": declared_hash,
        "metadata": metadata,
        "title": title,
        "sections": sections,
        "table": table,
        "attachments": attachments,
        "flowcharts": list(flowcharts_by_url.values()),
        "correction_records": clean_markdown(correction_section),
    }


def source_files(source_dir: Path) -> tuple[list[Path], list[Path]]:
    accepted = []
    rejected = []
    for path in sorted(source_dir.glob("*.md")):
        if re.match(r"^(?:0[1-9]|[1-3][0-9]|40)_.+\.md$", path.name):
            accepted.append(path)
        else:
            rejected.append(path)
    return accepted, rejected


def validate_source(source_dir: Path) -> dict[str, Any]:
    accepted, rejected = source_files(source_dir)
    if len(accepted) != 40:
        raise ValueError(f"Expected 40 numbered service-guide files, found {len(accepted)}")
    guides = [parse_guide(path) for path in accepted]
    source_urls = [guide["metadata"].get("source_url") for guide in guides]
    page_ids = [str(guide["metadata"].get("source_page_id") or "") for guide in guides]
    if len(set(source_urls)) != 40 or not all(source_urls):
        raise ValueError("Source URLs are missing or duplicated")
    if len(set(page_ids)) != 40 or not all(page_ids):
        raise ValueError("Source page IDs are missing or duplicated")
    totals = {
        "source_file_count": len(accepted) + len(rejected),
        "accepted_file_count": len(accepted),
        "rejected_file_count": len(rejected),
        "official_section_count": sum(len(guide["sections"]) for guide in guides),
        "empty_section_count": sum(section["empty"] for guide in guides for section in guide["sections"]),
        "table_count": len(guides),
        "attachment_count": sum(len(guide["attachments"]) for guide in guides),
        "flowchart_count": sum(len(guide["flowcharts"]) for guide in guides),
        "corrected_link_count": sum(int(guide["metadata"].get("corrected_link_count") or 0) for guide in guides),
        "corrected_text_url_count": sum(int(guide["metadata"].get("corrected_text_url_count") or 0) for guide in guides),
        "metadata_parser_repairs": 0,
        "duplicate_source_url_count": len(source_urls) - len(set(source_urls)),
        "duplicate_source_page_id_count": len(page_ids) - len(set(page_ids)),
    }
    return {"guides": guides, "rejected": rejected, "totals": totals}


def remove_existing_guide(conn: sqlite3.Connection, document_id: str) -> None:
    old_chunks = [row[0] for row in conn.execute("select chunk_id from chunks where document_id = ?", (document_id,))]
    if old_chunks:
        conn.execute("create temp table if not exists old_guide_chunks(chunk_id text primary key)")
        conn.execute("delete from old_guide_chunks")
        conn.executemany("insert into old_guide_chunks values (?)", [(chunk_id,) for chunk_id in old_chunks])
        conn.execute("delete from kg_relations where evidence_chunk_id in (select chunk_id from old_guide_chunks)")
        conn.execute(
            """
            delete from kg_entities
            where source_id = ? or source_id in (select chunk_id from old_guide_chunks)
            """,
            (document_id,),
        )
        conn.execute("delete from chunk_vectors where chunk_id in (select chunk_id from old_guide_chunks)")
        conn.execute("delete from chunk_embeddings where chunk_id in (select chunk_id from old_guide_chunks)")
    conn.execute("delete from chunks_fts where document_id = ?", (document_id,))
    conn.execute("delete from chunks where document_id = ?", (document_id,))
    conn.execute("delete from documents where document_id = ?", (document_id,))


def insert_guide(conn: sqlite3.Connection, guide: dict[str, Any], raw_path: Path, now: str) -> dict[str, Any]:
    metadata = guide["metadata"]
    identity = str(metadata.get("source_page_id") or metadata["source_url"])
    document_id = stable_id("mnr_service_guide", identity, prefix="guide")
    remove_existing_guide(conn, document_id)
    source_trace = {
        "source_url": metadata["source_url"],
        "source_page_id": str(metadata.get("source_page_id") or ""),
        "source_site": SOURCE_PLATFORM,
        "category": metadata.get("category"),
        "catalog_url": metadata.get("catalog_url"),
        "online_service_url": metadata.get("online_service_url"),
        "url_date": metadata.get("url_date"),
        "retrieved_at": metadata.get("retrieved_at"),
        "raw_markdown": str(raw_path.relative_to(PROJECT_ROOT)),
        "attachments": guide["attachments"],
        "flowcharts": guide["flowcharts"],
        "link_correction_records": guide["correction_records"],
    }
    quality = {
        "source_sha256": guide["source_sha256"],
        "declared_normalized_content_sha256": guide["declared_content_sha256"],
        "section_count": len(guide["sections"]),
        "empty_section_count": sum(section["empty"] for section in guide["sections"]),
        "table_count": 1,
        "attachment_count": len(guide["attachments"]),
        "flowchart_count": len(guide["flowcharts"]),
        "corrected_link_count": int(metadata.get("corrected_link_count") or 0),
        "corrected_text_url_count": int(metadata.get("corrected_text_url_count") or 0),
    }
    chunk_count = len(guide["sections"]) + 1
    conn.execute(
        """
        insert into documents (
          document_id,title,standard_no,document_type,status,source_type,text_access,
          validation_status,visibility,review_status,publish_date,implementation_date,
          ingestion_time,updated_at,source_priority,source_trace_json,bibliographic_json,
          quality_json,page_count,chunk_count,table_count,can_answer,official_url,source_platform
        ) values (?, ?, null, 'service_guide', 'current', 'official_fulltext', 'html_text',
          'structured_source', 'internal', 'approved_for_service', null, null,
          ?, ?, 140, ?, ?, ?, 0, ?, 1, 1, ?, ?)
        """,
        (
            document_id,
            guide["title"],
            now,
            now,
            json.dumps(source_trace, ensure_ascii=False),
            json.dumps(metadata, ensure_ascii=False),
            json.dumps(quality, ensure_ascii=False),
            chunk_count,
            metadata["source_url"],
            SOURCE_PLATFORM,
        ),
    )

    chunk_rows = []
    fts_rows = []
    for section in guide["sections"]:
        chunk_id = stable_id(document_id, "section", section["order"], section["name"], prefix="chunk")
        validation_status = "empty_source_section" if section["empty"] else "structured_source"
        row = (
            chunk_id,
            document_id,
            "service_guide_section",
            guide["title"],
            None,
            section["name"],
            None,
            None,
            None,
            None,
            None,
            section["text"],
            None,
            "official_fulltext",
            "html_text",
            "structured_service_guide_section",
            1.0,
            validation_status,
            "internal",
            metadata["source_url"],
            now,
        )
        chunk_rows.append(row)
        if not section["empty"]:
            fts_rows.append((chunk_id, document_id, guide["title"], None, section["name"], section["text"]))

    table = guide["table"]
    table_chunk_id = stable_id(document_id, "table", "申请材料", prefix="chunk")
    table_text = table_search_text(table)
    table_row = (
        table_chunk_id,
        document_id,
        "table",
        guide["title"],
        None,
        "申请材料 > 申请材料目录",
        None,
        None,
        None,
        None,
        None,
        table_text,
        json.dumps(table, ensure_ascii=False),
        "official_fulltext",
        "html_text",
        "structured_service_guide_gfm_table",
        1.0,
        "structured_source",
        "internal",
        metadata["source_url"],
        now,
    )
    chunk_rows.append(table_row)
    fts_rows.append((table_chunk_id, document_id, guide["title"], None, table_row[5], table_text))
    conn.executemany(
        """
        insert into chunks (
          chunk_id,document_id,chunk_type,title,standard_no,section_path,clause_no,
          page_start,page_end,char_start,char_end,text,table_json,source_type,text_access,
          parse_method,confidence,validation_status,visibility,source_ref,created_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        chunk_rows,
    )
    conn.executemany(
        "insert into chunks_fts(chunk_id,document_id,title,standard_no,section_path,text) values (?, ?, ?, ?, ?, ?)",
        fts_rows,
    )
    return {
        "document_id": document_id,
        "title": guide["title"],
        "source_url": metadata["source_url"],
        "source_page_id": str(metadata.get("source_page_id") or ""),
        "url_date": metadata.get("url_date"),
        "publish_date": None,
        "source_file": str(guide["path"]),
        "raw_markdown": str(raw_path.relative_to(PROJECT_ROOT)),
        "section_count": len(guide["sections"]),
        "empty_section_count": sum(section["empty"] for section in guide["sections"]),
        "answerable_section_count": sum(not section["empty"] for section in guide["sections"]),
        "table_count": 1,
        "table_row_count": len(table["rows"]),
        "attachment_count": len(guide["attachments"]),
        "flowchart_count": len(guide["flowcharts"]),
        "corrected_link_count": int(metadata.get("corrected_link_count") or 0),
        "corrected_text_url_count": int(metadata.get("corrected_text_url_count") or 0),
        "chunk_count": len(chunk_rows),
        "fts_count": len(fts_rows),
    }


def write_manifest(rows: list[dict[str, Any]]) -> tuple[Path, Path]:
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    json_path = MANIFEST_DIR / "mnr_service_guide_manifest.json"
    csv_path = MANIFEST_DIR / "mnr_service_guide_manifest.csv"
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if rows:
        with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    return json_path, csv_path


def database_validation(db_path: Path, require_indexes: bool = False) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        docs = conn.execute(
            "select * from documents where document_type = 'service_guide' and source_platform = ?",
            (SOURCE_PLATFORM,),
        ).fetchall()
        document_ids = [row["document_id"] for row in docs]
        source_urls = [row["official_url"] for row in docs]
        page_ids = []
        for row in docs:
            trace = json.loads(row["source_trace_json"] or "{}")
            page_ids.append(str(trace.get("source_page_id") or ""))
        section_count = conn.execute(
            "select count(*) from chunks where chunk_type = 'service_guide_section' and document_id in (select document_id from documents where document_type = 'service_guide')"
        ).fetchone()[0]
        table_count = conn.execute(
            "select count(*) from chunks where chunk_type = 'table' and parse_method = 'structured_service_guide_gfm_table'"
        ).fetchone()[0]
        empty_count = conn.execute(
            "select count(*) from chunks where chunk_type = 'service_guide_section' and validation_status = 'empty_source_section'"
        ).fetchone()[0]
        fts_count = conn.execute(
            "select count(*) from chunks_fts where document_id in (select document_id from documents where document_type = 'service_guide')"
        ).fetchone()[0]
        vector_count = conn.execute(
            "select count(*) from chunk_vectors where chunk_id in (select chunk_id from chunks where document_id in (select document_id from documents where document_type = 'service_guide'))"
        ).fetchone()[0]
        embedding_count = conn.execute(
            "select count(*) from chunk_embeddings where chunk_id in (select chunk_id from chunks where document_id in (select document_id from documents where document_type = 'service_guide'))"
        ).fetchone()[0]
        kg_entity_count = conn.execute(
            """
            select count(*) from kg_entities
            where source_id in (select document_id from documents where document_type = 'service_guide')
               or source_id in (select chunk_id from chunks where document_id in (select document_id from documents where document_type = 'service_guide'))
            """
        ).fetchone()[0]
        kg_relation_count = conn.execute(
            """
            select count(*) from kg_relations
            where evidence_chunk_id in (select chunk_id from chunks where document_id in (select document_id from documents where document_type = 'service_guide'))
            """
        ).fetchone()[0]
        attachment_count = 0
        flowchart_count = 0
        publish_date_count = 0
        for row in docs:
            trace = json.loads(row["source_trace_json"] or "{}")
            attachment_count += len(trace.get("attachments") or [])
            flowchart_count += len(trace.get("flowcharts") or [])
            publish_date_count += int(bool(row["publish_date"]))
        expected_indexed = section_count - empty_count + table_count
        integrity = conn.execute("pragma integrity_check").fetchone()[0]
        result = {
            "document_count": len(docs),
            "section_count": section_count,
            "empty_section_count": empty_count,
            "table_count": table_count,
            "attachment_count": attachment_count,
            "flowchart_count": flowchart_count,
            "fts_count": fts_count,
            "expected_answerable_index_count": expected_indexed,
            "local_vector_count": vector_count,
            "dense_embedding_count": embedding_count,
            "kg_source_entity_count": kg_entity_count,
            "kg_evidence_relation_count": kg_relation_count,
            "duplicate_source_url_count": len(source_urls) - len(set(source_urls)),
            "duplicate_source_page_id_count": len(page_ids) - len(set(page_ids)),
            "unexpected_publish_date_count": publish_date_count,
            "integrity_check": integrity,
        }
        result["ok"] = (
            result["document_count"] == 40
            and result["section_count"] == 920
            and result["table_count"] == 40
            and result["attachment_count"] == 46
            and result["flowchart_count"] == 40
            and result["fts_count"] == expected_indexed
            and result["duplicate_source_url_count"] == 0
            and result["duplicate_source_page_id_count"] == 0
            and result["unexpected_publish_date_count"] == 0
            and result["integrity_check"] == "ok"
            and (
                not require_indexes
                or (
                    result["local_vector_count"] == expected_indexed
                    and result["dense_embedding_count"] == expected_indexed
                    and result["kg_source_entity_count"] > 0
                    and result["kg_evidence_relation_count"] > 0
                )
            )
        )
        return result
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest structured MNR mineral-service guides.")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--require-indexes", action="store_true")
    args = parser.parse_args()

    if args.validate:
        result = database_validation(args.db, require_indexes=args.require_indexes)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    source = validate_source(args.source_dir)
    summary: dict[str, Any] = {
        "task": "T017",
        "mode": "dry_run",
        "source_dir": str(args.source_dir),
        **source["totals"],
        "rejected_files": [path.name for path in source["rejected"]],
        "expected_sections": 920,
        "expected_tables": 40,
        "expected_attachments": 46,
        "expected_flowcharts": 40,
        "cloud_sync_required": True,
    }
    if not args.apply:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    index_path = args.source_dir / "_INDEX.md"
    if index_path.exists():
        shutil.copy2(index_path, RAW_DIR / index_path.name)
    now = utc_now()
    manifest_rows = []
    with connect(args.db) as conn:
        for guide in source["guides"]:
            raw_path = RAW_DIR / guide["filename"]
            raw_path.write_text(guide["source_text"], encoding="utf-8")
            manifest_rows.append(insert_guide(conn, guide, raw_path, now))
    manifest_json, manifest_csv = write_manifest(manifest_rows)
    validation = database_validation(args.db)
    summary.update(
        {
            "mode": "applied",
            "manifest_json": str(manifest_json),
            "manifest_csv": str(manifest_csv),
            "raw_dir": str(RAW_DIR),
            "documents_inserted": len(manifest_rows),
            "section_chunks_inserted": sum(row["section_count"] for row in manifest_rows),
            "table_chunks_inserted": sum(row["table_count"] for row in manifest_rows),
            "fts_rows_inserted": sum(row["fts_count"] for row in manifest_rows),
            "validation": validation,
        }
    )
    summary_path = LOG_DIR / "mnr_service_guide_ingest_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if validation["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
