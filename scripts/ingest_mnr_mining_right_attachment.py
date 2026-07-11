from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.kb_build_utils import stable_id  # noqa: E402
from mining_qa.knowledge_store import DEFAULT_DB_PATH, connect, utc_now  # noqa: E402


PARENT_DOCUMENT_ID = "policy-d4869b5b5bf8804f"
PARENT_STANDARD_NO = "自然资规〔2023〕4号"
PARENT_TITLE = "自然资源部关于进一步完善矿产资源勘查开采登记管理的通知"
PARENT_URL = "https://f.mnr.gov.cn/202305/t20230512_2786192.html"
ATTACHMENT_URL = "https://f.mnr.gov.cn/202305/P020230512660474974800.doc"
DEFAULT_SOURCE = (
    PROJECT_ROOT
    / "data"
    / "knowledge_base"
    / "raw"
    / "mnr_policy"
    / "attachments"
    / PARENT_DOCUMENT_ID
    / "采矿权申请资料清单及要求.doc.doc"
)
KB_ROOT = PROJECT_ROOT / "data" / "knowledge_base"
PROCESSED_DIR = KB_ROOT / "processed" / "mnr_policy_attachments" / PARENT_DOCUMENT_ID
MANIFEST_DIR = KB_ROOT / "manifests"
LOG_DIR = KB_ROOT / "logs"
SOURCE_PLATFORM = "自然资源部政策法规库附件"

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W_NS}

EXPECTED_HEADERS = [
    "序号",
    "材料名称",
    "新立",
    "延续",
    "注销",
    "扩大矿区范围",
    "缩小矿区范围",
    "开采主矿种、开采方式",
    "采矿权人名称",
    "转让",
    "要求",
]

APPLICATION_VIEWS = (
    {"key": "new_establishment", "top_key": "new_establishment", "section": "新立", "column": 2},
    {"key": "extension", "top_key": "extension", "section": "延续", "column": 3},
    {"key": "cancellation", "top_key": "cancellation", "section": "注销", "column": 4},
    {"key": "change_expand", "top_key": "change", "section": "变更 > 扩大矿区范围", "column": 5},
    {"key": "change_reduce", "top_key": "change", "section": "变更 > 缩小矿区范围", "column": 6},
    {
        "key": "change_mineral_method",
        "top_key": "change",
        "section": "变更 > 开采主矿种、开采方式",
        "column": 7,
    },
    {"key": "change_holder_name", "top_key": "change", "section": "变更 > 采矿权人名称", "column": 8},
    {"key": "change_transfer", "top_key": "change", "section": "变更 > 转让", "column": 9},
)

