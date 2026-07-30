#!/usr/bin/env python3
"""Build a private, reproducible v4 retrieval-preprocessing layer.

The accepted T026 ``content_units`` remain immutable provenance records.  This
script derives parent/leaf retrieval structures, supplements context for short
units, splits safely delimited long units, records cross-page stitching, and
reconstructs the three imposed two-page supplemental standards from OCR line
coordinates.  It does not create final RAG chunks, FTS, embeddings, ANN, a
knowledge graph, or any cloud artifact.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable

from mining_qa.kb_build_utils import CN_NUM, infer_clause_no, stable_id
from mining_qa.kb_rebuild import clean_display_text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "knowledge_base_v4" / "db" / "corpus.sqlite"
DEFAULT_GOLD = (
    PROJECT_ROOT
    / "data"
    / "knowledge_base_v4"
    / "evaluation"
    / "fullscan_gold_cases_v1.json"
)
DEFAULT_ORE_ROOT = Path("/home/nalanmading/My-project/ore_expert")
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "knowledge_base_v4" / "retrieval_preprocessing_v1"
SCHEMA_VERSION = "v4-retrieval-preprocessing.v1"

WIDE_LAYOUT_STANDARDS = {
    "GB/T 25283-2023",
    "DZ/T 0430-2023",
    "DZ/T 0400-2022",
}
SHORT_THRESHOLD = 20
LONG_THRESHOLD = 1200

EXACT_CLAUSE_RE = re.compile(r"^(?:\d+(?:\.\d+){1,6}|[A-Z]\.\d+(?:\.\d+)*)$")
CLAUSE_WITH_TEXT_RE = re.compile(
    r"^((?:[1-9]\d?|[A-Z])(?:\.\d+){1,6})\s*[\u4e00-\u9fff（(]"
)
TOP_SECTION_RE = re.compile(r"^([1-9]|1\d|20)\s+[\u4e00-\u9fff]")
APPENDIX_RE = re.compile(r"^附录\s*([A-ZＡ-Ｚ])")
POLICY_BOUNDARY_RE = re.compile(
    rf"^(?:[{CN_NUM}]+、|[（(][{CN_NUM}0-9]+[）)]|\d+[.、]|[a-zA-Z][）)])"
)
GENERIC_SUBITEM_RE = re.compile(
    rf"^(?:[（(][{CN_NUM}0-9]+[）)]|\d+[.、）)]|[a-zA-Z][）)])"
)
PAGE_LABEL_RE = re.compile(r"^(?:\d{1,3}|[IVXLCⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+)$", re.I)
EXPLICIT_HIERARCHICAL_BOUNDARY_RE = re.compile(
    r"^(?:\d+(?:\s*\.\s*\d+){1,6}|[A-Z]\s*\.\s*\d+(?:\s*\.\s*\d+)*)\s+",
    re.I,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def schema_snapshot(connection: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    return [
        (row[0], row[1], row[2], row[3] or "")
        for row in connection.execute(
            "select type,name,tbl_name,sql from sqlite_master order by type,name"
        )
    ]


def unit_is_explicitly_excluded(structure_json: str) -> bool:
    try:
        payload = json.loads(structure_json or "{}")
    except json.JSONDecodeError:
        return False
    false_flags = ("answer_evidence_eligible", "include_in_answering", "can_answer")
    true_flags = ("exclude_from_answering", "excluded_from_answering")
    return any(payload.get(key) is False for key in false_flags) or any(
        payload.get(key) is True for key in true_flags
    )


def compact_text(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", value or "").lower()


def source_page_refs(unit: dict[str, Any]) -> list[str]:
    structure = unit.get("structure") or {}
    refs = structure.get("source_page_refs") or []
    if not refs and unit.get("source_ref"):
        refs = [unit["source_ref"]]
    return list(dict.fromkeys(str(ref) for ref in refs if ref))


def load_source_units(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        select c.*,d.corpus,d.document_type,d.title,coalesce(d.standard_no,'') standard_no,
               d.effective_status,d.review_status,d.can_answer
        from content_units c
        join documents d using(document_id)
        where d.effective_status='current'
          and d.review_status='approved_for_service'
          and d.can_answer=1
        order by d.document_id,c.unit_order,c.unit_id
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        if unit_is_explicitly_excluded(row["structure_json"]):
            continue
        item = dict(row)
        item["structure"] = json.loads(item.pop("structure_json") or "{}")
        item["char_length"] = len(item["clean_text"])
        item["source_page_refs"] = source_page_refs(item)
        result.append(item)
    return result


def context_header(unit: dict[str, Any], local_heading: str | None = None) -> str:
    values = [
        f"文件：{unit['title']}",
        f"标准号：{unit['standard_no']}" if unit.get("standard_no") else "",
        f"章节：{unit['section_path']}" if unit.get("section_path") else "",
        f"条款：{unit['clause_no']}" if unit.get("clause_no") else "",
        f"局部层级：{local_heading}" if local_heading else "",
    ]
    return "\n".join(value for value in values if value)


def neighbor_context(
    unit: dict[str, Any],
    units_by_document: dict[str, list[dict[str, Any]]],
) -> tuple[str, list[str]]:
    siblings = units_by_document[unit["document_id"]]
    index = next(i for i, item in enumerate(siblings) if item["unit_id"] == unit["unit_id"])
    selected: list[dict[str, Any]] = []
    for offset in (-1, 1):
        candidate_index = index + offset
        if not (0 <= candidate_index < len(siblings)):
            continue
        candidate = siblings[candidate_index]
        same_section = bool(
            unit.get("section_path")
            and candidate.get("section_path") == unit.get("section_path")
        )
        same_page = bool(
            unit.get("page_start") is not None
            and candidate.get("page_start") == unit.get("page_start")
        )
        if same_section or same_page:
            selected.append(candidate)
    parts = []
    for candidate in selected:
        label = "前文" if candidate["unit_order"] < unit["unit_order"] else "后文"
        excerpt = re.sub(r"\s+", " ", candidate["clean_text"]).strip()[:180]
        parts.append(f"{label}：{excerpt}")
    return "\n".join(parts), [item["unit_id"] for item in selected]


def split_explicit_blocks(text: str, unit_type: str) -> list[dict[str, str]]:
    """Split only at visible numbered/list boundaries; never by fixed length."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return []
    boundary = POLICY_BOUNDARY_RE if unit_type == "policy_clause" else GENERIC_SUBITEM_RE
    blocks: list[dict[str, str]] = []
    current: list[str] = []
    active_heading: str | None = None

    def flush() -> None:
        nonlocal current
        if current:
            blocks.append(
                {
                    "text": "\n".join(current),
                    "local_heading": active_heading or "",
                }
            )
        current = []

    for line in lines:
        starts = bool(boundary.match(line) or EXPLICIT_HIERARCHICAL_BOUNDARY_RE.match(line))
        is_group_heading = bool(
            re.match(rf"^(?:[{CN_NUM}]+、|[（(][{CN_NUM}]+[）)])", line)
        )
        if starts and current:
            flush()
        if is_group_heading:
            active_heading = line[:160]
        current.append(line)
    flush()
    meaningful = [block for block in blocks if len(compact_text(block["text"])) >= 8]
    if len(meaningful) < 2 or len(blocks) > 50:
        return []
    reconstructed = "\n".join(block["text"] for block in blocks)
    normalized_source = "\n".join(lines)
    if reconstructed != normalized_source:
        raise ValueError("Explicit block split changed source line order")
    return blocks


