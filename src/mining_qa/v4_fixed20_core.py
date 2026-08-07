from __future__ import annotations

"""Clean production implementation of the T092 fixed-20 algorithm.

This module deliberately has no dependency on ``scripts/``.  It is a pure
runtime extraction of the accepted retrieval and structural-selection rules;
it contains no benchmark cases, Gold labels, real evidence identifiers,
reference answers, or copied standard body text.
"""

from collections import defaultdict
from collections.abc import Mapping
from contextvars import ContextVar, Token
from copy import deepcopy
import json
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any
import unicodedata

from .technical_level_evidence_chain import (
    apply_to_fixed20_runtime,
    fail_closed_runtime,
)
from .technical_sufficiency_decision_t092 import (
    ALGORITHM_SNAPSHOT_ID,
    RULE_SCHEMA_VERSION,
    TRACE_KEY,
    deterministic_technical_sufficiency_plan,
)
from . import v4_retrieval_primitives as retrieval


TASK_ID = "T094"
SCHEMA_VERSION = "geowiki-v4-t094-clean-fixed20.v1"
FINAL_POOL_SIZE = 20
ARMS = (
    "A_raw_first20",
    "B_generic_branch_clean_fixed20",
    "C_generic_full_structural_fixed20",
)
BASE_POOL_KEY = ARMS[2]

APPENDIX_CHILD = re.compile(r"^([A-Z])\.(\d+)\.(\d+)$")
EXPLICIT_CLAUSE = re.compile(r"^\d+(?:\.\d+)+$")
NORMATIVE_HIGH_AUTHORITY_TYPES = {
    "law",
    "regulation",
    "administrative_regulation",
    "departmental_rule",
}
NORMATIVE_REQUEST_ANCHORS = ("规定", "要求", "怎么", "如何")
REVIEW_CONCEPT_VARIANTS = ("评审", "审查")
UNPRESSED_QUESTION_ANCHORS = ("不作为压覆", "未压覆")
UNPRESSED_CONTEXT_ANCHORS = ("调查", "附件", "调查报告")
APPENDIX_DOMAIN_ANCHORS = (
    "未压覆",
    "不作为压覆",
    "压覆",
    "矿产资源调查",
    "调查报告",
    "勘查报告",
    "储量报告",
)
CONDITIONAL_APPLICABILITY_ANCHORS = (
    "特定情形",
    "哪些情形",
    "什么情形",
    "何时适用",
    "适用条件",
)
CONDITIONAL_SOURCE_MARKERS = (
    "有异议",
    "结论为",
    "反之",
    "如果",
    "若",
    "当",
    "只有",
    "除外",
    "签订",
    "后",
)
MATERIAL_REFERENCE_SUFFIXES = (
    "协议",
    "合同",
    "委托函",
    "证明",
    "意见书",
    "许可证",
    "批复文件",
)
CONDITIONAL_MATERIAL_SUBSTITUTION_ANCHORS = (
    "没有重大调整",
    "无重大调整",
    "未作重大调整",
    "未发生重大调整",
    "可以提交什么",
    "可提交什么",
)
DETAILED_CLASSIFICATION_SCOPE_ANCHORS = (
    "分类体系",
    "类型体系",
    "分为哪些类型",
    "各类型",
)
DETAILED_CLASSIFICATION_CONDITION_ANCHORS = (
    "各类型如何判定",
    "核心判定条件",
    "核心确定条件",
    "地质可靠程度",
    "判定条件",
    "确定条件",
)
DETAILED_CLASSIFICATION_RELATION_ANCHORS = (
    "转换关系",
    "调整规则",
    "何时需要调整",
    "类型调整",
    "调整类型",
)
GENERIC_STANDARD_TITLE_PARTS = (
    "固体矿产地质勘查规范",
    "矿产地质勘查规范",
    "地质勘查规范",
    "勘查规范",
)
BROAD_FAMILY_PATTERNS = (
    "分类体系",
    "类型体系",
    "组成体系",
    "分为哪些类型",
    "各分为哪些",
    "分别如何组成",
    "分类分级",
)
SHORT_DOMAIN_PARENT_HEADINGS = {"资源量", "储量"}
PAIR_FIELD_A = "划分单元"
PAIR_FIELD_B = "矿产资源储量规模"
BROAD_CLASSIFICATION_PATTERNS = (
    "怎么划分",
    "如何划分",
    "分为哪些",
    "分为哪",
    "有哪些类型",
    "哪些类型",
    "分类体系",
    "组成体系",
)
SECTION_ANCHORS = ("分类", "类型", "划分", "组成", "体系")
CLASSIFICATION_RELATION = re.compile(
    r"分为|划分为|不再分级|类型包括|包括.{0,40}(?:资源量|储量)"
)
CONVERSION_RELATION = re.compile(r"在.{0,100}中根据.{0,100}估算")
SUPPORTED_BRANCHES = ("新立", "延续")
TARGET_SUFFIXES = ("项目", "事项", "类型", "材料", "流程", "条件", "要求", "规定")
DETERMINATION_PATTERNS = ("如何确定", "怎样确定", "怎么确定")


def compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def eligible(row: Mapping[str, Any]) -> bool:
    return bool(row.get("search_eligible") and row.get("citation_eligible"))