TOP_LEVEL_SECTIONS = (
    ("new_establishment", "新立"),
    ("extension", "延续"),
    ("change", "变更"),
    ("cancellation", "注销"),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def clean_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\u3000", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def paragraph_text(paragraph: ET.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        name = local_name(node)
        if name == "t":
            parts.append(node.text or "")
        elif name == "tab":
            parts.append("\t")
        elif name in {"br", "cr"}:
            parts.append("\n")
    return clean_text("".join(parts))


def cell_text(cell: ET.Element) -> str:
    paragraphs = [paragraph_text(item) for item in cell.findall(".//w:p", NS)]
    return clean_text("\n".join(item for item in paragraphs if item))


def attr_value(element: ET.Element | None, name: str) -> str | None:
    if element is None:
        return None
    return element.attrib.get(f"{{{W_NS}}}{name}")


def parse_ooxml_table(table: ET.Element, table_index: int) -> dict[str, Any]:
    grid_width = len(table.findall("./w:tblGrid/w:gridCol", NS))
    active_vertical: dict[int, str] = {}
    rows: list[list[str]] = []
    merge_events: list[dict[str, Any]] = []

    for row_index, row in enumerate(table.findall("./w:tr", NS)):
        values: list[str] = []
        next_vertical: dict[int, str] = {}
        column = 0
        for cell in row.findall("./w:tc", NS):
            properties = cell.find("./w:tcPr", NS)
            span_node = properties.find("./w:gridSpan", NS) if properties is not None else None
            span = int(attr_value(span_node, "val") or 1)
            merge_node = properties.find("./w:vMerge", NS) if properties is not None else None
            merge_state = attr_value(merge_node, "val") if merge_node is not None else None
            if merge_node is not None and not merge_state:
                merge_state = "continue"
            raw_text = cell_text(cell)

            if span > 1 or merge_node is not None:
                merge_events.append(
                    {
                        "table_index": table_index,
                        "row": row_index,
                        "column": column,
                        "column_span": span,
                        "vertical_merge": merge_state,
                        "text": raw_text,
                    }
                )

            for offset in range(span):
                grid_column = column + offset
                if merge_state == "restart":
                    value = raw_text
                    next_vertical[grid_column] = raw_text
                elif merge_state == "continue":
                    value = active_vertical.get(grid_column, raw_text)
                    next_vertical[grid_column] = value
                else:
                    value = raw_text
                values.append(value)
            column += span

        if grid_width and len(values) != grid_width:
            raise ValueError(
                f"Table {table_index} row {row_index} expanded to {len(values)} cells; expected {grid_width}"
            )
        rows.append(values)
        active_vertical = next_vertical

    return {"grid_width": grid_width, "rows": rows, "merge_events": merge_events}


def parse_docx(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    body = root.find(".//w:body", NS)
    if body is None:
        raise ValueError("Converted DOCX has no document body")

    paragraphs: list[str] = []
    tables: list[dict[str, Any]] = []
    for child in body:
        name = local_name(child)
        if name == "p":
            text = paragraph_text(child)
            if text:
                paragraphs.append(text)
        elif name == "tbl":
            tables.append(parse_ooxml_table(child, len(tables)))
    return {"paragraphs": paragraphs, "tables": tables}


def soffice_version(binary: str) -> str:
    result = subprocess.run([binary, "--version"], check=True, capture_output=True, text=True)
    return clean_text(result.stdout or result.stderr)


def convert_legacy_doc(source: Path, target: Path) -> dict[str, str]:
    binary = shutil.which("libreoffice") or shutil.which("soffice")
    if not binary:
        raise RuntimeError("LibreOffice/soffice is required to convert the legacy DOC attachment")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="t018_soffice_") as temp_name:
        temp_dir = Path(temp_name)
        profile_uri = (temp_dir / "profile").resolve().as_uri()
        command = [
            binary,
            "--headless",
            f"-env:UserInstallation={profile_uri}",
            "--convert-to",
            "docx",
            "--outdir",
            str(temp_dir),
            str(source),
        ]
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        candidates = sorted(temp_dir.glob("*.docx"))
        if len(candidates) != 1:
            raise RuntimeError(f"Legacy DOC conversion produced {len(candidates)} DOCX files: {result.stdout}")
        shutil.copy2(candidates[0], target)
    return {"binary": binary, "version": soffice_version(binary), "target": str(target)}


def logical_source(source: Path, converted: Path) -> dict[str, Any]:
    parsed = parse_docx(converted)
    paragraphs = parsed["paragraphs"]
    physical_tables = parsed["tables"]
    if len(physical_tables) != 2:
        raise ValueError(f"Expected 2 physical Word tables, found {len(physical_tables)}")
    first_rows = physical_tables[0]["rows"]
    second_rows = physical_tables[1]["rows"]
    if len(first_rows) < 3:
        raise ValueError("First physical table is missing its two-row header or data")
    if first_rows[1] != EXPECTED_HEADERS:
        raise ValueError(f"Unexpected expanded table headers: {first_rows[1]}")

    raw_rows = [*first_rows[2:], *second_rows]
    if len(raw_rows) != 21:
        raise ValueError(f"Expected 21 logical material rows, found {len(raw_rows)}")
    rows: list[dict[str, Any]] = []
    for logical_index, values in enumerate(raw_rows, 1):
        if len(values) != len(EXPECTED_HEADERS):
            raise ValueError(f"Material row {logical_index} has {len(values)} cells")
        try:
            sequence = int(values[0])
        except ValueError as exc:
            raise ValueError(f"Invalid material sequence at logical row {logical_index}: {values[0]!r}") from exc
        if sequence != logical_index:
            raise ValueError(f"Material sequence discontinuity: expected {logical_index}, found {sequence}")
        markers = values[2:10]
        if any(marker not in {"▲", "—"} for marker in markers):
            raise ValueError(f"Unexpected required marker in row {sequence}: {markers}")
        rows.append(
            {
                "sequence": sequence,
                "material_name": clean_text(values[1]),
                "markers": {
                    view["key"]: values[int(view["column"])]
                    for view in APPLICATION_VIEWS
                },
                "requirement": clean_text(values[10]),
                "source_location": {
                    "physical_table": 1 if logical_index <= len(first_rows[2:]) else 2,
                    "logical_row": logical_index,
                },
            }
        )

    title = next((item for item in paragraphs if "采矿权申请资料清单及要求" in item), "")
    if title != "采矿权申请资料清单及要求":
        raise ValueError(f"Unexpected attachment title: {title!r}")
    notes = [item for item in paragraphs if item.startswith("注：") or re.match(r"^[23]\.\s*", item)]
    if len(notes) != 3:
        raise ValueError(f"Expected 3 global submission notes, found {len(notes)}")

    views = []
    for spec in APPLICATION_VIEWS:
        view_rows = []
        for row in rows:
            marker = row["markers"][str(spec["key"])]
            view_rows.append(
                {
                    "sequence": row["sequence"],
                    "material_name": row["material_name"],
                    "marker": marker,
                    "required": marker == "▲",
                    "requirement": row["requirement"],
                    "source_location": row["source_location"],
                }
            )
        views.append({**spec, "rows": view_rows})

    return {
        "source_path": str(source),
        "source_sha256": sha256_file(source),
        "converted_path": str(converted),
        "converted_sha256": sha256_file(converted),
        "title": title,
        "paragraphs": paragraphs,
        "global_notes": notes,
        "physical_table_count": len(physical_tables),
        "logical_table_count": 1,
        "logical_row_count": len(rows),
        "merge_events": [event for table in physical_tables for event in table["merge_events"]],
        "headers": EXPECTED_HEADERS,
        "rows": rows,
        "views": views,
    }


def normalize_material(value: str) -> str:
    return re.sub(r"[\s（）()、，,。；;：:\-—_/]", "", value or "").upper()


def material_names_from_table(table_json: str | None) -> list[str]:
    if not table_json:
        return []
    try:
        table = json.loads(table_json)
    except json.JSONDecodeError:
        return []
    names = []
    for row in table.get("rows") or []:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            clean_key = re.sub(r"\s+|\*", "", str(key))
            if "材料名称" in clean_key or "提交材料名称" in clean_key:
                text = clean_text(str(value or ""))
                if text:
                    names.append(text)
                break
    return names


def guide_application_key(title: str) -> str | None:
    if "首次" in title:
        return "new_establishment"
    if "续期" in title or "延续" in title:
        return "extension"
    if "注销" in title:
        return "cancellation"
    if "扩大开采区域范围" in title:
        return "change_expand"
    if "缩小开采区域范围" in title:
        return "change_reduce"
    if "开采矿种" in title or "开采方式" in title:
        return "change_mineral_method"
    if "采矿权人名称" in title:
        return "change_holder_name"
    if "转移" in title or "转让" in title:
        return "change_transfer"
    return None


def build_guide_comparison(conn: sqlite3.Connection, source: dict[str, Any]) -> list[dict[str, Any]]:
    views = {str(view["key"]): view for view in source["views"]}
    guides = conn.execute(
        """
        select document_id, title, official_url, bibliographic_json
        from documents
        where document_type = 'service_guide'
          and (title like '%采矿权%' or title like '%采矿许可%')
        order by title
        """
    ).fetchall()
    comparison = []
    for guide in guides:
        application_key = guide_application_key(guide["title"] or "")
        if not application_key or application_key not in views:
            continue
        table_row = conn.execute(
            """
            select table_json from chunks
            where document_id = ? and parse_method = 'structured_service_guide_gfm_table'
            limit 1
            """,
            (guide["document_id"],),
        ).fetchone()
        guide_materials = material_names_from_table(table_row["table_json"] if table_row else None)
        attachment_materials = [
            row["material_name"] for row in views[application_key]["rows"] if row["required"]
        ]
        guide_by_norm = {normalize_material(item): item for item in guide_materials}
        attachment_by_norm = {normalize_material(item): item for item in attachment_materials}
        common_keys = sorted(set(guide_by_norm) & set(attachment_by_norm))
        bibliography = json.loads(guide["bibliographic_json"] or "{}")
        comparison.append(
            {
                "guide_document_id": guide["document_id"],
                "guide_title": guide["title"],
                "guide_url": guide["official_url"],
                "guide_url_date": bibliography.get("url_date"),
                "attachment_parent_publish_date": "2023年05月06日",
                "application_key": application_key,
                "application_section": views[application_key]["section"],
                "attachment_material_count": len(attachment_materials),
                "guide_material_count": len(guide_materials),
                "exact_normalized_overlap": [guide_by_norm[key] for key in common_keys],
                "attachment_only": [
                    attachment_by_norm[key] for key in sorted(set(attachment_by_norm) - set(guide_by_norm))
                ],
                "guide_only": [guide_by_norm[key] for key in sorted(set(guide_by_norm) - set(attachment_by_norm))],
                "relationship_scope": "supporting_source_only",
                "conflict_policy": "preserve_both_by_scope_and_date",
            }
        )
    if len(comparison) != 17:
        raise ValueError(f"Expected 17 matching service guides, found {len(comparison)}")
    return comparison


def table_text(view: dict[str, Any], global_notes: list[str]) -> str:
    lines = [f"采矿权{view['section']}申请资料表", "序号 | 材料名称 | 表中标记 | 要求"]
    for row in view["rows"]:
        requirement = row["requirement"].replace("\n", "；")
        lines.append(
            f"{row['sequence']} | {row['material_name']} | {row['marker']} | {requirement}"
        )
    lines.extend(f"全局说明：{note}" for note in global_notes)
    return "\n".join(lines)


def row_evidence_text(view: dict[str, Any], row: dict[str, Any], global_notes: list[str]) -> str:
    lines = [
        f"采矿权{view['section']}申请材料第{row['sequence']}项：{row['material_name']}。",
        "表中标记：▲，属于必须提交的资料；“要求”栏有特殊规定的，从其规定。",
    ]
    if row["requirement"]:
        lines.append(f"要求：{row['requirement']}")
    if row["sequence"] == 1:
        lines.append(
            "提交形式：采矿权申请登记书或申请书通过远程申报系统提交电子文档和纸质文档扫描件各一份，且内容相互一致。"
        )
    else:
        lines.append("提交形式：其他申请资料提交纸质文档扫描件。")
    lines.append("盖章规则：凡涉及申请人盖章，必须与矿业权人名称一致。")
    return "\n".join(lines)


def remove_existing_attachment(conn: sqlite3.Connection, document_id: str) -> None:
    old_chunks = [row[0] for row in conn.execute("select chunk_id from chunks where document_id = ?", (document_id,))]
    entity_ids: list[str] = []
    if conn.execute("select 1 from sqlite_master where type='table' and name='kg_entities'").fetchone():
        placeholders = ",".join("?" for _ in old_chunks)
        if old_chunks:
            entity_ids.extend(
                row[0]
                for row in conn.execute(
                    f"select entity_id from kg_entities where source_id = ? or source_id in ({placeholders})",
                    [document_id, *old_chunks],
                )
            )
        else:
            entity_ids.extend(
                row[0] for row in conn.execute("select entity_id from kg_entities where source_id = ?", (document_id,))
            )
    if conn.execute("select 1 from sqlite_master where type='table' and name='kg_relations'").fetchone():
        if old_chunks:
            placeholders = ",".join("?" for _ in old_chunks)
            conn.execute(f"delete from kg_relations where evidence_chunk_id in ({placeholders})", old_chunks)
        if entity_ids:
            placeholders = ",".join("?" for _ in entity_ids)
            conn.execute(
                f"delete from kg_relations where source_entity_id in ({placeholders}) or target_entity_id in ({placeholders})",
                [*entity_ids, *entity_ids],
            )
            conn.execute(f"delete from kg_entities where entity_id in ({placeholders})", entity_ids)
    for table_name in ("chunk_vectors", "chunk_embeddings"):
        if conn.execute("select 1 from sqlite_master where type='table' and name=?", (table_name,)).fetchone() and old_chunks:
            placeholders = ",".join("?" for _ in old_chunks)
            conn.execute(f"delete from {table_name} where chunk_id in ({placeholders})", old_chunks)
    conn.execute("delete from chunks_fts where document_id = ?", (document_id,))
    conn.execute("delete from chunks where document_id = ?", (document_id,))
    conn.execute("delete from documents where document_id = ?", (document_id,))


def insert_attachment(
    conn: sqlite3.Connection,
    source: dict[str, Any],
    conversion: dict[str, str],
    comparison: list[dict[str, Any]],
    now: str,
) -> dict[str, Any]:
    document_id = stable_id("mnr_policy_attachment", ATTACHMENT_URL, prefix="attachment")
    remove_existing_attachment(conn, document_id)
    guide_links = [
        {
            "document_id": item["guide_document_id"],
            "title": item["guide_title"],
            "source_url": item["guide_url"],
            "url_date": item["guide_url_date"],
            "application_key": item["application_key"],
            "application_section": item["application_section"],
            "relationship_scope": item["relationship_scope"],
            "conflict_policy": item["conflict_policy"],
        }
        for item in comparison
    ]
    source_trace = {
        "source_url": ATTACHMENT_URL,
        "parent_document_id": PARENT_DOCUMENT_ID,
        "parent_standard_no": PARENT_STANDARD_NO,
        "parent_title": PARENT_TITLE,
        "parent_url": PARENT_URL,
        "raw_doc": str(Path(source["source_path"]).relative_to(PROJECT_ROOT)),
        "converted_docx": str(Path(source["converted_path"]).relative_to(PROJECT_ROOT)),
        "parser": "LibreOffice legacy DOC conversion + direct OOXML table parser",
        "converter": conversion,
        "service_guide_links": guide_links,
    }
    bibliography = {
        "title": source["title"],
        "standard_no": f"{PARENT_STANDARD_NO}附件4",
        "attachment_no": "附件4",
        "parent_publish_date": "2023年05月06日",
        "application_types": ["新立", "延续", "变更", "注销"],
        "change_subtypes": [view["section"].split(" > ", 1)[1] for view in source["views"] if view["top_key"] == "change"],
    }
    quality = {
        "source_sha256": source["source_sha256"],
        "converted_sha256": source["converted_sha256"],
        "physical_table_count": source["physical_table_count"],
        "logical_table_count": source["logical_table_count"],
        "logical_row_count": source["logical_row_count"],
        "merge_event_count": len(source["merge_events"]),
        "unreadable_field_count": 0,
        "ambiguous_merge_count": 0,
        "parser_repairs": 0,
    }

    chunks: list[tuple[Any, ...]] = []
    fts_rows: list[tuple[Any, ...]] = []

    def add_chunk(
        chunk_id: str,
        chunk_type: str,
        section_path: str,
        text: str,
        table_data: dict[str, Any] | None,
        parse_method: str,
    ) -> None:
        row = (
            chunk_id,
            document_id,
            chunk_type,
            source["title"],
            f"{PARENT_STANDARD_NO}附件4",
            section_path,
            None,
            None,
            None,
            None,
            None,
            text,
            json.dumps(table_data, ensure_ascii=False) if table_data is not None else None,
            "official_fulltext",
            "pdf_text",
            parse_method,
            1.0,
            "structured_source",
            "internal",
            ATTACHMENT_URL,
            now,
        )
        chunks.append(row)
        fts_rows.append((chunk_id, document_id, source["title"], f"{PARENT_STANDARD_NO}附件4", section_path, text))

    overview_id = stable_id(document_id, "overview", prefix="chunk")
    overview_text = (
        "自然资规〔2023〕4号附件4《采矿权申请资料清单及要求》将采矿权申请资料分为新立、延续、变更、注销4种类型。"
        "变更类型细分为扩大矿区范围、缩小矿区范围、开采主矿种或开采方式、采矿权人名称、转让。"
    )
    add_chunk(
        overview_id,
        "attachment_overview",
        "附件4 > 适用类型",
        overview_text,
        {
            "application_types": ["新立", "延续", "变更", "注销"],
            "change_subtypes": [view["section"] for view in source["views"] if view["top_key"] == "change"],
            "global_notes": source["global_notes"],
            "merged_cells": source["merge_events"],
        },
        "structured_legacy_doc_overview",
    )

    views_by_top: dict[str, list[dict[str, Any]]] = {}
    for view in source["views"]:
        views_by_top.setdefault(str(view["top_key"]), []).append(view)
    for top_key, display in TOP_LEVEL_SECTIONS:
        top_views = views_by_top[top_key]
        required_count = sum(sum(row["required"] for row in view["rows"]) for view in top_views)
        if top_key == "change":
            summary = f"采矿权变更申请包含5个子类型，各子类型材料相互独立；合计{required_count}个带▲的类型化材料项。"
        else:
            summary = f"采矿权{display}申请表中共有{required_count}项带▲材料；要求栏的特殊规定优先于表中标记。"
        add_chunk(
            stable_id(document_id, "section", top_key, prefix="chunk"),
            "application_material_section",
            f"附件4 > {display}",
            summary + "\n" + "\n".join(source["global_notes"]),
            {"top_key": top_key, "views": [view["key"] for view in top_views]},
            "structured_legacy_doc_application_section",
        )

    required_manifest = []
    for view in source["views"]:
        table_json = {
            "caption": f"采矿权{view['section']}申请资料表",
            "headers": ["序号", "材料名称", "表中标记", "是否必须提交", "要求"],
            "rows": view["rows"],
            "matrix": [
                ["序号", "材料名称", "表中标记", "是否必须提交", "要求"],
                *[
                    [
                        row["sequence"],
                        row["material_name"],
                        row["marker"],
                        "是" if row["required"] else "否",
                        row["requirement"],
                    ]
                    for row in view["rows"]
                ],
            ],
            "application_key": view["key"],
            "top_key": view["top_key"],
            "section_path": view["section"],
            "global_notes": source["global_notes"],
            "marker_legend": {"▲": "必须提交，特殊规定优先", "—": "无须提交，特殊规定优先"},
        }
        add_chunk(
            stable_id(document_id, "table", view["key"], prefix="chunk"),
            "table",
            f"附件4 > {view['section']} > 申请资料表",
            table_text(view, source["global_notes"]),
            table_json,
            "structured_legacy_doc_application_table",
        )
        for row in view["rows"]:
            if not row["required"]:
                continue
            row_table = {
                "application_key": view["key"],
                "top_key": view["top_key"],
                "section_path": view["section"],
                "sequence": row["sequence"],
                "material_name": row["material_name"],
                "marker": row["marker"],
                "required": True,
                "requirement": row["requirement"],
                "source_location": row["source_location"],
                "global_notes": source["global_notes"],
            }
            chunk_id = stable_id(document_id, "material", view["key"], row["sequence"], prefix="chunk")
            add_chunk(
                chunk_id,
                "application_material_row",
                f"附件4 > {view['section']} > 材料 {row['sequence']}",
                row_evidence_text(view, row, source["global_notes"]),
                row_table,
                "structured_legacy_doc_material_row",
            )
            required_manifest.append(
                {
                    "chunk_id": chunk_id,
                    "application_key": view["key"],
                    "application_section": view["section"],
                    "sequence": row["sequence"],
                    "material_name": row["material_name"],
                    "requirement": row["requirement"],
                    "official_url": ATTACHMENT_URL,
                }
            )

    conn.execute(
        """
        insert into documents (
          document_id,title,standard_no,document_type,status,source_type,text_access,
          validation_status,visibility,review_status,publish_date,implementation_date,
          ingestion_time,updated_at,source_priority,source_trace_json,bibliographic_json,
          quality_json,page_count,chunk_count,table_count,can_answer,official_url,source_platform
        ) values (?, ?, ?, 'policy_attachment', '现行有效', 'official_fulltext', 'pdf_text',
          'structured_source', 'internal', 'approved_for_service', '2023年05月06日', null,
          ?, ?, 160, ?, ?, ?, 0, ?, ?, 1, ?, ?)
        """,
        (
            document_id,
            source["title"],
            f"{PARENT_STANDARD_NO}附件4",
            now,
            now,
            json.dumps(source_trace, ensure_ascii=False),
            json.dumps(bibliography, ensure_ascii=False),
            json.dumps(quality, ensure_ascii=False),
            len(chunks),
            len(source["views"]),
            ATTACHMENT_URL,
            SOURCE_PLATFORM,
        ),
    )
    conn.executemany(
        """
        insert into chunks (
          chunk_id,document_id,chunk_type,title,standard_no,section_path,clause_no,
          page_start,page_end,char_start,char_end,text,table_json,source_type,text_access,
          parse_method,confidence,validation_status,visibility,source_ref,created_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        chunks,
    )
    conn.executemany(
        "insert into chunks_fts(chunk_id,document_id,title,standard_no,section_path,text) values (?, ?, ?, ?, ?, ?)",
        fts_rows,
    )
    return {
        "document_id": document_id,
        "chunk_count": len(chunks),
        "fts_count": len(fts_rows),
        "table_count": len(source["views"]),
        "required_row_count": len(required_manifest),
        "required_manifest": required_manifest,
        "guide_link_count": len(guide_links),
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_manifests(result: dict[str, Any], comparison: list[dict[str, Any]]) -> dict[str, str]:
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    material_json = MANIFEST_DIR / "mnr_mining_right_attachment_materials.json"
    material_csv = MANIFEST_DIR / "mnr_mining_right_attachment_materials.csv"
    comparison_json = MANIFEST_DIR / "t018_service_guide_comparison.json"
    comparison_md = MANIFEST_DIR / "t018_service_guide_comparison.md"
    write_json(material_json, result["required_manifest"])
    with material_csv.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(result["required_manifest"][0].keys()))
        writer.writeheader()
        writer.writerows(result["required_manifest"])
    write_json(comparison_json, comparison)
    markdown_lines = [
        "# T018 附件4与办事指南材料对照",
        "",
        "- 附件基准：自然资规〔2023〕4号附件4，父政策发布日期为 2023年05月06日。",
        "- 对照范围：17 份采矿权/采矿许可办事指南，页面 URL 日期均为 2025-07-29。",
        "- 处理原则：两类来源按日期和适用范围并存，仅建立支持关系，不合并或覆盖材料行。",
        "- 下表采用规范化后的材料名称做精确对照；逐项 `attachment_only` / `guide_only` 内容见同名 JSON。",
        "",
        "| 办事指南 | 映射类型 | 附件必交项 | 指南材料项 | 精确重合 | 附件独有 | 指南独有 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in comparison:
        markdown_lines.append(
            "| {guide} | {section} | {attachment} | {guide_count} | {overlap} | {attachment_only} | {guide_only} |".format(
                guide=str(item["guide_title"]).replace("|", "\\|"),
                section=str(item["application_section"]).replace("|", "\\|"),
                attachment=item["attachment_material_count"],
                guide_count=item["guide_material_count"],
                overlap=len(item["exact_normalized_overlap"]),
                attachment_only=len(item["attachment_only"]),
                guide_only=len(item["guide_only"]),
            )
        )
    comparison_md.write_text("\n".join(markdown_lines) + "\n", encoding="utf-8")
    return {
        "material_manifest_json": str(material_json),
        "material_manifest_csv": str(material_csv),
        "service_guide_comparison_json": str(comparison_json),
        "service_guide_comparison_md": str(comparison_md),
    }


def database_validation(db_path: Path, require_indexes: bool = False) -> dict[str, Any]:
    document_id = stable_id("mnr_policy_attachment", ATTACHMENT_URL, prefix="attachment")
    with connect(db_path) as conn:
        doc = conn.execute("select * from documents where document_id = ?", (document_id,)).fetchone()
        chunk_count = conn.execute("select count(*) from chunks where document_id = ?", (document_id,)).fetchone()[0]
        section_count = conn.execute(
            "select count(*) from chunks where document_id = ? and chunk_type = 'application_material_section'",
            (document_id,),
        ).fetchone()[0]
        table_count = conn.execute(
            "select count(*) from chunks where document_id = ? and parse_method = 'structured_legacy_doc_application_table'",
            (document_id,),
        ).fetchone()[0]
        row_count = conn.execute(
            "select count(*) from chunks where document_id = ? and chunk_type = 'application_material_row'",
            (document_id,),
        ).fetchone()[0]
        extension_rows = conn.execute(
            """
            select table_json from chunks
            where document_id = ? and chunk_type = 'application_material_row'
              and section_path like '附件4 > 延续 >%'
            order by section_path
            """,
            (document_id,),
        ).fetchall()
        extension_sequences = sorted(json.loads(row[0])["sequence"] for row in extension_rows)
        fts_count = conn.execute("select count(*) from chunks_fts where document_id = ?", (document_id,)).fetchone()[0]
        vector_count = conn.execute(
            "select count(*) from chunk_vectors where chunk_id in (select chunk_id from chunks where document_id = ?)",
            (document_id,),
        ).fetchone()[0]
        embedding_count = conn.execute(
            "select count(*) from chunk_embeddings where chunk_id in (select chunk_id from chunks where document_id = ?)",
            (document_id,),
        ).fetchone()[0]
        attachment_entity_count = conn.execute(
            "select count(*) from kg_entities where entity_type = 'Attachment' and source_id = ?",
            (document_id,),
        ).fetchone()[0]
        relation_counts = {
            row[0]: row[1]
            for row in conn.execute(
                """
                select relation_type, count(*) from kg_relations
                where source_entity_id in (select entity_id from kg_entities where source_id = ?)
                group by relation_type
                """,
                (document_id,),
            )
        }
        integrity = conn.execute("pragma integrity_check").fetchone()[0]
        guide_links = []
        if doc:
            trace = json.loads(doc["source_trace_json"] or "{}")
            guide_links = trace.get("service_guide_links") or []
        result = {
            "document_count": int(doc is not None),
            "chunk_count": chunk_count,
            "section_count": section_count,
            "table_count": table_count,
            "required_row_count": row_count,
            "extension_required_row_count": len(extension_rows),
            "extension_sequences": extension_sequences,
            "fts_count": fts_count,
            "local_vector_count": vector_count,
            "dense_embedding_count": embedding_count,
            "attachment_entity_count": attachment_entity_count,
            "kg_relation_counts": relation_counts,
            "service_guide_link_count": len(guide_links),
            "integrity_check": integrity,
        }
        result["ok"] = (
            result["document_count"] == 1
            and result["chunk_count"] == 93
            and result["section_count"] == 4
            and result["table_count"] == 8
            and result["required_row_count"] == 80
            and result["extension_sequences"] == [1, 2, 3, 4, 5, 7, 18, 19, 20, 21]
            and result["fts_count"] == 93
            and result["service_guide_link_count"] == 17
            and result["integrity_check"] == "ok"
            and (
                not require_indexes
                or (
                    result["local_vector_count"] == 93
                    and result["dense_embedding_count"] == 93
                    and result["attachment_entity_count"] == 1
                    and result["kg_relation_counts"].get("ATTACHMENT_OF") == 1
                    and result["kg_relation_counts"].get("IMPLEMENTS_MATERIAL_LIST_FOR") == 1
                    and result["kg_relation_counts"].get("SUPPORTS_GUIDE") == 17
                    and result["kg_relation_counts"].get("REQUIRES_MATERIAL") == 80
                )
            )
        )
        return result


def source_summary(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_sha256": source["source_sha256"],
        "converted_sha256": source["converted_sha256"],
        "physical_table_count": source["physical_table_count"],
        "logical_table_count": source["logical_table_count"],
        "logical_row_count": source["logical_row_count"],
        "merge_event_count": len(source["merge_events"]),
        "global_note_count": len(source["global_notes"]),
        "application_views": [
            {
                "key": view["key"],
                "section": view["section"],
                "required_row_count": sum(row["required"] for row in view["rows"]),
            }
            for view in source["views"]
        ],
        "required_row_count": sum(
            sum(row["required"] for row in view["rows"]) for view in source["views"]
        ),
        "unreadable_field_count": 0,
        "ambiguous_merge_count": 0,
        "parser_repairs": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest MNR mining-right application material attachment 4.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--require-indexes", action="store_true")
    args = parser.parse_args()

    if args.validate:
        result = database_validation(args.db, require_indexes=args.require_indexes)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    if not args.source.exists():
        raise FileNotFoundError(args.source)
    if args.apply:
        converted = PROCESSED_DIR / "采矿权申请资料清单及要求.docx"
        conversion = convert_legacy_doc(args.source, converted)
        source = logical_source(args.source, converted)
        with connect(args.db) as conn:
            comparison = build_guide_comparison(conn, source)
            result = insert_attachment(conn, source, conversion, comparison, utc_now())
        manifests = write_manifests(result, comparison)
        validation = database_validation(args.db)
        summary = {
            "task": "T018",
            "mode": "applied",
            "source": str(args.source),
            "converted": str(converted),
            "conversion": conversion,
            **source_summary(source),
            "document_id": result["document_id"],
            "chunk_count": result["chunk_count"],
            "fts_count": result["fts_count"],
            "table_count": result["table_count"],
            "required_material_row_count": result["required_row_count"],
            "service_guide_link_count": result["guide_link_count"],
            **manifests,
            "validation": validation,
            "cloud_sync_required": True,
        }
        summary_path = LOG_DIR / "t018_mining_right_attachment_ingest_summary.json"
        write_json(summary_path, summary)
        summary["summary_path"] = str(summary_path)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if validation["ok"] else 1

    with tempfile.TemporaryDirectory(prefix="t018_dry_run_") as temp_name:
        converted = Path(temp_name) / "采矿权申请资料清单及要求.docx"
        conversion = convert_legacy_doc(args.source, converted)
        source = logical_source(args.source, converted)
        with connect(args.db) as conn:
            comparison = build_guide_comparison(conn, source)
        summary = {
            "task": "T018",
            "mode": "dry_run",
            "source": str(args.source),
            "conversion": conversion,
            **source_summary(source),
            "service_guide_link_count": len(comparison),
            "service_guide_date_scope_differences": sum(
                bool(item["attachment_only"] or item["guide_only"]) for item in comparison
            ),
            "cloud_sync_required": True,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