def looks_like_unstructured_table(text: str) -> bool:
    """Identify OCR table/chart streams that must not be split as subclauses."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    if any(re.match(r"^(?:表|续表)\s*[A-Z0-9]", line, re.I) for line in lines):
        return True
    numeric_lines = sum(
        bool(re.fullmatch(r"[-+<≥≤~～—./0-9%°℃mMkKgGtTPa]+", line, re.I))
        for line in lines
    )
    short_lines = sum(len(compact_text(line)) <= 6 for line in lines)
    return (
        len(lines) >= 30
        and (numeric_lines / len(lines) >= 0.20 or short_lines / len(lines) >= 0.55)
    )


def looks_like_front_matter_stream(text: str) -> bool:
    """Detect long OCR streams that merge a cover, contents, or foreword."""
    compact = re.sub(r"\s+", "", text or "")
    opening = compact[:500]
    is_contents = "目录" in opening and (
        text.count("......") >= 3 or text.count("……") >= 3
    )
    is_cover_and_foreword = (
        "中华人民共和国" in opening
        and "前言" in compact[:1200]
        and ("发布" in opening or "实施" in opening)
    )
    return is_contents or is_cover_and_foreword


def table_scope(section_path: str | None) -> str:
    value = str(section_path or "").strip()
    value = re.sub(r"\s*>\s*申请资料表$", "", value)
    value = re.sub(r"\s*>\s*材料\s*\d+.*$", "", value)
    return value


def is_substantive_front_matter(unit: dict[str, Any]) -> bool:
    """Keep standalone amendment text searchable despite its legacy unit type."""
    return unit.get("unit_type") == "front_matter" and "修改单" in str(
        unit.get("title") or ""
    )


@dataclass
class VirtualLine:
    text: str
    score: float
    x1: int
    y1: int
    x2: int
    y2: int


def printed_page_label(lines: list[VirtualLine]) -> tuple[str, VirtualLine] | None:
    if not lines:
        return None
    maximum_y = max(line.y2 for line in lines)
    candidates = [
        line
        for line in lines
        if line.y1 >= maximum_y * 0.88 and PAGE_LABEL_RE.fullmatch(line.text.strip())
    ]
    if not candidates:
        return None
    line = max(candidates, key=lambda item: (item.y1, item.score))
    return line.text.strip(), line


def split_wide_page(
    payload: dict[str, Any],
    *,
    source_ref: str,
    source_pdf_page: int | None,
) -> list[dict[str, Any]]:
    raw_lines = [line for line in payload.get("lines") or [] if line.get("box")]
    if not raw_lines:
        return []
    maximum_x = max(int(line["box"][2]) for line in raw_lines)
    if maximum_x < 1800:
        return []
    middle = maximum_x / 2.0
    result: list[dict[str, Any]] = []
    for column in ("left", "right"):
        selected: list[VirtualLine] = []
        for item in raw_lines:
            x1, y1, x2, y2 = (int(value) for value in item["box"][:4])
            center = (x1 + x2) / 2.0
            if (center < middle) != (column == "left"):
                continue
            local_x1 = x1 if column == "left" else int(x1 - middle)
            local_x2 = x2 if column == "left" else int(x2 - middle)
            selected.append(
                VirtualLine(
                    text=str(item.get("text") or "").strip(),
                    score=float(item.get("score") or 0.0),
                    x1=local_x1,
                    y1=y1,
                    x2=local_x2,
                    y2=y2,
                )
            )
        label = printed_page_label(selected)
        if label is None or not label[0].isdigit():
            continue
        printed_page, footer = label
        maximum_y = max(line.y2 for line in selected)
        body: list[VirtualLine] = []
        for line in sorted(selected, key=lambda item: (item.y1, item.x1)):
            if line is footer:
                continue
            if line.y1 < maximum_y * 0.15 and re.search(
                r"(?:GB|DZ)\s*/?T?\s*\d", line.text, re.I
            ):
                continue
            if line.text:
                body.append(line)
        text = "\n".join(line.text for line in body)
        result.append(
            {
                "physical_page_no": int(printed_page),
                "source_ref": source_ref,
                "source_pdf_page": source_pdf_page,
                "column": column,
                "text": clean_display_text(text)[0],
                "text_sha256": sha256_text(clean_display_text(text)[0]),
                "average_ocr_score": (
                    sum(line.score for line in body) / len(body) if body else 0.0
                ),
                "lines": [line.__dict__ for line in body],
            }
        )
    return result


def choose_virtual_pages(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for segment in segments:
        grouped[int(segment["physical_page_no"])].append(segment)
    result: list[dict[str, Any]] = []
    for page_no, candidates in sorted(grouped.items()):
        chosen = max(
            candidates,
            key=lambda item: (len(compact_text(item["text"])), item["average_ocr_score"]),
        )
        item = {key: value for key, value in chosen.items() if key != "lines"}
        item["lines"] = chosen["lines"]
        item["duplicate_candidates"] = [
            {
                "source_ref": candidate["source_ref"],
                "source_pdf_page": candidate["source_pdf_page"],
                "column": candidate["column"],
                "text_sha256": candidate["text_sha256"],
                "average_ocr_score": candidate["average_ocr_score"],
            }
            for candidate in candidates
        ]
        item["deduplicated_candidate_count"] = len(candidates)
        result.append(item)
    return result


def virtual_heading(line: str) -> tuple[str | None, bool]:
    value = line.strip()
    if EXACT_CLAUSE_RE.fullmatch(value):
        return value, False
    match = CLAUSE_WITH_TEXT_RE.match(value)
    if match:
        return match.group(1), False
    match = TOP_SECTION_RE.match(value)
    if match:
        return match.group(1), True
    match = APPENDIX_RE.match(value)
    if match:
        return f"附录{match.group(1)}", True
    return None, False


def parse_virtual_units(
    document: dict[str, Any],
    virtual_pages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    current: list[str] = []
    current_clause: str | None = None
    current_section: str | None = None
    active_section: str | None = None
    physical_pages: list[int] = []
    source_refs: list[str] = []

    def flush() -> None:
        nonlocal current, current_clause, current_section, physical_pages, source_refs
        if not current or current_clause is None:
            current = []
            physical_pages = []
            source_refs = []
            return
        text = clean_display_text("\n".join(current))[0]
        if len(compact_text(text)) >= 8:
            units.append(
                {
                    "unit_type": "section" if current_clause.isdigit() and "." not in current_clause else "clause",
                    "section_path": current_section,
                    "clause_no": current_clause,
                    "physical_page_start": min(physical_pages),
                    "physical_page_end": max(physical_pages),
                    "source_page_refs": list(dict.fromkeys(source_refs)),
                    "citation_text": text,
                }
            )
        current = []
        current_clause = None
        current_section = None
        physical_pages = []
        source_refs = []

    for page in virtual_pages:
        for line_item in page["lines"]:
            line = clean_display_text(str(line_item["text"]))[0]
            clause_no, is_section = virtual_heading(line)
            if clause_no is not None:
                flush()
                current_clause = clause_no
                if is_section:
                    active_section = line[:160]
                    current_section = active_section
                else:
                    current_section = active_section
            if current_clause is None:
                continue
            current.append(line)
            physical_pages.append(int(page["physical_page_no"]))
            for candidate in page.get("duplicate_candidates") or []:
                ref = candidate["source_ref"]
                if ref not in source_refs:
                    source_refs.append(ref)
    flush()
    return units


def source_similarity(derived_text: str, source_text: str) -> tuple[int, float]:
    left = compact_text(derived_text)
    right = compact_text(source_text)
    if not left or not right:
        return 0, 0.0
    match = SequenceMatcher(None, left, right, autojunk=False).find_longest_match()
    coverage = match.size / min(len(left), len(right))
    return match.size, coverage


def match_virtual_sources(
    virtual: dict[str, Any],
    source_units: list[dict[str, Any]],
) -> list[str]:
    refs = set(virtual.get("source_page_refs") or [])
    candidates = [
        unit
        for unit in source_units
        if refs & set(unit.get("source_page_refs") or [])
        and unit["unit_type"] != "table"
    ]
    scored: list[tuple[int, float, str]] = []
    for unit in candidates:
        longest, coverage = source_similarity(virtual["citation_text"], unit["clean_text"])
        if longest >= 18 and coverage >= 0.22:
            scored.append((longest, coverage, unit["unit_id"]))
    scored.sort(reverse=True)
    return [unit_id for _, _, unit_id in scored]


def render_report(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    long_actions = summary["long_unit_actions"]
    stitch = summary["cross_page_stitching"]
    lines = [
        "# v4 检索结构预处理 v1",
        "",
        f"生成时间：`{payload['created_at']}`",
        "",
        f"数据库 SHA-256：`{payload['database']['sha256_before']}`",
        "",
        "## 结果",
        "",
        f"- 输入：{summary['source_document_count']} 份可答文档、{summary['source_unit_count']} 个来源单元。",
        f"- 输出：{summary['retrieval_leaf_count']} 个派生检索叶子、{summary['parent_node_count']} 个父级节点。",
        f"- 少于 {SHORT_THRESHOLD} 字的 {summary['short_source_unit_count']} 个来源单元已全部补充文件、标准号、章节及可用相邻上下文。",
        f"- 不少于 {LONG_THRESHOLD} 字的 {summary['long_source_unit_count']} 个来源单元已逐一检查：{long_actions}。",
        f"- 仍需人工复核的派生叶子共 {summary['review_required_leaf_count']} 个，均可保留作风险召回，但全部禁止直接引用；其中不少于 {LONG_THRESHOLD} 字的有 {summary['long_review_required_leaf_count']} 个。",
        f"- 跨页来源单元 {stitch['total']} 个：{stitch['status_counts']}。跨页本身不是错误，连续条款保持为一个叶子。",
        f"- 宽幅双页来源：{summary['wide_layout']['input_pdf_pages']} 个 PDF 页面拆为 {summary['wide_layout']['column_segments']} 个栏页，按印刷页码去重后得到 {summary['wide_layout']['virtual_physical_pages']} 个物理页，重建 {summary['wide_layout']['derived_units']} 个条款单元。",
        "",
        "## 结构规则",
        "",
        "- 原 `content_units` 未修改；所有结果都可由本脚本重建。",
        "- 检索叶子按完整条款、政策子项或表格行组织；父级保存文档、章节、原长单元和总表上下文。",
        "- `search_text` 包含文件名、标准号、章节路径和引用正文；`citation_text` 只保留可追溯正文或表格行。",
        "- 短单元仅补上下文，不因长度被删除；长单元只在可见编号或结构化表格行处切分。",
        "- 普通跨页条款按来源页顺序串联；三份宽幅双页标准按 OCR 坐标分栏、印刷页码排序并去重。",
        "",
        "## 重点问题",
        "",
    ]
    for item in payload["priority_previews"]:
        lines.append(
            f"- `{item['id']}`：{item['status']}；派生证据 {item['derived_evidence_count']} 条。"
        )
        for preview in item.get("previews") or []:
            lines.append(f"  - `{preview['retrieval_unit_id']}`：{preview['citation_text'][:220]}")
    lines.extend(
        [
            "",
            "## Gold 映射",
            "",
            f"- 题目数：{summary['gold_mapping']['case_count']}；必要证据组：{summary['gold_mapping']['group_count']}。",
            f"- 未解析必要证据组：{summary['gold_mapping']['unresolved_group_count']}。",
            "",
            "## 安全与完整性",
            "",
            f"- 数据库执行前哈希：`{payload['database']['sha256_before']}`。",
            f"- 数据库执行后哈希：`{payload['database']['sha256_after']}`。",
            f"- schema 前后一致：`{str(payload['database']['schema_unchanged']).lower()}`。",
            f"- integrity_check：`{payload['database']['integrity_check']}`；外键违规：{payload['database']['foreign_key_violation_count']}。",
            "- 未创建或重建 FTS、Embedding、ANN、知识图谱；`cloud_sync_required=false`。",
        ]
    )
    return "\n".join(lines) + "\n"


WIDE_SOURCE_OVERRIDES = {
    ("GB/T 25283-2023", "9.2"): [
        "unit-1f9361bb69bc120b",
        "unit-b7701628aa401bb0",
    ],
    ("GB/T 25283-2023", "9.3"): ["unit-38a6d992e966a3aa"],
    ("GB/T 25283-2023", "9.4"): ["unit-88fe3a4285c56828"],
    ("DZ/T 0430-2023", "3.1"): ["unit-20fae87b2cfa089c"],
}

WIDE_SOURCE_ALLOWED_CLAUSES = {
    "unit-b7701628aa401bb0": ("GB/T 25283-2023", "9.2"),
    "unit-38a6d992e966a3aa": ("GB/T 25283-2023", "9.3"),
    "unit-88fe3a4285c56828": ("GB/T 25283-2023", "9.4"),
    "unit-20fae87b2cfa089c": ("DZ/T 0430-2023", "3.1"),
}


class RetrievalBuilder:
    def __init__(self, source_units: list[dict[str, Any]]):
        self.source_units = source_units
        self.source_by_id = {unit["unit_id"]: unit for unit in source_units}
        self.units_by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for unit in source_units:
            self.units_by_document[unit["document_id"]].append(unit)
        self.documents = {
            unit["document_id"]: {
                key: unit[key]
                for key in (
                    "document_id",
                    "corpus",
                    "document_type",
                    "title",
                    "standard_no",
                )
            }
            for unit in source_units
        }
        self.parents: dict[str, dict[str, Any]] = {}
        self.leaves: list[dict[str, Any]] = []
        self.source_map: dict[str, dict[str, Any]] = {
            unit["unit_id"]: {
                "source_unit_id": unit["unit_id"],
                "document_id": unit["document_id"],
                "unit_type": unit["unit_type"],
                "char_length": unit["char_length"],
                "is_short": unit["char_length"] < SHORT_THRESHOLD,
                "is_long": unit["char_length"] >= LONG_THRESHOLD,
                "is_multi_page": bool(
                    unit.get("page_start") is not None
                    and unit.get("page_end") is not None
                    and int(unit["page_end"]) > int(unit["page_start"])
                ),
                "short_context_supplemented": False,
                "long_action": None,
                "processing_actions": [],
                "retrieval_unit_ids": [],
                "parent_node_ids": [],
            }
            for unit in source_units
        }
        self.table_parents: dict[tuple[str, str], str] = {}

    def document_parent(self, document_id: str) -> str:
        document = self.documents[document_id]
        parent_id = stable_id(SCHEMA_VERSION, document_id, "document_parent", prefix="rparent")
        if parent_id not in self.parents:
            text = "\n".join(
                value
                for value in (
                    document["title"],
                    document["standard_no"],
                )
                if value
            )
            self.parents[parent_id] = {
                "parent_node_id": parent_id,
                "node_type": "document_parent",
                "parent_node_id_ref": None,
                **document,
                "section_path": None,
                "source_unit_ids": [],
                "context_text": text,
                "context_text_sha256": sha256_text(text),
            }
        return parent_id

    def section_parent(self, unit: dict[str, Any], section_path: str | None = None) -> str:
        section = str(section_path if section_path is not None else unit.get("section_path") or "").strip()
        if not section:
            return self.document_parent(unit["document_id"])
        parent_id = stable_id(
            SCHEMA_VERSION,
            unit["document_id"],
            "section_parent",
            section,
            prefix="rparent",
        )
        if parent_id not in self.parents:
            document_parent = self.document_parent(unit["document_id"])
            context = "\n".join(
                value
                for value in (
                    unit["title"],
                    unit.get("standard_no") or "",
                    section,
                )
                if value
            )
            self.parents[parent_id] = {
                "parent_node_id": parent_id,
                "node_type": "section_parent",
                "parent_node_id_ref": document_parent,
                **self.documents[unit["document_id"]],
                "section_path": section,
                "source_unit_ids": [],
                "context_text": context,
                "context_text_sha256": sha256_text(context),
            }
        return parent_id

    def source_parent(
        self,
        unit: dict[str, Any],
        *,
        node_type: str,
        parent_id: str | None = None,
    ) -> str:
        parent_node_id = stable_id(
            SCHEMA_VERSION,
            unit["unit_id"],
            node_type,
            prefix="rparent",
        )
        if parent_node_id not in self.parents:
            resolved_parent = parent_id or self.section_parent(unit)
            self.parents[parent_node_id] = {
                "parent_node_id": parent_node_id,
                "node_type": node_type,
                "parent_node_id_ref": resolved_parent,
                **self.documents[unit["document_id"]],
                "section_path": unit.get("section_path"),
                "source_unit_ids": [unit["unit_id"]],
                "context_text": unit["clean_text"],
                "context_text_sha256": sha256_text(unit["clean_text"]),
            }
        mapping = self.source_map[unit["unit_id"]]
        if parent_node_id not in mapping["parent_node_ids"]:
            mapping["parent_node_ids"].append(parent_node_id)
        return parent_node_id

    def add_leaf(
        self,
        unit: dict[str, Any],
        *,
        citation_text: str,
        source_unit_ids: list[str],
        derivation_method: str,
        parent_id: str | None = None,
        local_heading: str | None = None,
        clause_no: str | None = None,
        section_path: str | None = None,
        page_start: int | None = None,
        page_end: int | None = None,
        source_refs: list[str] | None = None,
        search_eligible: bool = True,
        citation_eligible: bool = True,
        review_status: str = "pass",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        citation_text = clean_display_text(citation_text)[0]
        if review_status == "review_required":
            citation_eligible = False
        resolved_section = section_path if section_path is not None else unit.get("section_path")
        resolved_clause = clause_no if clause_no is not None else unit.get("clause_no")
        resolved_parent = parent_id or self.section_parent(unit, resolved_section)
        header = context_header(
            {**unit, "section_path": resolved_section, "clause_no": resolved_clause},
            local_heading,
        )
        neighbor_text = ""
        context_source_ids: list[str] = []
        short_sources = [
            source_id
            for source_id in source_unit_ids
            if source_id in self.source_by_id
            and self.source_by_id[source_id]["char_length"] < SHORT_THRESHOLD
        ]
        if short_sources:
            primary = self.source_by_id[short_sources[0]]
            neighbor_text, context_source_ids = neighbor_context(primary, self.units_by_document)
        context_text = "\n".join(value for value in (header, neighbor_text) if value)
        search_text = "\n".join(value for value in (context_text, citation_text) if value)
        retrieval_unit_id = stable_id(
            SCHEMA_VERSION,
            unit["document_id"],
            derivation_method,
            resolved_clause,
            citation_text,
            sorted(source_unit_ids),
            prefix="runit",
        )
        leaf = {
            "retrieval_unit_id": retrieval_unit_id,
            "schema_version": SCHEMA_VERSION,
            "node_type": "retrieval_leaf",
            "parent_node_id": resolved_parent,
            **self.documents[unit["document_id"]],
            "unit_type": unit["unit_type"],
            "section_path": resolved_section,
            "clause_no": resolved_clause,
            "page_start": page_start if page_start is not None else unit.get("page_start"),
            "page_end": page_end if page_end is not None else unit.get("page_end"),
            "citation_text": citation_text,
            "citation_text_sha256": sha256_text(citation_text),
            "context_text": context_text,
            "context_source_unit_ids": context_source_ids,
            "search_text": search_text,
            "search_text_sha256": sha256_text(search_text),
            "char_length": len(citation_text),
            "source_unit_ids": sorted(set(source_unit_ids)),
            "source_page_refs": list(
                dict.fromkeys(source_refs if source_refs is not None else unit.get("source_page_refs") or [])
            ),
            "derivation_method": derivation_method,
            "search_eligible": bool(search_eligible),
            "citation_eligible": bool(citation_eligible),
            "review_status": review_status,
            "extra": extra or {},
        }
        self.leaves.append(leaf)
        for source_id in source_unit_ids:
            if source_id not in self.source_map:
                continue
            mapping = self.source_map[source_id]
            if retrieval_unit_id not in mapping["retrieval_unit_ids"]:
                mapping["retrieval_unit_ids"].append(retrieval_unit_id)
            if derivation_method not in mapping["processing_actions"]:
                mapping["processing_actions"].append(derivation_method)
            if mapping["is_short"] and context_text:
                mapping["short_context_supplemented"] = True
        return leaf

    def create_table_parents_and_rows(self) -> None:
        application_scopes = {
            (unit["document_id"], table_scope(unit.get("section_path")))
            for unit in self.source_units
            if unit["unit_type"] == "application_material_row"
        }
        for unit in self.source_units:
            if unit["unit_type"] != "table":
                continue
            scope = table_scope(unit.get("section_path"))
            section_parent = self.section_parent(unit, scope or unit.get("section_path"))
            table_parent = self.source_parent(
                unit,
                node_type="table_parent",
                parent_id=section_parent,
            )
            self.table_parents[(unit["document_id"], scope)] = table_parent
            mapping = self.source_map[unit["unit_id"]]
            mapping["processing_actions"].append("table_parent_created")
            if (unit["document_id"], scope) in application_scopes:
                mapping["processing_actions"].append("existing_application_rows_linked")
                if mapping["is_long"]:
                    mapping["long_action"] = "table_parent_with_existing_rows"
                continue
            table = (unit.get("structure") or {}).get("table") or {}
            matrix = table.get("matrix") if isinstance(table, dict) else None
            if not isinstance(matrix, list) or len(matrix) < 2:
                self.add_leaf(
                    unit,
                    citation_text=unit["clean_text"],
                    source_unit_ids=[unit["unit_id"]],
                    derivation_method="table_without_structured_rows_retained",
                    parent_id=table_parent,
                    citation_eligible=unit["validation_status"] != "parsed_from_ocr",
                    review_status="review_required",
                )
                if mapping["is_long"]:
                    mapping["long_action"] = "long_table_retained_for_review"
                continue
            caption = str(table.get("caption") or unit.get("section_path") or "表格")
            header = [str(cell).strip() for cell in matrix[0]]
            emitted = 0
            for row_index, row in enumerate(matrix[1:], 1):
                cells = [str(cell).strip() for cell in row]
                if not any(cells):
                    continue
                width = max(len(header), len(cells))
                padded_header = header + [""] * (width - len(header))
                padded_cells = cells + [""] * (width - len(cells))
                fields = [
                    f"{name or f'列{index + 1}'}：{value}"
                    for index, (name, value) in enumerate(zip(padded_header, padded_cells))
                    if value
                ]
                row_text = "\n".join([caption, *fields])
                self.add_leaf(
                    unit,
                    citation_text=row_text,
                    source_unit_ids=[unit["unit_id"]],
                    derivation_method="structured_table_row",
                    parent_id=table_parent,
                    clause_no=f"row-{row_index}",
                    citation_eligible=unit["validation_status"] in {
                        "manually_curated_table",
                        "governed_source",
                    },
                    review_status=(
                        "pass"
                        if unit["validation_status"] in {"manually_curated_table", "governed_source"}
                        else "review_required"
                    ),
                    extra={"table_row_index": row_index, "table_header": header},
                )
                emitted += 1
            mapping["processing_actions"].append(f"structured_table_rows_emitted:{emitted}")
            if mapping["is_long"]:
                mapping["long_action"] = "split_into_structured_table_rows"

    def create_current_unit_leaves(self) -> None:
        for unit in self.source_units:
            if unit["unit_type"] == "table":
                continue
            mapping = self.source_map[unit["unit_id"]]
            if unit["standard_no"] in WIDE_LAYOUT_STANDARDS:
                self.add_leaf(
                    unit,
                    citation_text=unit["clean_text"],
                    source_unit_ids=[unit["unit_id"]],
                    derivation_method="wide_layout_legacy_fallback",
                    search_eligible=False,
                    citation_eligible=False,
                    review_status="replaced_by_layout_reconstruction",
                )
                if mapping["is_long"]:
                    mapping["long_action"] = "wide_layout_reconstructed"
                continue
            if unit["unit_type"] == "application_material_row":
                scope = table_scope(unit.get("section_path"))
                parent_id = self.table_parents.get(
                    (unit["document_id"], scope),
                    self.section_parent(unit, scope),
                )
                self.add_leaf(
                    unit,
                    citation_text=unit["clean_text"],
                    source_unit_ids=[unit["unit_id"]],
                    derivation_method="application_material_row_linked",
                    parent_id=parent_id,
                )
                if mapping["is_long"]:
                    mapping["long_action"] = "application_row_retained"
                continue
            if mapping["is_long"]:
                if unit["unit_type"] == "front_matter" and not is_substantive_front_matter(unit):
                    blocks = []
                    retain_method = "long_front_matter_context_only"
                    long_action = "front_matter_retained_as_context_only"
                elif looks_like_front_matter_stream(unit["clean_text"]):
                    blocks = []
                    retain_method = "long_ocr_front_matter_context_only"
                    long_action = "ocr_front_matter_retained_as_context_only"
                elif looks_like_unstructured_table(unit["clean_text"]):
                    blocks = []
                    retain_method = "unstructured_table_like_unit_retained"
                    long_action = "table_like_source_retained_for_review"
                else:
                    blocks = split_explicit_blocks(unit["clean_text"], unit["unit_type"])
                    retain_method = "long_atomic_unit_retained"
                    long_action = "retained_no_safe_explicit_boundary"
                if blocks:
                    parent_id = self.source_parent(unit, node_type="long_source_parent")
                    for block_index, block in enumerate(blocks, 1):
                        self.add_leaf(
                            unit,
                            citation_text=block["text"],
                            source_unit_ids=[unit["unit_id"]],
                            derivation_method="explicit_numbered_subclause_split",
                            parent_id=parent_id,
                            local_heading=block["local_heading"] or None,
                            clause_no=f"{unit.get('clause_no') or 'part'}#{block_index}",
                            citation_eligible=(
                                len(block["text"]) < LONG_THRESHOLD
                                and (
                                    unit["unit_type"] != "front_matter"
                                    or is_substantive_front_matter(unit)
                                )
                            ),
                            review_status=(
                                "pass" if len(block["text"]) < LONG_THRESHOLD else "review_required"
                            ),
                            extra={
                                "piece_index": block_index,
                                "piece_count": len(blocks),
                                "source_text_sha256": sha256_text(unit["clean_text"]),
                            },
                        )
                    mapping["long_action"] = f"split_at_explicit_boundaries:{len(blocks)}"
                else:
                    self.add_leaf(
                        unit,
                        citation_text=unit["clean_text"],
                        source_unit_ids=[unit["unit_id"]],
                        derivation_method=retain_method,
                        search_eligible=(
                            retain_method != "long_ocr_front_matter_context_only"
                            and (
                                unit["unit_type"] != "front_matter"
                                or is_substantive_front_matter(unit)
                            )
                        ),
                        citation_eligible=False,
                        review_status=(
                            "context_only"
                            if unit["unit_type"] == "front_matter"
                            or retain_method == "long_ocr_front_matter_context_only"
                            else "review_required"
                        ),
                    )
                    mapping["long_action"] = long_action
                continue
            search_eligible = (
                unit["unit_type"] != "front_matter"
                or is_substantive_front_matter(unit)
            )
            citation_eligible = search_eligible and len(compact_text(unit["clean_text"])) >= 8
            self.add_leaf(
                unit,
                citation_text=unit["clean_text"],
                source_unit_ids=[unit["unit_id"]],
                derivation_method="source_unit_with_context",
                search_eligible=search_eligible,
                citation_eligible=citation_eligible,
                review_status="pass" if citation_eligible else "context_only",
            )


def reconstruct_wide_documents(
    connection: sqlite3.Connection,
    builder: RetrievalBuilder,
    ore_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    virtual_pages_output: list[dict[str, Any]] = []
    input_pdf_pages = 0
    column_segments = 0
    derived_count = 0
    by_standard: dict[str, dict[str, int]] = {}
    for document_id, document in sorted(builder.documents.items()):
        standard_no = document["standard_no"]
        if standard_no not in WIDE_LAYOUT_STANDARDS:
            continue
        page_rows = connection.execute(
            "select page_no,source_page_ref from pages where document_id=? order by page_no",
            (document_id,),
        ).fetchall()
        input_pdf_pages += len(page_rows)
        segments: list[dict[str, Any]] = []
        for row in page_rows:
            source_ref = row["source_page_ref"]
            if not source_ref:
                continue
            path = ore_root / source_ref
            if not path.is_file():
                raise FileNotFoundError(f"Missing OCR page JSON: {path}")
            page_payload = json.loads(path.read_text(encoding="utf-8"))
            parts = split_wide_page(
                page_payload,
                source_ref=source_ref,
                source_pdf_page=row["page_no"],
            )
            segments.extend(parts)
            column_segments += len(parts)
        virtual_pages = choose_virtual_pages(segments)
        for page in virtual_pages:
            virtual_pages_output.append({"document_id": document_id, "standard_no": standard_no, **page})
        virtual_units = parse_virtual_units(document, virtual_pages)
        document_source_units = builder.units_by_document[document_id]
        for virtual in virtual_units:
            source_ids = match_virtual_sources(virtual, document_source_units)
            source_ids = [
                source_id
                for source_id in source_ids
                if source_id not in WIDE_SOURCE_ALLOWED_CLAUSES
                or WIDE_SOURCE_ALLOWED_CLAUSES[source_id]
                == (standard_no, str(virtual["clause_no"]))
            ]
            for source_id in WIDE_SOURCE_OVERRIDES.get(
                (standard_no, str(virtual["clause_no"])), []
            ):
                if source_id in builder.source_by_id and source_id not in source_ids:
                    source_ids.append(source_id)
            representative = (
                builder.source_by_id[source_ids[0]]
                if source_ids
                else {**document, "unit_type": virtual["unit_type"], "section_path": virtual["section_path"], "clause_no": virtual["clause_no"], "page_start": virtual["physical_page_start"], "page_end": virtual["physical_page_end"], "source_page_refs": virtual["source_page_refs"], "clean_text": virtual["citation_text"]}
            )
            representative = {
                **representative,
                "unit_type": virtual["unit_type"],
                "section_path": virtual["section_path"],
                "clause_no": virtual["clause_no"],
            }
            review_required = len(virtual["citation_text"]) >= LONG_THRESHOLD
            builder.add_leaf(
                representative,
                citation_text=virtual["citation_text"],
                source_unit_ids=source_ids,
                derivation_method="wide_layout_virtual_clause",
                clause_no=virtual["clause_no"],
                section_path=virtual["section_path"],
                page_start=virtual["physical_page_start"],
                page_end=virtual["physical_page_end"],
                source_refs=virtual["source_page_refs"],
                search_eligible=True,
                citation_eligible=not review_required,
                review_status="review_required" if review_required else "pass",
                extra={
                    "page_number_model": "printed_physical_page",
                    "layout": "two_page_imposition_split_by_ocr_coordinates",
                },
            )
            derived_count += 1
        by_standard[standard_no] = {
            "input_pdf_pages": len(page_rows),
            "column_segments": len(segments),
            "virtual_physical_pages": len(virtual_pages),
            "derived_units": len(virtual_units),
        }
    return virtual_pages_output, {
        "input_pdf_pages": input_pdf_pages,
        "column_segments": column_segments,
        "virtual_physical_pages": len(virtual_pages_output),
        "derived_units": derived_count,
        "by_standard": by_standard,
    }


def build_cross_page_records(
    builder: RetrievalBuilder,
    database_pages: dict[str, dict[int, dict[str, Any]]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for unit in builder.source_units:
        mapping = builder.source_map[unit["unit_id"]]
        if not mapping["is_multi_page"]:
            continue
        start = int(unit["page_start"])
        end = int(unit["page_end"])
        refs = unit.get("source_page_refs") or []
        expected = end - start + 1
        db_page_evidence = [
            database_pages.get(unit["document_id"], {}).get(page_no)
            for page_no in range(start, end + 1)
        ]
        complete_db_pages = all(db_page_evidence)
        if unit["standard_no"] in WIDE_LAYOUT_STANDARDS:
            status = "reconstructed_from_virtual_physical_pages"
        elif len(refs) == expected:
            status = "joined_with_complete_page_mapping"
        elif len(refs) >= 2:
            status = "joined_with_noncanonical_page_mapping"
        elif complete_db_pages:
            status = "joined_with_database_page_records"
        else:
            status = "joined_from_database_order_review_required"
        records.append(
            {
                "source_unit_id": unit["unit_id"],
                "document_id": unit["document_id"],
                "standard_no": unit["standard_no"],
                "clause_no": unit.get("clause_no"),
                "page_start": start,
                "page_end": end,
                "expected_page_count": expected,
                "source_page_ref_count": len(refs),
                "source_page_refs": refs,
                "database_page_evidence": [
                    item for item in db_page_evidence if item is not None
                ],
                "stitch_status": status,
                "stitched_text_sha256": sha256_text(unit["clean_text"]),
                "retrieval_unit_ids": mapping["retrieval_unit_ids"],
                "decision": "keep_semantically_continuous_unit_across_page_boundary",
            }
        )
    return records


def build_gold_mapping(
    gold_cases: list[dict[str, Any]],
    leaves: list[dict[str, Any]],
    *,
    created_at: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    eligible_leaves = [leaf for leaf in leaves if leaf["search_eligible"]]
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for leaf in eligible_leaves:
        for source_id in leaf["source_unit_ids"]:
            by_source[source_id].append(leaf)

    mapped_cases: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for case in gold_cases:
        mapped_groups: list[list[str]] = []
        for group_index, group in enumerate(case.get("required_groups") or [], 1):
            candidates: dict[str, dict[str, Any]] = {}
            for source_id in group:
                for leaf in by_source.get(source_id, []):
                    candidates[leaf["retrieval_unit_id"]] = leaf
            if case["id"] in {"Q14", "Q15"}:
                candidates = {
                    key: leaf
                    for key, leaf in candidates.items()
                    if "探矿权转采矿权" in leaf["citation_text"]
                    and "评审备案" in leaf["citation_text"]
                }
            elif case["id"] == "Q25":
                preferred = {
                    key: leaf
                    for key, leaf in candidates.items()
                    if "unit-b3dcd9212e5430fd" in leaf["source_unit_ids"]
                }
                if preferred:
                    candidates = preferred
            mapped = sorted(candidates)
            mapped_groups.append(mapped)
            if not mapped:
                unresolved.append(
                    {
                        "case_id": case["id"],
                        "group_index": group_index,
                        "source_unit_ids": group,
                    }
                )
        known_bad = sorted(
            {
                leaf["retrieval_unit_id"]
                for source_id in case.get("known_bad_units") or []
                for leaf in by_source.get(source_id, [])
            }
        )
        mapped_cases.append(
            {
                **case,
                "source_required_groups": case.get("required_groups") or [],
                "required_groups": mapped_groups,
                "source_known_bad_units": case.get("known_bad_units") or [],
                "known_bad_units": known_bad,
            }
        )
    payload = {
        "schema_version": "v4-retrieval-gold-mapping.v1",
        "created_at": created_at or utc_now(),
        "case_count": len(mapped_cases),
        "required_group_count": sum(len(case["required_groups"]) for case in mapped_cases),
        "unresolved_groups": unresolved,
        "cases": mapped_cases,
    }
    return payload, mapped_cases


def priority_previews(
    mapped_cases: list[dict[str, Any]],
    leaves: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {leaf["retrieval_unit_id"]: leaf for leaf in leaves}
    selected_ids = {"Q14", "Q15", "Q16", "Q17", "Q19", "Q23A", "Q23B", "Q25"}
    result: list[dict[str, Any]] = []
    for case in mapped_cases:
        if case["id"] not in selected_ids:
            continue
        evidence_ids = list(
            dict.fromkeys(unit_id for group in case["required_groups"] for unit_id in group)
        )
        previews = [
            {
                "retrieval_unit_id": unit_id,
                "citation_text": by_id[unit_id]["citation_text"],
                "clause_no": by_id[unit_id]["clause_no"],
                "source_unit_ids": by_id[unit_id]["source_unit_ids"],
            }
            for unit_id in evidence_ids[:4]
            if unit_id in by_id
        ]
        result.append(
            {
                "id": case["id"],
                "status": (
                    "resolved"
                    if all(case["required_groups"]) or not case.get("answerable", True)
                    else "unresolved"
                ),
                "derived_evidence_count": len(evidence_ids),
                "previews": previews,
            }
        )
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    db_path = args.db.resolve()
    gold_path = args.gold.resolve()
    ore_root = args.ore_root.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    hash_before = sha256_file(db_path)
    connection = connect_readonly(db_path)
    schema_before = schema_snapshot(connection)
    integrity_check = connection.execute("pragma integrity_check").fetchone()[0]
    foreign_key_violations = connection.execute("pragma foreign_key_check").fetchall()
    source_units = load_source_units(connection)
    database_pages: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for row in connection.execute(
        """
        select p.page_id,p.document_id,p.page_no,p.source_page_ref,p.clean_text_sha256
        from pages p
        join documents d using(document_id)
        where d.effective_status='current'
          and d.review_status='approved_for_service'
          and d.can_answer=1
          and p.page_no is not null
        order by p.document_id,p.page_no
        """
    ):
        database_pages[row["document_id"]][int(row["page_no"])] = dict(row)
    builder = RetrievalBuilder(source_units)
    builder.create_table_parents_and_rows()
    builder.create_current_unit_leaves()
    virtual_pages, wide_summary = reconstruct_wide_documents(
        connection,
        builder,
        ore_root,
    )
    connection.close()

    short_missed = [
        item["source_unit_id"]
        for item in builder.source_map.values()
        if item["is_short"] and not item["short_context_supplemented"]
    ]
    long_missed = [
        item["source_unit_id"]
        for item in builder.source_map.values()
        if item["is_long"] and not item["long_action"]
    ]
    if short_missed:
        raise ValueError(f"Short source units without supplemented context: {short_missed[:20]}")
    if long_missed:
        raise ValueError(f"Long source units without a split decision: {long_missed[:20]}")

    source_document_ids = {unit["document_id"] for unit in source_units}
    searchable_document_ids = {
        leaf["document_id"] for leaf in builder.leaves if leaf["search_eligible"]
    }
    missing_searchable_documents = sorted(source_document_ids - searchable_document_ids)
    if missing_searchable_documents:
        raise ValueError(
            "Answer-eligible source documents missing from retrieval leaves: "
            f"{missing_searchable_documents}"
        )

    cross_page_records = build_cross_page_records(builder, database_pages)
    gold_payload = json.loads(gold_path.read_text(encoding="utf-8"))
    mapped_gold_payload, mapped_cases = build_gold_mapping(
        gold_payload["cases"],
        builder.leaves,
        created_at=gold_payload.get("created_at"),
    )
    previews = priority_previews(mapped_cases, builder.leaves)
    review_queue = [
        {
            "retrieval_unit_id": leaf["retrieval_unit_id"],
            "document_id": leaf["document_id"],
            "title": leaf["title"],
            "standard_no": leaf["standard_no"],
            "section_path": leaf["section_path"],
            "clause_no": leaf["clause_no"],
            "char_length": leaf["char_length"],
            "derivation_method": leaf["derivation_method"],
            "search_eligible": leaf["search_eligible"],
            "citation_eligible": leaf["citation_eligible"],
            "source_unit_ids": leaf["source_unit_ids"],
            "source_page_refs": leaf["source_page_refs"],
        }
        for leaf in builder.leaves
        if leaf["review_status"] == "review_required"
    ]
    review_citation_leaks = [
        item["retrieval_unit_id"]
        for item in review_queue
        if item["citation_eligible"]
    ]
    if review_citation_leaks:
        raise ValueError(
            "Review-required leaves cannot be direct citation evidence: "
            f"{review_citation_leaks[:20]}"
        )

    write_jsonl(output_dir / "retrieval_units_v1.jsonl", builder.leaves)
    write_jsonl(
        output_dir / "retrieval_parents_v1.jsonl",
        sorted(builder.parents.values(), key=lambda item: item["parent_node_id"]),
    )
    write_jsonl(
        output_dir / "source_unit_mapping_v1.jsonl",
        sorted(builder.source_map.values(), key=lambda item: item["source_unit_id"]),
    )
    write_jsonl(output_dir / "cross_page_stitching_v1.jsonl", cross_page_records)
    write_jsonl(output_dir / "wide_virtual_pages_v1.jsonl", virtual_pages)
    write_jsonl(output_dir / "review_queue_v1.jsonl", review_queue)
    (output_dir / "gold_cases_retrieval_v1.json").write_text(
        stable_json(mapped_gold_payload), encoding="utf-8"
    )

    hash_after = sha256_file(db_path)
    verify_connection = connect_readonly(db_path)
    schema_after = schema_snapshot(verify_connection)
    verify_connection.close()
    long_actions = Counter(
        item["long_action"]
        for item in builder.source_map.values()
        if item["is_long"]
    )
    stitch_statuses = Counter(item["stitch_status"] for item in cross_page_records)
    summary = {
        "source_document_count": len(source_document_ids),
        "search_eligible_document_count": len(searchable_document_ids),
        "source_unit_count": len(source_units),
        "short_source_unit_count": sum(
            item["is_short"] for item in builder.source_map.values()
        ),
        "short_context_supplemented_count": sum(
            item["short_context_supplemented"] for item in builder.source_map.values()
        ),
        "long_source_unit_count": sum(
            item["is_long"] for item in builder.source_map.values()
        ),
        "long_unit_actions": dict(sorted(long_actions.items())),
        "retrieval_leaf_count": len(builder.leaves),
        "search_eligible_leaf_count": sum(leaf["search_eligible"] for leaf in builder.leaves),
        "citation_eligible_leaf_count": sum(
            leaf["citation_eligible"] for leaf in builder.leaves
        ),
        "review_required_leaf_count": len(review_queue),
        "review_required_searchable_leaf_count": sum(
            item["search_eligible"] for item in review_queue
        ),
        "review_required_citation_eligible_leaf_count": sum(
            item["citation_eligible"] for item in review_queue
        ),
        "long_review_required_leaf_count": sum(
            item["char_length"] >= LONG_THRESHOLD for item in review_queue
        ),
        "parent_node_count": len(builder.parents),
        "cross_page_stitching": {
            "total": len(cross_page_records),
            "status_counts": dict(sorted(stitch_statuses.items())),
        },
        "wide_layout": wide_summary,
        "gold_mapping": {
            "case_count": mapped_gold_payload["case_count"],
            "group_count": mapped_gold_payload["required_group_count"],
            "unresolved_group_count": len(mapped_gold_payload["unresolved_groups"]),
        },
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "database": {
            "path": str(db_path),
            "sha256_before": hash_before,
            "sha256_after": hash_after,
            "schema_unchanged": schema_before == schema_after,
            "integrity_check": integrity_check,
            "foreign_key_violation_count": len(foreign_key_violations),
        },
        "constraints": {
            "source_content_units_mutated": False,
            "final_chunks_created": False,
            "fts_created": False,
            "embeddings_created": False,
            "ann_created": False,
            "knowledge_graph_created": False,
            "cloud_sync_required": False,
        },
        "summary": summary,
        "priority_previews": previews,
        "outputs": {
            "retrieval_units": str((output_dir / "retrieval_units_v1.jsonl").resolve()),
            "retrieval_parents": str((output_dir / "retrieval_parents_v1.jsonl").resolve()),
            "source_unit_mapping": str((output_dir / "source_unit_mapping_v1.jsonl").resolve()),
            "cross_page_stitching": str((output_dir / "cross_page_stitching_v1.jsonl").resolve()),
            "wide_virtual_pages": str((output_dir / "wide_virtual_pages_v1.jsonl").resolve()),
            "review_queue": str((output_dir / "review_queue_v1.jsonl").resolve()),
            "gold_cases": str((output_dir / "gold_cases_retrieval_v1.json").resolve()),
        },
    }
    (output_dir / "manifest_v1.json").write_text(stable_json(payload), encoding="utf-8")
    (output_dir / "report_v1.md").write_text(render_report(payload), encoding="utf-8")
    return payload


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--db", type=Path, default=DEFAULT_DB)
    result.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    result.add_argument("--ore-root", type=Path, default=DEFAULT_ORE_ROOT)
    result.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return result


def main() -> None:
    payload = run(parser().parse_args())
    print(stable_json(payload["summary"]), end="")


if __name__ == "__main__":
    main()