def read_rows(path: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_id = {row["retrieval_unit_id"]: row for row in rows}
    if len(rows) != 24168 or len(by_id) != len(rows):
        raise RuntimeError("T068 retrieval population changed")
    return rows, by_id


def load_governed_families(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("schema_version")
        != "geowiki-v4-governed-concept-families.v1"
        or payload.get("aggregation") != "max_variant_once_per_concept"
    ):
        raise RuntimeError("T068 governed concept registry changed")
    families = []
    for family in payload["families"]:
        if family.get("status") != "active":
            continue
        families.append(
            {
                "family_id": family["family_id"],
                "variants": list(
                    dict.fromkeys(compact(value) for value in family["variants"])
                ),
            }
        )
    return families


def normalize_material_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    return re.sub(r"\s+", "", value)


def explicit_material_branch(question: str) -> str | None:
    text = normalize_material_text(question)
    if "采矿" not in text or "提交" not in text or "材料" not in text:
        return None
    if not any(token in text for token in ("哪些材料", "什么材料")):
        return None
    if "新立" in text:
        return "新立"
    if "延续" in text:
        return "延续"
    return None


def material_number(section_path: str) -> int:
    match = re.search(r">\s*材料\s+(\d+)\s*$", section_path or "")
    if not match:
        raise ValueError(f"material row lacks source number: {section_path}")
    return int(match.group(1))


def branch_manifest(rows: list[dict[str, Any]], branch: str) -> list[dict[str, Any]]:
    if branch not in SUPPORTED_BRANCHES:
        raise ValueError(f"unsupported branch: {branch}")
    pattern = re.compile(rf"^附件4\s*>\s*{re.escape(branch)}\s*>\s*材料\s+\d+\s*$")
    selected = []
    for row in rows:
        if row.get("unit_type") != "application_material_row":
            continue
        if "自然资规〔2023〕4号附件4" not in (row.get("standard_no") or ""):
            continue
        if not pattern.match(row.get("section_path") or ""):
            continue
        if not eligible(row):
            raise RuntimeError(
                "authoritative application branch row is not eligible: "
                f"{row['retrieval_unit_id']}"
            )
        selected.append(
            {
                "unit_id": row["retrieval_unit_id"],
                "material_no": material_number(row.get("section_path") or ""),
                "section_path": row["section_path"],
                "citation_text_sha256": row["citation_text_sha256"],
                "source_unit_ids": row.get("source_unit_ids") or [],
            }
        )
    selected.sort(key=lambda row: (row["material_no"], row["unit_id"]))
    if len({row["unit_id"] for row in selected}) != len(selected):
        raise RuntimeError(f"duplicate application branch row: {branch}")
    expected_count = {"新立": 14, "延续": 10}[branch]
    if len(selected) != expected_count:
        raise RuntimeError(f"application branch row count changed: {branch} {len(selected)}")
    return selected


def list_manifests(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {branch: branch_manifest(rows, branch) for branch in SUPPORTED_BRANCHES}


def source_branch(row: Mapping[str, Any]) -> str | None:
    if (
        row.get("unit_type") != "application_material_row"
        or "自然资规〔2023〕4号附件4"
        not in str(row.get("standard_no") or "")
    ):
        return None
    match = re.match(
        r"^附件4\s*>\s*([^>]+?)\s*>", str(row.get("section_path") or "")
    )
    return match.group(1).strip() if match else None


def clean_clause_no(value: str) -> str:
    return (value or "").strip().rstrip(".")


def immediate_parent_clause(value: str) -> str | None:
    clause = clean_clause_no(value)
    if "#" in clause or not clause:
        return None
    parts = clause.split(".")
    if len(parts) < 3 or not all(part.isdigit() for part in parts[1:]):
        return None
    if not (parts[0].isdigit() or (len(parts[0]) == 1 and parts[0].isalpha())):
        return None
    return ".".join(parts[:-1])


def final_clause_number(value: str) -> int | None:
    clause = clean_clause_no(value)
    part = clause.rsplit(".", 1)[-1]
    return int(part) if part.isdigit() else None


def explicit_family_catalog(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eligible_rows = {
        row["retrieval_unit_id"]: row for row in rows if eligible(row)
    }
    source_order = {
        row["retrieval_unit_id"]: index for index, row in enumerate(rows)
    }
    headers = {
        (row["document_id"], clean_clause_no(row.get("clause_no") or ""))
        for row in eligible_rows.values()
        if clean_clause_no(row.get("clause_no") or "")
    }
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for unit_id, row in eligible_rows.items():
        parent = immediate_parent_clause(row.get("clause_no") or "")
        if parent and (row["document_id"], parent) in headers:
            grouped[(row["document_id"], parent)].append(unit_id)
    families = []
    for (document_id, parent), child_ids in grouped.items():
        children = sorted(set(child_ids), key=source_order.__getitem__)
        if not 2 <= len(children) <= 12:
            continue
        numbered: dict[int, str] = {}
        duplicate_numbers = []
        for unit_id in children:
            number = final_clause_number(
                eligible_rows[unit_id].get("clause_no") or ""
            )
            if number is None:
                continue
            if number in numbered:
                duplicate_numbers.append(number)
            numbered[number] = unit_id
        if duplicate_numbers or len(numbered) < 2:
            continue
        families.append(
            {
                "document_id": document_id,
                "parent_clause": parent,
                "child_ids": children,
                "numbered_children": numbered,
                "child_count": len(children),
            }
        )
    families.sort(key=lambda row: (row["document_id"], row["parent_clause"]))
    return families


def compact_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").lower()
    return re.sub(r"\s+", "", normalized)


def question_target(question: str) -> str | None:
    text = compact_text(question).rstrip("?？。")
    marker = next((value for value in DETERMINATION_PATTERNS if value in text), None)
    if marker is None:
        return None
    prefix = text.split(marker, 1)[0].rstrip("应")
    segments = [value for value in re.split(r"[，,；;：:]", prefix) if value]
    target = segments[-1] if segments else prefix
    if "中" in target:
        suffix = target.rsplit("中", 1)[-1]
        if len(suffix) >= 2:
            target = suffix
    target = target.strip("的")
    return target if len(target) >= 2 else None


def target_anchors(target: str | None) -> list[str]:
    if not target:
        return []
    candidates = [target]
    for suffix in TARGET_SUFFIXES:
        if target.endswith(suffix) and len(target) - len(suffix) >= 4:
            candidates.append(target[: -len(suffix)])
    return sorted(set(candidates), key=lambda value: (-len(value), value))


def shared_anchor(
    anchors: list[str],
    left: Mapping[str, Any],
    middle: Mapping[str, Any],
    right: Mapping[str, Any],
) -> str | None:
    texts = [
        compact_text(str(row.get("citation_text") or ""))
        for row in (left, middle, right)
    ]
    return next(
        (
            anchor
            for anchor in anchors
            if len(anchor) >= 4 and all(anchor in text for text in texts)
        ),
        None,
    )


def gap_completion(
    case: Mapping[str, Any],
    base_pool: list[str],
    families: list[dict[str, Any]],
    row_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    target = question_target(str(case["question"]))
    anchors = target_anchors(target)
    if target is None:
        return {
            "triggered": False,
            "question_target": None,
            "target_anchors": [],
            "structural_gap_count": 0,
            "rejected_gaps": [],
            "selected": [],
            "selected_ids": [],
            "selection": "non_determination_question_identity",
            "gold_not_used_for_selection": True,
        }
    present = set(base_pool)
    candidates = set(case["candidate_ids"])
    selected = []
    rejected = []
    structural_gap_count = 0
    for family in families:
        numbered = family["numbered_children"]
        for number, unit_id in sorted(numbered.items()):
            if unit_id in present or number - 1 not in numbered or number + 1 not in numbered:
                continue
            left_id = numbered[number - 1]
            right_id = numbered[number + 1]
            if left_id not in present or right_id not in present:
                continue
            structural_gap_count += 1
            row = row_by_id[unit_id]
            anchor = shared_anchor(
                anchors, row_by_id[left_id], row, row_by_id[right_id]
            )
            reasons = []
            if unit_id not in candidates:
                reasons.append("outside_frozen_candidate_union")
            if not anchor:
                reasons.append("no_shared_question_target_anchor")
            if "确定" not in compact_text(str(row.get("citation_text") or "")):
                reasons.append("middle_clause_lacks_determination_relation")
            record = {
                "document_id": family["document_id"],
                "parent_clause": family["parent_clause"],
                "left_id": left_id,
                "left_clause_no": row_by_id[left_id].get("clause_no"),
                "unit_id": unit_id,
                "clause_no": row.get("clause_no"),
                "right_id": right_id,
                "right_clause_no": row_by_id[right_id].get("clause_no"),
                "shared_anchor": anchor,
                "citation_text_sha256": row.get("citation_text_sha256"),
            }
            if reasons:
                rejected.append(record | {"reasons": reasons})
            else:
                selected.append(record)
    selected.sort(
        key=lambda row: (row["document_id"], row["parent_clause"], row["clause_no"])
    )
    selected_ids = list(dict.fromkeys(row["unit_id"] for row in selected))
    return {
        "triggered": bool(selected_ids),
        "question_target": target,
        "target_anchors": anchors,
        "structural_gap_count": structural_gap_count,
        "rejected_gaps": rejected,
        "selected": selected,
        "selected_ids": selected_ids,
        "selection": "intervening_sibling_with_shared_target_and_relation",
        "gold_not_used_for_selection": True,
    }


def broad_family_request(question: str) -> bool:
    text = compact(question)
    if "阶段" in text and not any(
        anchor in text
        for anchor in ("分类体系", "类型体系", "组成体系", "如何组成", "分类分级")
    ):
        return False
    return any(pattern in text for pattern in BROAD_FAMILY_PATTERNS)


def heading_core(row: Mapping[str, Any]) -> str:
    text = str(row.get("citation_text") or "")
    first_line = next(
        (line.strip() for line in text.splitlines() if line.strip()), ""
    )
    clause = str(row.get("clause_no") or "").strip()
    if clause:
        first_line = re.sub(rf"^{re.escape(clause)}\s*", "", first_line)
    return compact(first_line)


def direct_child_catalog(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str], list[str]]:
    catalog: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in rows:
        clause = str(row.get("clause_no") or "").strip()
        if not EXPLICIT_CLAUSE.fullmatch(clause) or not eligible(row):
            continue
        parent = clause.rsplit(".", 1)[0]
        catalog[(row["document_id"], parent)].append(row["retrieval_unit_id"])
    return dict(catalog)


def minimal_source_family_completion(
    question: str,
    base_ids: list[str],
    row_by_id: Mapping[str, Mapping[str, Any]],
    child_catalog: Mapping[tuple[str, str], list[str]],
) -> dict[str, Any]:
    if not broad_family_request(question):
        return {
            "triggered": False,
            "selection": "non_broad_family_question_identity",
            "matched_parent_routes": [],
            "selected": [],
            "selected_ids": [],
            "gold_used_for_selection": False,
        }
    question_text = compact(question)
    matched_by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for unit_id in base_ids:
        row = row_by_id[unit_id]
        clause = str(row.get("clause_no") or "").strip()
        if not EXPLICIT_CLAUSE.fullmatch(clause):
            continue
        if (row["document_id"], clause) not in child_catalog:
            continue
        heading = heading_core(row)
        if len(heading) < 3 or heading not in question_text:
            continue
        matched_by_document[str(row["document_id"])].append(
            {
                "unit_id": unit_id,
                "standard_no": str(row.get("standard_no") or ""),
                "section_path": str(row.get("section_path") or ""),
                "clause_no": clause,
                "heading": heading,
            }
        )

    present = set(base_ids)
    routes: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    for document_id, matches in matched_by_document.items():
        standard_no = matches[0]["standard_no"]
        explicit_standard = bool(standard_no) and compact(standard_no) in question_text
        distinct_parents = {match["clause_no"] for match in matches}
        if not explicit_standard and len(distinct_parents) < 2:
            continue
        route_reason = (
            "explicit_standard_number_plus_named_parent"
            if explicit_standard
            else "two_or_more_named_parents_same_document"
        )
        for match in matches:
            route = {
                **match,
                "document_id": document_id,
                "route_reason": route_reason,
                "direct_child_count": len(
                    child_catalog[(document_id, match["clause_no"])]
                ),
            }
            routes.append(route)
            for child_id in child_catalog[(document_id, match["clause_no"])]:
                if child_id in present:
                    continue
                child = row_by_id[child_id]
                selected.append(
                    {
                        "unit_id": child_id,
                        "document_id": document_id,
                        "standard_no": str(child.get("standard_no") or ""),
                        "section_path": str(child.get("section_path") or ""),
                        "clause_no": str(child.get("clause_no") or ""),
                        "parent_clause_no": match["clause_no"],
                        "parent_heading": match["heading"],
                        "route_reason": route_reason,
                        "inside_frozen_candidate_union": False,
                    }
                )
                present.add(child_id)
    return {
        "triggered": bool(selected),
        "selection": "broad_question_named_parent_direct_children",
        "matched_parent_routes": routes,
        "selected": selected,
        "selected_ids": [row["unit_id"] for row in selected],
        "selected_count": len(selected),
        "gold_used_for_selection": False,
    }


def heading_matches_question(heading: str, question_text: str) -> bool:
    if len(heading) < 3 and heading not in SHORT_DOMAIN_PARENT_HEADINGS:
        return False
    return heading in question_text


def direct_family_reservation(
    question: str,
    candidate_ids: list[str],
    row_by_id: Mapping[str, Mapping[str, Any]],
    child_catalog: Mapping[tuple[str, str], list[str]],
) -> dict[str, Any]:
    if not broad_family_request(question):
        return {
            "triggered": False,
            "selection": "non_broad_family_identity",
            "routes": [],
            "selected_ids": [],
        }
    question_text = compact(question)
    candidate_set = set(candidate_ids)
    matched_by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for unit_id in candidate_ids:
        row = row_by_id[unit_id]
        clause = str(row.get("clause_no") or "").strip()
        if not EXPLICIT_CLAUSE.fullmatch(clause):
            continue
        if (row["document_id"], clause) not in child_catalog:
            continue
        heading = heading_core(row)
        if not heading_matches_question(heading, question_text):
            continue
        matched_by_document[str(row["document_id"])].append(
            {
                "unit_id": unit_id,
                "document_id": row["document_id"],
                "standard_no": str(row.get("standard_no") or ""),
                "clause_no": clause,
                "heading": heading,
            }
        )
    routes: list[dict[str, Any]] = []
    selected: list[str] = []
    for document_id, matches in matched_by_document.items():
        standard_no = matches[0]["standard_no"]
        explicit_standard = bool(standard_no) and compact(standard_no) in question_text
        distinct_parents = {match["clause_no"] for match in matches}
        if not explicit_standard and len(distinct_parents) < 2:
            continue
        reason = (
            "explicit_standard_number_plus_named_parent"
            if explicit_standard
            else "two_or_more_named_parents_same_document"
        )
        for match in matches:
            child_ids = [
                unit_id
                for unit_id in child_catalog[(document_id, match["clause_no"])]
                if unit_id in candidate_set
            ]
            routes.append(
                {
                    **match,
                    "route_reason": reason,
                    "reserved_direct_child_count": len(child_ids),
                }
            )
            for unit_id in child_ids:
                if unit_id not in selected:
                    selected.append(unit_id)
    return {
        "triggered": bool(selected),
        "selection": "named_parent_direct_child_family_reservation",
        "routes": routes,
        "selected_ids": selected,
    }


def paired_field_reservation(
    question: str,
    candidate_ids: list[str],
    row_by_id: Mapping[str, Mapping[str, Any]],
    child_catalog: Mapping[tuple[str, str], list[str]],
) -> dict[str, Any]:
    question_text = compact(question)
    if PAIR_FIELD_A not in question_text or not any(
        anchor in question_text
        for anchor in ("数量指标", "资源储量规模", PAIR_FIELD_B)
    ):
        return {
            "triggered": False,
            "selection": "non_paired_field_question_identity",
            "routes": [],
            "selected_ids": [],
        }
    candidate_set = set(candidate_ids)
    explicit_documents = {
        str(row_by_id[unit_id]["document_id"])
        for unit_id in candidate_ids
        if str(row_by_id[unit_id].get("standard_no") or "")
        and compact(str(row_by_id[unit_id].get("standard_no") or ""))
        in question_text
    }
    routes: list[dict[str, Any]] = []
    selected: list[str] = []
    for (document_id, parent_clause), child_ids in child_catalog.items():
        if document_id not in explicit_documents:
            continue
        available = [unit_id for unit_id in child_ids if unit_id in candidate_set]
        if not available:
            continue
        headings = {
            unit_id: heading_core(row_by_id[unit_id]) for unit_id in available
        }
        first_field = [
            unit_id for unit_id, heading in headings.items() if PAIR_FIELD_A in heading
        ]
        second_field = [
            unit_id for unit_id, heading in headings.items() if PAIR_FIELD_B in heading
        ]
        if not first_field or not second_field:
            continue
        field_ids = first_field + second_field
        routes.append(
            {
                "document_id": document_id,
                "standard_no": str(
                    row_by_id[field_ids[0]].get("standard_no") or ""
                ),
                "parent_clause_no": parent_clause,
                "field_a_ids": first_field,
                "field_b_ids": second_field,
                "route_reason": "explicit_standard_repeated_two_field_structure",
            }
        )
        for unit_id in field_ids:
            if unit_id not in selected:
                selected.append(unit_id)
    return {
        "triggered": bool(selected),
        "selection": "repeated_paired_field_family_reservation",
        "routes": routes,
        "selected_ids": selected,
    }


def source_structure_package(
    question: str,
    candidate_ids: list[str],
    row_by_id: Mapping[str, Mapping[str, Any]],
    child_catalog: Mapping[tuple[str, str], list[str]],
) -> dict[str, Any]:
    direct = direct_family_reservation(
        question, candidate_ids, row_by_id, child_catalog
    )
    paired = paired_field_reservation(
        question, candidate_ids, row_by_id, child_catalog
    )
    selected = list(dict.fromkeys(direct["selected_ids"] + paired["selected_ids"]))
    return {
        "triggered": bool(selected),
        "selection": "direct_family_or_repeated_paired_fields",
        "direct_family": direct,
        "paired_fields": paired,
        "selected_ids": selected,
        "selected_count": len(selected),
        "gold_used_for_selection": False,
    }


def fixed_reserved_pool(
    reservations: list[str], candidate_order: list[str]
) -> tuple[list[str], dict[str, Any]]:
    reserved = list(dict.fromkeys(reservations))
    if len(reserved) > FINAL_POOL_SIZE:
        raise RuntimeError("source reservation exceeds fixed final pool")
    order_set = set(candidate_order)
    if any(unit_id not in order_set for unit_id in reserved):
        raise RuntimeError("reservation lies outside fixed candidate union")
    chosen = set(reserved)
    for unit_id in candidate_order:
        if unit_id not in chosen:
            chosen.add(unit_id)
        if len(chosen) == FINAL_POOL_SIZE:
            break
    if len(chosen) != FINAL_POOL_SIZE:
        raise RuntimeError("candidate order cannot fill fixed final pool")
    pool = [unit_id for unit_id in candidate_order if unit_id in chosen]
    if len(pool) != FINAL_POOL_SIZE:
        raise RuntimeError("fixed final pool size changed")
    return pool, {
        "reserved_ids": reserved,
        "reserved_count": len(reserved),
        "fill_contract": "reserve_then_fill_unchanged_candidate_order",
        "final_pool_size": FINAL_POOL_SIZE,
    }


def broad_classification_question(question: str) -> bool:
    text = compact(question)
    if "阶段" in text and not any(
        anchor in text for anchor in ("分类体系", "组成体系", "类型", "类别")
    ):
        return False
    return any(pattern in text for pattern in BROAD_CLASSIFICATION_PATTERNS)


def section_key(row: Mapping[str, Any]) -> tuple[str, str] | None:
    section_path = str(row.get("section_path") or "").strip()
    if not section_path or not any(anchor in section_path for anchor in SECTION_ANCHORS):
        return None
    clause = str(row.get("clause_no") or "").strip()
    if not EXPLICIT_CLAUSE.fullmatch(clause):
        return None
    return str(row["document_id"]), section_path


def relation_role(row: Mapping[str, Any]) -> str | None:
    text = compact(str(row.get("citation_text") or ""))
    if CLASSIFICATION_RELATION.search(text):
        return "classification_or_hierarchy"
    if CONVERSION_RELATION.search(text):
        return "source_to_target_estimation"
    return None


def section_catalog(
    rows: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str], list[str]], dict[str, int]]:
    catalog: dict[tuple[str, str], list[str]] = {}
    source_order: dict[str, int] = {}
    for index, row in enumerate(rows):
        unit_id = row["retrieval_unit_id"]
        source_order[unit_id] = index
        if not eligible(row):
            continue
        key = section_key(row)
        if key:
            catalog.setdefault(key, []).append(unit_id)
    return catalog, source_order


def classification_completion(
    case: Mapping[str, Any],
    pool_ids: list[str],
    row_by_id: Mapping[str, Mapping[str, Any]],
    catalog: Mapping[tuple[str, str], list[str]],
) -> dict[str, Any]:
    if not broad_classification_question(str(case["question"])):
        return {
            "triggered": False,
            "selection": "non_broad_classification_question_identity",
            "eligible_sections": [],
            "selected": [],
            "selected_ids": [],
            "gold_used_for_selection": False,
        }
    seed_by_section: dict[tuple[str, str], list[str]] = {}
    for unit_id in pool_ids:
        row = row_by_id.get(unit_id)
        key = section_key(row) if row else None
        if key:
            seed_by_section.setdefault(key, []).append(unit_id)
    eligible_sections = {
        key: seed_ids
        for key, seed_ids in seed_by_section.items()
        if len({row_by_id[unit_id]["clause_no"] for unit_id in seed_ids}) >= 2
    }
    present = set(pool_ids)
    selected: list[dict[str, Any]] = []
    for key, seed_ids in eligible_sections.items():
        for unit_id in catalog.get(key, []):
            role = relation_role(row_by_id[unit_id])
            if unit_id in present or unit_id not in case["candidate_ids"] or role is None:
                continue
            selected.append(
                {
                    "unit_id": unit_id,
                    "relation_role": role,
                    "document_id": key[0],
                    "section_path": key[1],
                    "clause_no": row_by_id[unit_id].get("clause_no"),
                    "seed_ids": seed_ids,
                    "inside_frozen_candidate_union": True,
                }
            )
            present.add(unit_id)
    return {
        "triggered": bool(selected),
        "selection": "broad_question_plus_multi_clause_classification_section_relation",
        "eligible_sections": [
            {"document_id": key[0], "section_path": key[1], "seed_ids": seed_ids}
            for key, seed_ids in eligible_sections.items()
        ],
        "selected": selected,
        "selected_ids": [row["unit_id"] for row in selected],
        "selected_count": len(selected),
        "gold_used_for_selection": False,
    }


def application_branch(row: Mapping[str, Any]) -> str | None:
    return source_branch(row)


def unpressed_investigation_question(question: str) -> bool:
    text = compact(question)
    return any(anchor in text for anchor in UNPRESSED_QUESTION_ANCHORS) and any(
        anchor in text for anchor in UNPRESSED_CONTEXT_ANCHORS
    )


def dzt0479_assessment_branch(row: Mapping[str, Any]) -> bool:
    if (
        row.get("standard_no") != "DZ/T 0479-2024"
        or row.get("title") != "压覆矿产资源调查评估规范"
    ):
        return False
    clause = str(row.get("clause_no") or "").strip()
    return (
        clause == "7"
        or clause.startswith("7.")
        or clause == "8.2"
        or clause == "附录C"
        or clause.startswith("C.")
    )


def generic_forbidden_reason(
    question: str, row: Mapping[str, Any]
) -> str | None:
    selected_branch = explicit_material_branch(question)
    row_branch = application_branch(row)
    if selected_branch and row_branch and row_branch != selected_branch:
        return f"application_sibling_branch:{row_branch}"
    if unpressed_investigation_question(question) and dzt0479_assessment_branch(row):
        return "dzt0479_assessment_sibling_branch"
    return None


def raw_eligible_order(
    candidate_order: list[str],
    candidate_ids: list[str],
    row_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    return [
        unit_id
        for unit_id in dict.fromkeys(candidate_order + candidate_ids)
        if eligible(row_by_id[unit_id])
    ]


def clean_order(
    question: str,
    ordered: list[str],
    row_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[list[str], list[dict[str, str]]]:
    allowed = []
    quarantined = []
    for unit_id in ordered:
        reason = generic_forbidden_reason(question, row_by_id[unit_id])
        if reason:
            quarantined.append({"unit_id": unit_id, "reason": reason})
        else:
            allowed.append(unit_id)
    if len(allowed) < FINAL_POOL_SIZE:
        raise RuntimeError("T068 clean candidate order shorter than 20")
    return allowed, quarantined


def application_list_package(
    question: str, manifests: Mapping[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    branch = explicit_material_branch(question)
    if branch is None:
        return {
            "triggered": False,
            "branch": None,
            "selected_ids": [],
            "selection": "non_explicit_application_material_question",
        }
    text = compact(question)
    if any(anchor in text for anchor in CONDITIONAL_MATERIAL_SUBSTITUTION_ANCHORS):
        return {
            "triggered": False,
            "branch": branch,
            "selected_ids": [],
            "selection": "conditional_material_substitution_not_full_list",
        }
    selected = [row["unit_id"] for row in manifests[branch]]
    return {
        "triggered": True,
        "branch": branch,
        "selected_ids": selected,
        "selected_count": len(selected),
        "selection": "explicit_question_to_complete_authoritative_source_branch",
        "gold_used_for_selection": False,
    }


def appendix_child_catalog(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str], list[str]]:
    catalog: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in rows:
        clause = str(row.get("clause_no") or "").strip()
        match = APPENDIX_CHILD.fullmatch(clause)
        if not match or not eligible(row):
            continue
        parent = f"{match.group(1)}.{match.group(2)}"
        catalog[(row["document_id"], parent)].append(row["retrieval_unit_id"])
    return dict(catalog)


def appendix_attachment_package(
    question: str,
    original_candidate_ids: list[str],
    row_by_id: Mapping[str, Mapping[str, Any]],
    catalog: Mapping[tuple[str, str], list[str]],
    rows_by_document: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    text = compact(question)
    if not (
        "附录" in text
        and "附件" in text
        and "材料" in text
        and any(anchor in text for anchor in ("哪些", "列明", "清单"))
    ):
        return {
            "triggered": False,
            "routes": [],
            "selected_ids": [],
            "selection": "non_appendix_attachment_list_question",
        }
    original_set = set(original_candidate_ids)
    question_domains = [
        anchor for anchor in APPENDIX_DOMAIN_ANCHORS if anchor in text
    ]
    routes = []
    selected = []
    for (document_id, parent), child_ids in catalog.items():
        if len(child_ids) < 3 or not original_set.intersection(child_ids):
            continue
        children_text = compact(
            "\n".join(
                str(row_by_id[unit_id].get("citation_text") or "")
                for unit_id in child_ids
            )
        )
        if "附件" not in children_text or "材料" not in children_text:
            continue
        document_text = compact(
            "\n".join(
                str(row.get("title") or "")
                + "\n"
                + str(row.get("citation_text") or "")
                for row in rows_by_document[document_id]
            )
        )
        required_specific_domain = next(
            (anchor for anchor in ("不作为压覆", "未压覆") if anchor in text), None
        )
        if required_specific_domain and required_specific_domain not in document_text:
            continue
        if (
            not required_specific_domain
            and question_domains
            and not any(anchor in document_text for anchor in question_domains)
        ):
            continue
        letter = parent.split(".", 1)[0]
        appendix_clause = f"附录{letter}"
        appendix_titles = [
            row["retrieval_unit_id"]
            for row in rows_by_document[document_id]
            if eligible(row)
            and str(row.get("clause_no") or "").strip() == appendix_clause
        ]
        cross_references = [
            row["retrieval_unit_id"]
            for row in rows_by_document[document_id]
            if eligible(row)
            and f"参见{appendix_clause}"
            in compact(str(row.get("citation_text") or ""))
        ]
        package = [
            unit_id
            for unit_id in dict.fromkeys(
                cross_references + appendix_titles + child_ids
            )
            if generic_forbidden_reason(question, row_by_id[unit_id]) is None
        ]
        if not package:
            continue
        conditional_context_ids: list[str] = []
        if any(anchor in text for anchor in CONDITIONAL_APPLICABILITY_ANCHORS):
            child_han_texts = [
                re.sub(
                    r"[^一-龥]",
                    "",
                    str(row_by_id[unit_id].get("citation_text") or ""),
                )
                for unit_id in child_ids
            ]
            material_anchors = set()
            for child_text in child_han_texts:
                for suffix in MATERIAL_REFERENCE_SUFFIXES:
                    search_from = 0
                    while (end := child_text.find(suffix, search_from)) >= 0:
                        end += len(suffix)
                        for length in range(5, min(9, end) + 1):
                            material_anchors.add(child_text[end - length : end])
                        search_from = end
            conditional_context_ids = [
                row["retrieval_unit_id"]
                for row in rows_by_document[document_id]
                if eligible(row)
                and row["retrieval_unit_id"] not in package
                and generic_forbidden_reason(question, row) is None
                and any(
                    marker in compact(str(row.get("citation_text") or ""))
                    for marker in CONDITIONAL_SOURCE_MARKERS
                )
                and any(
                    anchor
                    in re.sub(
                        r"[^一-龥]", "", str(row.get("citation_text") or "")
                    )
                    for anchor in material_anchors
                )
            ]
            package.extend(conditional_context_ids)
        routes.append(
            {
                "document_id": document_id,
                "parent": parent,
                "direct_child_count": len(child_ids),
                "appendix_title_ids": appendix_titles,
                "cross_reference_ids": cross_references,
                "conditional_context_ids": conditional_context_ids,
                "package_ids": package,
                "route_reason": "explicit_appendix_attachment_list_plus_present_child",
            }
        )
        selected.extend(package)
    selected = list(dict.fromkeys(selected))
    return {
        "triggered": bool(selected),
        "routes": routes,
        "selected_ids": selected,
        "selected_count": len(selected),
        "selection": "source_appendix_cross_reference_title_and_direct_child_family",
        "gold_used_for_selection": False,
    }


def matching_governed_families(
    question: str, families: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    text = compact(question)
    return [
        family
        for family in families
        if any(variant in text for variant in family["variants"])
    ]


def governed_concept_authority_protection(
    question: str,
    candidate_ids: list[str],
    base_ids: list[str],
    row_by_id: Mapping[str, Mapping[str, Any]],
    families: list[dict[str, Any]],
) -> dict[str, Any]:
    text = compact(question)
    matched_families = matching_governed_families(question, families)
    has_review_concept = any(value in text for value in REVIEW_CONCEPT_VARIANTS)
    if (
        not matched_families
        or not has_review_concept
        or not any(anchor in text for anchor in NORMATIVE_REQUEST_ANCHORS)
        or explicit_material_branch(question) is not None
    ):
        return {
            "triggered": False,
            "matched_family_ids": [family["family_id"] for family in matched_families],
            "selected_ids": [],
            "selection": "governed_multi_concept_normative_route_not_applicable",
        }
    base_set = set(base_ids)
    matches = []
    for position, unit_id in enumerate(candidate_ids, start=1):
        row = row_by_id[unit_id]
        if not eligible(row) or row.get("document_type") not in NORMATIVE_HIGH_AUTHORITY_TYPES:
            continue
        row_text = compact(str(row.get("citation_text") or ""))
        family_hits = [
            family["family_id"]
            for family in matched_families
            if any(variant in row_text for variant in family["variants"])
        ]
        review_hit = any(value in row_text for value in REVIEW_CONCEPT_VARIANTS)
        if len(family_hits) != len(matched_families) or not review_hit:
            continue
        matches.append(
            {
                "unit_id": unit_id,
                "document_type": row.get("document_type"),
                "candidate_position": position,
                "family_hits": family_hits,
                "review_concept_hit": True,
            }
        )
    matches.sort(
        key=lambda row: (
            0
            if row["document_type"]
            in {"law", "regulation", "administrative_regulation"}
            else 1,
            row["candidate_position"],
            row["unit_id"],
        )
    )
    selected = [
        row["unit_id"]
        for row in matches[:1]
        if row["unit_id"] not in base_set
    ]
    return {
        "triggered": bool(selected),
        "matched_family_ids": [family["family_id"] for family in matched_families],
        "review_concept_variants": list(REVIEW_CONCEPT_VARIANTS),
        "matches": matches,
        "selected_ids": selected,
        "selected_count": len(selected),
        "selection": "all_query_concepts_plus_high_authority_normative_candidate",
        "gold_used_for_selection": False,
    }


def detailed_classification_question(question: str) -> bool:
    text = compact(question)
    return (
        any(anchor in text for anchor in DETAILED_CLASSIFICATION_SCOPE_ANCHORS)
        and any(
            anchor in text for anchor in DETAILED_CLASSIFICATION_CONDITION_ANCHORS
        )
        and any(
            anchor in text for anchor in DETAILED_CLASSIFICATION_RELATION_ANCHORS
        )
    )


def document_question_score(question: str, row: Mapping[str, Any]) -> int:
    text = compact(question)
    title = compact(str(row.get("title") or ""))
    standard_no = compact(str(row.get("standard_no") or ""))
    score = 0
    if standard_no and standard_no in text:
        score += 100
    if title and title in text:
        score += 80
    subject = title
    for generic in GENERIC_STANDARD_TITLE_PARTS:
        subject = subject.replace(generic, "")
    subject_anchors = [
        value
        for value in re.split(r"[、，,（）()/]+", subject)
        if len(value) >= 2
    ]
    score += 10 * sum(anchor in text for anchor in subject_anchors)
    return score


def detailed_classification_package(
    question: str,
    pool_ids: list[str],
    candidate_ids: list[str],
    row_by_id: Mapping[str, Mapping[str, Any]],
    catalog: Mapping[tuple[str, str], list[str]],
) -> dict[str, Any]:
    if not detailed_classification_question(question):
        return {
            "applicable": False,
            "triggered": False,
            "selection": "non_detailed_classification_question",
            "eligible_sections": [],
            "selected_ids": [],
            "gold_used_for_selection": False,
        }
    candidate_set = set(candidate_ids)
    pool_set = set(pool_ids)
    eligible_sections = []
    for key, section_ids in catalog.items():
        in_candidate = [
            unit_id for unit_id in section_ids if unit_id in candidate_set
        ]
        seed_ids = [unit_id for unit_id in in_candidate if unit_id in pool_set]
        if len(seed_ids) < 2 or len(in_candidate) < 3:
            continue
        score = document_question_score(question, row_by_id[in_candidate[0]])
        if score <= 0:
            continue
        eligible_sections.append(
            {
                "document_id": key[0],
                "section_path": key[1],
                "document_score": score,
                "seed_ids": seed_ids,
                "package_ids": in_candidate,
            }
        )
    if not eligible_sections:
        return {
            "applicable": True,
            "triggered": False,
            "selection": "detailed_classification_no_unambiguous_source_section",
            "eligible_sections": [],
            "selected_ids": [],
            "gold_used_for_selection": False,
        }
    highest_score = max(row["document_score"] for row in eligible_sections)
    matched = [
        row for row in eligible_sections if row["document_score"] == highest_score
    ]
    if len(matched) != 1:
        return {
            "applicable": True,
            "triggered": False,
            "selection": "detailed_classification_ambiguous_source_section",
            "eligible_sections": eligible_sections,
            "selected_ids": [],
            "gold_used_for_selection": False,
        }
    selected = matched[0]["package_ids"]
    return {
        "applicable": True,
        "triggered": bool(selected),
        "selection": "detailed_question_plus_named_source_full_classification_section",
        "eligible_sections": eligible_sections,
        "selected_ids": selected,
        "selected_count": len(selected),
        "already_present_count": sum(unit_id in pool_set for unit_id in selected),
        "gold_used_for_selection": False,
    }


def fixed_pool(reservations: list[str], order: list[str]) -> list[str]:
    pool, _ = fixed_reserved_pool(reservations, order)
    return pool


def _generic_structural_fixed20_runner(
    *,
    question: str,
    original_candidate_ids: list[str],
    original_candidate_order: list[str],
    row_by_id: Mapping[str, Mapping[str, Any]],
    child_catalog: Mapping[tuple[str, str], list[str]],
    appendix_catalog: Mapping[tuple[str, str], list[str]],
    rows_by_document: Mapping[str, list[dict[str, Any]]],
    list_manifests: Mapping[str, list[dict[str, Any]]],
    gap_families: list[dict[str, Any]],
    governed_families: list[dict[str, Any]],
    classification_catalog: Mapping[tuple[str, str], list[str]],
) -> dict[str, Any]:
    raw_order = raw_eligible_order(
        original_candidate_order, original_candidate_ids, row_by_id
    )
    if len(raw_order) < FINAL_POOL_SIZE:
        raise RuntimeError("T068 raw candidate order shorter than 20")
    raw_pool = raw_order[:FINAL_POOL_SIZE]

    source_family_trace = minimal_source_family_completion(
        question, original_candidate_ids, row_by_id, child_catalog
    )
    appendix_trace = appendix_attachment_package(
        question,
        original_candidate_ids,
        row_by_id,
        appendix_catalog,
        rows_by_document,
    )
    list_trace = application_list_package(question, list_manifests)
    expanded_candidate_ids = list(
        dict.fromkeys(
            original_candidate_ids
            + source_family_trace["selected_ids"]
            + appendix_trace["selected_ids"]
            + list_trace["selected_ids"]
        )
    )
    expanded_order = raw_eligible_order(
        original_candidate_order, expanded_candidate_ids, row_by_id
    )
    branch_clean_order, quarantined = clean_order(
        question, expanded_order, row_by_id
    )
    branch_clean_pool = branch_clean_order[:FINAL_POOL_SIZE]

    selection_case = {
        "id": "generic_runtime",
        "question": question,
        "candidate_ids": set(branch_clean_order),
    }
    gap_trace = gap_completion(
        selection_case,
        branch_clean_pool
        + list_trace["selected_ids"]
        + appendix_trace["selected_ids"],
        gap_families,
        row_by_id,
    )
    concept_trace = governed_concept_authority_protection(
        question,
        branch_clean_order,
        branch_clean_pool,
        row_by_id,
        governed_families,
    )
    seed_pool = list(
        dict.fromkeys(
            branch_clean_pool
            + list_trace["selected_ids"]
            + gap_trace["selected_ids"]
            + appendix_trace["selected_ids"]
            + concept_trace["selected_ids"]
        )
    )
    detailed_trace = detailed_classification_package(
        question,
        seed_pool,
        branch_clean_order,
        row_by_id,
        classification_catalog,
    )
    classification_trace = (
        detailed_trace
        if detailed_trace["applicable"]
        else classification_completion(
            selection_case,
            seed_pool,
            row_by_id,
            classification_catalog,
        )
    )
    source_structure_trace = source_structure_package(
        question, branch_clean_order, row_by_id, child_catalog
    )
    reservations = list(
        dict.fromkeys(
            list_trace["selected_ids"]
            + gap_trace["selected_ids"]
            + appendix_trace["selected_ids"]
            + concept_trace["selected_ids"]
            + classification_trace["selected_ids"]
            + source_structure_trace["selected_ids"]
        )
    )
    if len(reservations) > FINAL_POOL_SIZE:
        raise RuntimeError("T068 generic reservation exceeds fixed20")
    final_candidate_ids = list(
        dict.fromkeys(expanded_candidate_ids + reservations)
    )
    final_candidate_order = raw_eligible_order(
        branch_clean_order, final_candidate_ids, row_by_id
    )
    final_candidate_order, final_quarantined = clean_order(
        question, final_candidate_order, row_by_id
    )
    forbidden_reservations = [
        unit_id
        for unit_id in reservations
        if unit_id not in set(final_candidate_order)
    ]
    if forbidden_reservations:
        raise RuntimeError(
            "T068 structural reservation conflicts with branch quarantine: "
            + ",".join(forbidden_reservations)
        )
    full_pool = fixed_pool(reservations, final_candidate_order)
    return {
        "pools": {
            ARMS[0]: raw_pool,
            ARMS[1]: branch_clean_pool,
            ARMS[2]: full_pool,
        },
        "candidate_ids": final_candidate_order,
        "candidate_order": final_candidate_order,
        "reservations": reservations,
        "traces": {
            "branch_quarantine": {
                "triggered": bool(quarantined or final_quarantined),
                "quarantined": quarantined + final_quarantined,
                "selected_ids": [],
            },
            "application_list": list_trace,
            "intervening_gap": gap_trace,
            "appendix_attachment": appendix_trace,
            "governed_concept_authority": concept_trace,
            "classification_section": classification_trace,
            "t064_candidate_completion": source_family_trace,
            "t065_source_structure": source_structure_trace,
        },
        "selection_contract": {
            "case_id_read": False,
            "dataset_split_read": False,
            "gold_read": False,
            "target_rank_read": False,
            "expected_evidence_id_read": False,
        },
    }


def _t090_fixed20_runner(**kwargs: Any) -> dict[str, Any]:
    base_runtime = _generic_structural_fixed20_runner(**kwargs)
    try:
        return apply_to_fixed20_runtime(
            question=str(kwargs.get("question") or ""),
            base_runtime=base_runtime,
            row_by_id=kwargs["row_by_id"],
            fixed_pool=fixed_pool,
            base_pool_key=BASE_POOL_KEY,
        )
    except Exception as exc:
        return fail_closed_runtime(base_runtime, reason=type(exc).__name__)


_VERIFIED_DECISION_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "t094_verified_decision_context", default=None
)


def bind_verified_decision(
    decision_question: str, technical_decision: Any | None
) -> Token:
    return _VERIFIED_DECISION_CONTEXT.set(
        {
            "decision_question": decision_question,
            "technical_decision": technical_decision,
        }
    )


def reset_verified_decision(token: Token) -> None:
    _VERIFIED_DECISION_CONTEXT.reset(token)


def _with_trace(
    runtime: Mapping[str, Any], trace: Mapping[str, Any]
) -> dict[str, Any]:
    result = dict(runtime)
    traces = deepcopy(dict(runtime.get("traces") or {}))
    traces[TRACE_KEY] = dict(trace)
    result["traces"] = traces
    return result


def _fail_closed_trace(reason: str) -> dict[str, Any]:
    return {
        "schema_version": RULE_SCHEMA_VERSION,
        "algorithm_snapshot_id": ALGORITHM_SNAPSHOT_ID,
        "triggered": False,
        "selection": "t092_rule_error_fail_closed",
        "error_type": reason,
        "operator": None,
        "evidence_refs": [],
        "selected_ids": [],
        "roles": {},
        "candidate_only": True,
        "candidate_rescored": False,
        "gold_used_for_selection": False,
        "case_id_used_for_selection": False,
    }


def apply_t092_reservation(
    *,
    question: str,
    base_runtime: Mapping[str, Any],
    row_by_id: Mapping[str, Mapping[str, Any]],
    technical_decision: Any | None = None,
    decision_is_bound: bool = False,
) -> dict[str, Any]:
    pools = base_runtime.get("pools")
    if not isinstance(pools, Mapping) or BASE_POOL_KEY not in pools:
        raise ValueError("T090 runtime does not contain the fixed20 pool")
    base_pool = list(pools[BASE_POOL_KEY])
    candidate_order = list(base_runtime.get("candidate_order") or [])
    existing_reservations = list(base_runtime.get("reservations") or [])
    if len(base_pool) != FINAL_POOL_SIZE or len(set(base_pool)) != FINAL_POOL_SIZE:
        raise ValueError("T090 Final is not exact20")
    if len(candidate_order) < FINAL_POOL_SIZE or len(set(candidate_order)) != len(
        candidate_order
    ):
        raise ValueError("T090 Candidate order is invalid")
    candidate_set = set(candidate_order)
    if any(
        identifier not in candidate_set
        for identifier in (*base_pool, *existing_reservations)
    ):
        raise ValueError("T090 pool or reservation lies outside Candidate")
    if fixed_pool(existing_reservations, candidate_order) != base_pool:
        raise ValueError("T090 Final is not reproducible")

    trace = deterministic_technical_sufficiency_plan(
        question,
        candidate_order,
        row_by_id,
        technical_decision=technical_decision,
        decision_is_bound=decision_is_bound,
    )
    if not trace.get("triggered"):
        return _with_trace(base_runtime, trace)
    selected_ids = list(trace.get("selected_ids") or [])
    if not selected_ids or any(
        identifier not in candidate_set for identifier in selected_ids
    ):
        raise ValueError("T092 selected evidence outside Candidate")
    reservations = list(
        dict.fromkeys((*existing_reservations, *selected_ids))
    )
    if len(reservations) > FINAL_POOL_SIZE:
        return _with_trace(
            base_runtime,
            {
                **trace,
                "triggered": False,
                "selection": "combined_reservations_exceed_fixed20_fail_closed",
                "selected_ids": [],
                "reservation_overflow_count": len(reservations),
            },
        )
    final_pool = fixed_pool(reservations, candidate_order)
    if (
        len(final_pool) != FINAL_POOL_SIZE
        or len(set(final_pool)) != FINAL_POOL_SIZE
        or any(identifier not in candidate_set for identifier in final_pool)
    ):
        raise ValueError("T092 fixed20 invariant failed")
    result = _with_trace(base_runtime, trace)
    result_pools = dict(pools)
    result_pools[BASE_POOL_KEY] = final_pool
    result["pools"] = result_pools
    result["reservations"] = reservations
    return result


def generic_fixed20_runner(**kwargs: Any) -> dict[str, Any]:
    base_runtime = _t090_fixed20_runner(**kwargs)
    verified_context = _VERIFIED_DECISION_CONTEXT.get()
    decision_is_bound = verified_context is not None
    decision_question = (
        str(verified_context["decision_question"])
        if verified_context is not None
        else str(kwargs.get("question") or "")
    )
    technical_decision = (
        verified_context.get("technical_decision")
        if verified_context is not None
        else None
    )
    try:
        return apply_t092_reservation(
            question=decision_question,
            base_runtime=base_runtime,
            row_by_id=kwargs["row_by_id"],
            technical_decision=technical_decision,
            decision_is_bound=decision_is_bound,
        )
    except Exception as exc:
        return _with_trace(base_runtime, _fail_closed_trace(type(exc).__name__))


# Compatibility facade used by the unchanged, already-accepted Store search
# methods.  It exposes only production primitives; every object is defined in
# ``src/mining_qa`` and none lazily imports an experiment script.
t029 = SimpleNamespace(
    FtsSearcher=retrieval.FtsSearcher,
    rerank_fts_candidates=retrieval.rerank_fts_candidates,
)
t039 = SimpleNamespace(apply_head_admission=retrieval.apply_head_admission)
t036 = SimpleNamespace(DashscopeNativeClient=retrieval.DashscopeNativeClient)
t063 = SimpleNamespace(
    DENSE_HEAD_DEPTH=retrieval.DENSE_HEAD_DEPTH,
    DENSE_TOP_K=retrieval.DENSE_TOP_K,
    LEXICAL_HEAD_DEPTH=retrieval.LEXICAL_HEAD_DEPTH,
    LEXICAL_TOP_K=retrieval.LEXICAL_TOP_K,
    DashscopeNativeClient=retrieval.DashscopeNativeClient,
    FullScanSearcher=retrieval.FullScanSearcher,
    RankedUnit=retrieval.RankedUnit,
    Unit=retrieval.Unit,
    apply_head_admission=retrieval.apply_head_admission,
    equal_rrf=retrieval.equal_rrf,
    load_retrieval_units=retrieval.load_retrieval_units,
    ordered_documents=retrieval.ordered_documents,
    t029=t029,
    t036=t036,
    t039=t039,
)
t055 = SimpleNamespace(
    explicit_family_catalog=explicit_family_catalog,
    gap_completion=gap_completion,
)
t058 = SimpleNamespace(
    list_manifests=list_manifests,
    source_branch=source_branch,
    t055=t055,
)
t061 = SimpleNamespace(
    classification_completion=classification_completion,
    section_catalog=section_catalog,
    t058=t058,
)
t064 = SimpleNamespace(
    direct_child_catalog=direct_child_catalog,
    minimal_source_family_completion=minimal_source_family_completion,
)

# Flat aliases consumed by the Store.
LEXICAL_TOP_K = retrieval.LEXICAL_TOP_K
DENSE_TOP_K = retrieval.DENSE_TOP_K
STAGE1_DOCUMENT_COUNT = retrieval.STAGE1_DOCUMENT_COUNT
retrieve_candidate_frontier = retrieval.retrieve_candidate_frontier


__all__ = (
    "ALGORITHM_SNAPSHOT_ID",
    "ARMS",
    "BASE_POOL_KEY",
    "SCHEMA_VERSION",
    "TASK_ID",
    "appendix_child_catalog",
    "apply_t092_reservation",
    "bind_verified_decision",
    "generic_fixed20_runner",
    "load_governed_families",
    "read_rows",
    "reset_verified_decision",
    "retrieve_candidate_frontier",
    "t061",
    "t063",
    "t064",
)
