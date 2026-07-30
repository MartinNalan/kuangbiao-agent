#!/usr/bin/env python3
"""Audit v4 retrieval-preprocessing readiness without mutating the corpus.

The source ``content_units`` remain the provenance layer.  This audit only
describes structural risk pools and the readiness of the current gold
evidence for a later, separately approved retrieval-unit derivation step.
It creates no Chunk, FTS, embedding, ANN, or graph artifact.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "knowledge_base_v4" / "db" / "corpus.sqlite"
DEFAULT_GOLD = (
    PROJECT_ROOT
    / "data"
    / "knowledge_base_v4"
    / "evaluation"
    / "fullscan_gold_cases_v1.json"
)
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "knowledge_base_v4" / "evaluation"


STATUS_LABELS = {
    "ready_current_unit": "现有单元边界可作为首轮检索叶子",
    "needs_preprocessing": "需要检索预处理",
    "boundary_blocked": "引用边界待修复",
    "corpus_gap": "语料缺失诊断",
    "invalid_gold": "Gold 单元异常",
}

ISSUE_LABELS = {
    "evidence_source_missing": "正确处罚依据尚未进入 v4",
    "missing_required_unit": "Gold 单元在数据库中缺失",
    "required_unit_not_answerable": "Gold 单元不在当前可答范围",
    "unsafe_boundary": "现有单元混入无关版面文字，不可直接引用",
    "long_policy_unit": "政策单元过长且包含多个子条款，应派生子条款叶子",
    "table_row_parent_link": "总表与材料行缺少父子关系",
    "cross_page_review": "跨页条款需核对语义连续性",
    "multi_evidence_bundle": "完整答案需要组合多组证据",
    "very_short_leaf": "过短单元需要标题或父级上下文",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


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


def length_band(length: int) -> str:
    if length < 20:
        return "<20"
    if length < 50:
        return "20-49"
    if length < 150:
        return "50-149"
    if length < 300:
        return "150-299"
    if length < 600:
        return "300-599"
    if length < 1200:
        return "600-1199"
    return ">=1200"


def page_span(unit: dict[str, Any]) -> int | None:
    start = unit.get("page_start")
    end = unit.get("page_end")
    if start is None or end is None:
        return None
    return int(end) - int(start)


def classify_gold_case(
    case: dict[str, Any],
    units: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    groups = case.get("required_groups") or []
    required_ids = sorted({unit_id for group in groups for unit_id in group})
    unsafe_ids = set(case.get("unsafe_for_direct_quote") or [])
    issues: list[str] = []

    if not case.get("answerable", True):
        issues.append("evidence_source_missing")
        status = "corpus_gap"
        selected: list[dict[str, Any]] = []
    else:
        missing = [unit_id for unit_id in required_ids if unit_id not in units]
        if missing:
            issues.append("missing_required_unit")
        selected = [units[unit_id] for unit_id in required_ids if unit_id in units]
        if any(not unit["eligible"] for unit in selected):
            issues.append("required_unit_not_answerable")
        if unsafe_ids & set(required_ids):
            issues.append("unsafe_boundary")
        if any(
            unit["unit_type"] == "policy_clause" and unit["char_length"] >= 1200
            for unit in selected
        ):
            issues.append("long_policy_unit")
        types = {unit["unit_type"] for unit in selected}
        if "table" in types and "application_material_row" in types:
            issues.append("table_row_parent_link")
        if any((page_span(unit) or 0) > 0 for unit in selected):
            issues.append("cross_page_review")
        if len(groups) > 1:
            issues.append("multi_evidence_bundle")
        if any(unit["char_length"] < 20 for unit in selected):
            issues.append("very_short_leaf")

        if "missing_required_unit" in issues or "required_unit_not_answerable" in issues:
            status = "invalid_gold"
        elif "unsafe_boundary" in issues:
            status = "boundary_blocked"
        elif issues:
            status = "needs_preprocessing"
        else:
            status = "ready_current_unit"

    return {
        "id": case["id"],
        "family": case["family"],
        "question": case["question"],
        "answerable": case.get("answerable", True),
        "required_group_count": len(groups),
        "required_unit_count": len(required_ids),
        "status": status,
        "status_zh": STATUS_LABELS[status],
        "issues": issues,
        "issues_zh": [ISSUE_LABELS[issue] for issue in issues],
        "unit_types": sorted({unit["unit_type"] for unit in selected}),
        "minimum_char_length": min(
            (unit["char_length"] for unit in selected), default=None
        ),
        "maximum_char_length": max(
            (unit["char_length"] for unit in selected), default=None
        ),
        "unsafe_unit_ids": sorted(unsafe_ids & set(required_ids)),
    }


def load_eligible_units(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        select c.unit_id,c.document_id,c.parent_unit_id,c.unit_type,
               coalesce(c.section_path,'') section_path,
               coalesce(c.clause_no,'') clause_no,c.page_start,c.page_end,
               c.clean_text,c.structure_json,d.corpus,d.title,
               coalesce(d.standard_no,'') standard_no
        from content_units c
        join documents d using(document_id)
        where d.effective_status='current'
          and d.review_status='approved_for_service'
          and d.can_answer=1
        order by d.document_id,c.unit_order
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        if unit_is_explicitly_excluded(row["structure_json"]):
            continue
        item = dict(row)
        item["char_length"] = len(row["clean_text"])
        item["eligible"] = True
        result.append(item)
    return result


def load_gold_units(
    connection: sqlite3.Connection,
    required_ids: set[str],
    eligible_ids: set[str],
) -> dict[str, dict[str, Any]]:
    if not required_ids:
        return {}
    placeholders = ",".join("?" for _ in required_ids)
    rows = connection.execute(
        f"""
        select c.unit_id,c.document_id,c.parent_unit_id,c.unit_type,
               coalesce(c.section_path,'') section_path,
               coalesce(c.clause_no,'') clause_no,c.page_start,c.page_end,
               c.clean_text,d.corpus,d.title,coalesce(d.standard_no,'') standard_no
        from content_units c
        join documents d using(document_id)
        where c.unit_id in ({placeholders})
        """,
        sorted(required_ids),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        item["char_length"] = len(row["clean_text"])
        item["eligible"] = row["unit_id"] in eligible_ids
        result[row["unit_id"]] = item
    return result


def build_statistics(units: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for unit in units:
        grouped[unit["unit_type"]].append(unit)
    for unit_type, items in sorted(grouped.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        lengths = [item["char_length"] for item in items]
        by_type[unit_type] = {
            "count": len(items),
            "minimum_char_length": min(lengths),
            "average_char_length": round(sum(lengths) / len(lengths), 1),
            "maximum_char_length": max(lengths),
            "multi_page_count": sum((page_span(item) or 0) > 0 for item in items),
        }

    corpus_documents: dict[str, set[str]] = defaultdict(set)
    corpus_units = Counter()
    length_bands = Counter()
    for unit in units:
        corpus_documents[unit["corpus"]].add(unit["document_id"])
        corpus_units[unit["corpus"]] += 1
        length_bands[length_band(unit["char_length"])] += 1

    return {
        "eligible_document_count": len({unit["document_id"] for unit in units}),
        "eligible_unit_count": len(units),
        "parented_unit_count": sum(unit["parent_unit_id"] is not None for unit in units),
        "missing_section_path_count": sum(not unit["section_path"].strip() for unit in units),
        "missing_clause_no_count": sum(not unit["clause_no"].strip() for unit in units),
        "missing_page_start_count": sum(unit["page_start"] is None for unit in units),
        "under_20_char_count": sum(unit["char_length"] < 20 for unit in units),
        "over_1200_char_count": sum(unit["char_length"] >= 1200 for unit in units),
        "multi_page_count": sum((page_span(unit) or 0) > 0 for unit in units),
        "length_bands": {
            band: length_bands[band]
            for band in ("<20", "20-49", "50-149", "150-299", "300-599", "600-1199", ">=1200")
        },
        "by_corpus": {
            corpus: {
                "document_count": len(corpus_documents[corpus]),
                "unit_count": corpus_units[corpus],
            }
            for corpus in sorted(corpus_units)
        },
        "by_unit_type": by_type,
    }


def render_report(payload: dict[str, Any]) -> str:
    stats = payload["statistics"]
    lines = [
        "# v4 检索预处理就绪度审计 v1",
        "",
        f"生成时间：`{payload['created_at']}`",
        "",
        f"数据库 SHA-256：`{payload['database']['sha256_before']}`",
        "",
        "## 审计边界",
        "",
        "- 本次只读检查现有内容单元及 Gold 证据边界。",
        "- 未修改 v4 数据库，未生成最终 Chunk、FTS、Embedding、ANN 或知识图谱。",
        "- 以下数量是检索预处理风险池，不等同于原文错误或 OCR 错误。",
        "",
        "## 核心结论",
        "",
        f"- 当前可答范围为 {stats['eligible_document_count']} 份文档、{stats['eligible_unit_count']} 个内容单元。",
        f"- 已建立父子关系的内容单元为 {stats['parented_unit_count']} 个；当前尚无条款—章节父子链。",
        f"- 少于 20 字的单元有 {stats['under_20_char_count']} 个，需要上下文补强或降噪判断，不能仅按长度删除。",
        f"- 不少于 1,200 字的单元有 {stats['over_1200_char_count']} 个，需检查是否应按子条款、题目或表格行派生叶子。",
        f"- 跨页单元有 {stats['multi_page_count']} 个，需判断是合法跨页连续条款还是版面串接。",
        "",
        "## 长度分布",
        "",
        "| 字符数 | 单元数 |",
        "|---|---:|",
    ]
    for band, count in stats["length_bands"].items():
        lines.append(f"| {band} | {count} |")

    lines.extend(
        [
            "",
            "## 单元类型",
            "",
            "| 类型 | 数量 | 平均字符数 | 最大字符数 | 跨页单元 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for unit_type, values in stats["by_unit_type"].items():
        lines.append(
            f"| {unit_type} | {values['count']} | {values['average_char_length']} | "
            f"{values['maximum_char_length']} | {values['multi_page_count']} |"
        )

    lines.extend(
        [
            "",
            "## Gold 证据边界检查",
            "",
            "| 题目 | 当前状态 | 必要证据组 | 主要问题 |",
            "|---|---|---:|---|",
        ]
    )
    for case in payload["gold_readiness"]:
        issues = "；".join(case["issues_zh"]) or "未发现结构性阻塞"
        lines.append(
            f"| {case['id']} | {case['status_zh']} | {case['required_group_count']} | {issues} |"
        )

    lines.extend(
        [
            "",
            "## 第一版检索预处理原则草案",
            "",
            "1. 原始 `content_units` 保持不变，继续作为正文与溯源层；检索单元采用可重建的派生层。",
            "2. 检索叶子优先保持一个完整条款、一个政策子项或一个表格行，不跨越无关条款。",
            "3. 建立文档—章节—条款/表格行父子关系。检索文本可以补入标题、标准号和章节路径，答案引用只使用叶子原文。",
            "4. 长度只作为风险信号：短单元补父级上下文，不直接删除；超长单元按语义和编号边界拆分，不机械等长切割。",
            "5. 跨页条款先核对连续性。连续条款可保持一个叶子；混入其他栏、附录或表格的内容应派生干净叶子。",
            "6. 表格采用“总表父级 + 行级叶子”。总表用于理解列定义和全局说明，具体材料条件由行级叶子引用。",
            "7. 每个派生单元必须保留来源文档、标准号、条款号、页码、原 `unit_id` 列表、文本哈希及派生规则版本。",
            "",
            "## 建议的下一小步",
            "",
            "先制作少量、可人工核对的派生单元预览，不写入数据库：",
            "",
            "- 将 `自然资规〔2023〕4号` 的超长政策单元按（二）（三）及其数字子项拆成叶子；",
            "- 为附件4建立总表—申请情形—材料行父子关系；",
            "- 从 `GB/T 25283-2023` 第9.2～9.4条派生无附录串入的干净引用叶子；",
            "- 对 `GB/T 13908-2020` 第8.7.2.3条验证跨页连续性并保留完整条款。",
            "",
            "预览经人工审阅后，再决定是否生成全库实验性检索单元。",
            "",
            "## 完整性",
            "",
            f"- 数据库执行前哈希：`{payload['database']['sha256_before']}`。",
            f"- 数据库执行后哈希：`{payload['database']['sha256_after']}`。",
            f"- SQLite integrity_check：`{payload['database']['integrity_check']}`。",
            f"- 外键违规：{payload['database']['foreign_key_violation_count']}。",
            f"- schema 前后一致：`{str(payload['database']['schema_unchanged']).lower()}`。",
        ]
    )
    return "\n".join(lines) + "\n"


def schema_snapshot(connection: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    return [
        (row[0], row[1], row[2], row[3] or "")
        for row in connection.execute(
            "select type,name,tbl_name,sql from sqlite_master order by type,name"
        )
    ]


def run(args: argparse.Namespace) -> dict[str, Any]:
    db_path = args.db.resolve()
    gold_path = args.gold.resolve()
    output_dir = args.output_dir.resolve()
    hash_before = sha256_file(db_path)
    connection = connect_readonly(db_path)
    schema_before = schema_snapshot(connection)
    integrity_check = connection.execute("pragma integrity_check").fetchone()[0]
    foreign_key_violations = connection.execute("pragma foreign_key_check").fetchall()
    units = load_eligible_units(connection)
    statistics = build_statistics(units)

    gold_payload = json.loads(gold_path.read_text(encoding="utf-8"))
    cases = gold_payload["cases"]
    required_ids = {
        unit_id
        for case in cases
        for group in case.get("required_groups") or []
        for unit_id in group
    }
    eligible_ids = {unit["unit_id"] for unit in units}
    gold_units = load_gold_units(connection, required_ids, eligible_ids)
    gold_readiness = [classify_gold_case(case, gold_units) for case in cases]
    connection.close()

    hash_after = sha256_file(db_path)
    verify_connection = connect_readonly(db_path)
    schema_after = schema_snapshot(verify_connection)
    verify_connection.close()
    payload = {
        "schema_version": "v4-retrieval-preprocessing-readiness-v1",
        "created_at": utc_now(),
        "database": {
            "path": str(db_path),
            "sha256_before": hash_before,
            "sha256_after": hash_after,
            "integrity_check": integrity_check,
            "foreign_key_violation_count": len(foreign_key_violations),
            "schema_unchanged": schema_before == schema_after,
        },
        "constraints": {
            "read_only": True,
            "source_content_units_mutated": False,
            "final_chunks_created": False,
            "fts_created": False,
            "embeddings_created": False,
            "ann_created": False,
            "knowledge_graph_created": False,
            "cloud_sync_required": False,
        },
        "statistics": statistics,
        "gold_readiness": gold_readiness,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "retrieval_preprocessing_audit_v1.json"
    report_path = output_dir / "retrieval_preprocessing_audit_v1.md"
    json_path.write_text(stable_json(payload), encoding="utf-8")
    report_path.write_text(render_report(payload), encoding="utf-8")
    return payload


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--db", type=Path, default=DEFAULT_DB)
    result.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    result.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return result


def main() -> None:
    payload = run(parser().parse_args())
    status_counts = Counter(case["status"] for case in payload["gold_readiness"])
    print(
        stable_json(
            {
                "database_sha256": payload["database"]["sha256_after"],
                "eligible_documents": payload["statistics"]["eligible_document_count"],
                "eligible_units": payload["statistics"]["eligible_unit_count"],
                "parented_units": payload["statistics"]["parented_unit_count"],
                "under_20_chars": payload["statistics"]["under_20_char_count"],
                "over_1200_chars": payload["statistics"]["over_1200_char_count"],
                "multi_page_units": payload["statistics"]["multi_page_count"],
                "gold_statuses": dict(sorted(status_counts.items())),
            }
        ),
        end="",
    )


if __name__ == "__main__":
    main()
