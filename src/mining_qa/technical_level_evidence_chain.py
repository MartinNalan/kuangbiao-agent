from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any

from . import query_decision
from .technical_test_hierarchy import (
    MINERAL_PROCESSING_TEST_LEVELS,
    TechnicalTestLevel,
)


SCHEMA_VERSION = "geowiki-technical-level-evidence-chain.v1"
ALGORITHM_SNAPSHOT_ID = "t090-technical-level-chain-v1"
GOVERNED_AXIS = "mineral_processing_test_level"
GOVERNED_STANDARD_NO = "DZ/T 0340-2020"
FINAL_POOL_SIZE = 20
TRACE_KEY = "technical_level_evidence_chain"


@dataclass(frozen=True)
class TechnicalRequirementPath:
    path_id: str
    axis: str
    required_value: str
    status: str


@dataclass(frozen=True)
class TechnicalSufficiencyDecision:
    """A narrow source-governed answer contract for level/option questions."""

    actual_level_key: str
    requirement_clause: str
    operator: str
    paths: tuple[TechnicalRequirementPath, ...]
    overall_status: str
    evidence_refs: tuple[tuple[str, str], ...]
    condition_triggered: bool | None = None
    relation_basis: str = ""

# Only these two adjacent relations have an explicit source-text bridge in the
# governed standard.  Clause numbering or the rank registry alone never
# creates a production coverage relation.
GOVERNED_ADJACENT_EDGES = {
    frozenset(("selectability", "laboratory_flow")): {
        "supporting_level": "laboratory_flow",
        "markers": ("实验室流程试验", "可选性试验", "基础上"),
    },
    frozenset(("laboratory_flow", "laboratory_expanded_continuous")): {
        "supporting_level": "laboratory_expanded_continuous",
        "markers": ("扩大连续试验", "实验室流程试验", "推荐"),
    },
}

SUFFICIENCY_PATTERNS = (
    re.compile(r"(?:能否|是否|可否|能不能).{0,16}(?:满足|达到|覆盖|替代|视为|符合)"),
    re.compile(r"(?:满足|达到|覆盖|替代|视为|符合).{0,16}(?:要求|条件|等级)"),
    re.compile(r"(?:等级|研究程度).{0,12}(?:能否|是否).{0,12}(?:满足|达到|覆盖)"),
)
ACTUAL_PREFIX = re.compile(
    r"(?:已|已经|只|仅|我已|我只)?(?:选择并)?(?:完成|做了|作了|进行过|开展过|实施了)$"
)
REQUIRED_PREFIX = re.compile(
    r"(?:最低)?(?:要求|应做|应进行|应完成|需要|需要进行|需要完成|需做|需进行|必须达到|要求达到|必要时进行)$"
)


def compact(value: Any) -> str:
    return "".join(str(value or "").split())


def _level_names(level: TechnicalTestLevel) -> tuple[str, ...]:
    return tuple(dict.fromkeys((level.label, *level.aliases)))


def _all_level_occurrences(
    question: str,
) -> list[tuple[TechnicalTestLevel, int, int, str]]:
    """Return the longest non-overlapping governed level mentions."""
    text = compact(question)
    raw: list[tuple[TechnicalTestLevel, int, int, str]] = []
    for level in MINERAL_PROCESSING_TEST_LEVELS:
        for name in _level_names(level):
            cursor = 0
            while True:
                start = text.find(name, cursor)
                if start < 0:
                    break
                raw.append((level, start, start + len(name), name))
                cursor = start + 1
    raw.sort(key=lambda item: (item[1], -(item[2] - item[1]), item[0].rank))
    return [
        item
        for item in raw
        if not any(
            other[1] <= item[1]
            and other[2] >= item[2]
            and (other[2] - other[1]) > (item[2] - item[1])
            for other in raw
        )
    ]


def _near_prefix(text: str, start: int, pattern: re.Pattern[str]) -> bool:
    prefix = text[max(0, start - 14) : start]
    prefix = re.split(r"[；;。！？?，,：:]", prefix)[-1]
    return bool(pattern.search(prefix))


def _contains_governed_or_alternative(question: str) -> bool:
    """Reject an explicit OR between two governed test levels."""
    text = compact(question)
    occurrences = _all_level_occurrences(text)
    for left in occurrences:
        for right in occurrences:
            if left[0].key == right[0].key or left[1] >= right[1]:
                continue
            between = text[left[2] : right[1]]
            if len(between) <= 8 and "或" in between:
                return True
    return False


def parse_explicit_level_comparison(question: str) -> dict[str, Any]:
    """Parse only an explicit, same-axis sufficiency comparison."""
    text = compact(question)
    base = {
        "applicable": False,
        "axis": GOVERNED_AXIS,
        "source_standard_no": GOVERNED_STANDARD_NO,
        "actual_level_key": None,
        "required_level_key": None,
    }
    if not any(pattern.search(text) for pattern in SUFFICIENCY_PATTERNS):
        return {**base, "reason": "no_explicit_sufficiency_relation"}
    if _contains_governed_or_alternative(text):
        return {**base, "reason": "governed_levels_are_explicit_or_alternatives"}

    actual: list[TechnicalTestLevel] = []
    required: list[TechnicalTestLevel] = []
    for level, start, _, _ in _all_level_occurrences(text):
        if _near_prefix(text, start, ACTUAL_PREFIX):
            actual.append(level)
        if _near_prefix(text, start, REQUIRED_PREFIX):
            required.append(level)
    actual = list(dict.fromkeys(actual))
    required = list(dict.fromkeys(required))
    if len(actual) != 1 or len(required) != 1:
        return {
            **base,
            "reason": "completed_and_required_levels_not_both_unambiguous",
            "actual_level_candidates": [item.key for item in actual],
            "required_level_candidates": [item.key for item in required],
        }
    if actual[0].key == required[0].key:
        return {
            **base,
            "reason": "same_level_conformance_is_not_hierarchy_comparison",
            "actual_level_key": actual[0].key,
            "required_level_key": required[0].key,
        }
    edge = GOVERNED_ADJACENT_EDGES.get(
        frozenset((actual[0].key, required[0].key))
    )
    if edge is None:
        return {
            **base,
            "reason": "level_pair_is_not_a_governed_source_edge",
            "actual_level_key": actual[0].key,
            "required_level_key": required[0].key,
        }
    return {
        **base,
        "applicable": True,
        "reason": "explicit_same_governed_axis_sufficiency_comparison",
        "actual_level_key": actual[0].key,
        "required_level_key": required[0].key,
        "actual_level_clause": actual[0].source_clause,
        "required_level_clause": required[0].source_clause,
        "governed_edge_supporting_level": edge["supporting_level"],
    }


def _level_by_key(key: str) -> TechnicalTestLevel:
    for level in MINERAL_PROCESSING_TEST_LEVELS:
        if level.key == key:
            return level
    raise KeyError(key)


def level_by_key(key: str) -> TechnicalTestLevel:
    """Return a governed level without exposing the mutable registry internals."""
    return _level_by_key(key)


def _row_text(row: Mapping[str, Any]) -> str:
    return compact(row.get("citation_text") or row.get("search_text") or "")


def _row_is_eligible(row: Mapping[str, Any]) -> bool:
    return bool(row.get("search_eligible") and row.get("citation_eligible"))


def _single_slot_value(slot: Any) -> str | None:
    values = tuple(getattr(slot, "values", ()) or ())
    state = getattr(slot, "state", None)
    if state != query_decision.SlotState.KNOWN or len(values) != 1:
        return None
    return str(values[0])


def _controlled_short_scale(question: str) -> str | None:
    matches = re.findall(
        r"(小型|中型|大型)(?:较易选|易选|难选)(?:矿石|矿砂)", compact(question)
    )
    values = {
        {"小型": "small", "中型": "medium", "大型": "large"}[label]
        for label in matches
    }
    return next(iter(values)) if len(values) == 1 else None


def truth_table_requirement_clause(question: str) -> dict[str, Any]:
    stage = _single_slot_value(query_decision.extract_stage(question))
    scale = _single_slot_value(
        query_decision.extract_resource_scale(
            question,
            allow_bare_gold_fallback=False,
        )
    ) or _controlled_short_scale(question)
    ore = _single_slot_value(query_decision.extract_ore_selectability(question))
    if not stage or not scale or not ore:
        return {
            "resolved": False,
            "reason": "truth_table_slots_not_unique",
            "stage": stage,
            "resource_scale": scale,
            "ore_selectability": ore,
            "clause_no": None,
        }
    clause = query_decision.TECHNICAL_STAGE_TRUTH_TABLE.get((stage, scale, ore))
    if not clause:
        return {
            "resolved": False,
            "reason": "truth_table_has_no_direct_branch",
            "stage": stage,
            "resource_scale": scale,
            "ore_selectability": ore,
            "clause_no": None,
        }
    return {
        "resolved": True,
        "reason": "unique_query_decision_truth_table_branch",
        "stage": stage,
        "resource_scale": scale,
        "ore_selectability": ore,
        "clause_no": clause,
    }


def compile_technical_sufficiency_decision(
    question: str,
) -> TechnicalSufficiencyDecision | None:
    """Compile a deterministic evidence/answer contract for governed cases.

    This deliberately covers only the two source-verified adjacent level
    relations accepted by T089 and the explicit 6.5.4 alternative wording.
    It does not infer higher-level coverage from numeric clause order.
    """
    text = compact(question)
    truth_branch = truth_table_requirement_clause(question)
    if not truth_branch["resolved"]:
        return None
    requirement_clause = str(truth_branch["clause_no"])

    occurrences = _all_level_occurrences(text)
    actual = list(
        dict.fromkeys(
            level
            for level, start, _, _ in occurrences
            if _near_prefix(text, start, ACTUAL_PREFIX)
        )
    )

    # Clause 6.5.4 expressly lists semi-industrial and industrial tests as
    # alternatives when the additional-test condition applies.  Matching a
    # listed option is not a hierarchy-coverage conclusion.
    explicit_options = (
        "半工业试验" in text
        and "工业试验" in text
        and bool(re.search(r"半工业试验.{0,3}或.{0,3}工业试验", text))
    )
    if requirement_clause == "6.5.4" and explicit_options:
        if len(actual) != 1 or actual[0].key not in {"semi_industrial", "industrial"}:
            return None
        condition_triggered = bool(
            "必要时" in text
            and re.search(r"(?:进入|已触发|条件已满足|已选择并完成|选择并完成)", text)
        )
        if not condition_triggered:
            return None
        selected = actual[0]
        return TechnicalSufficiencyDecision(
            actual_level_key=selected.key,
            requirement_clause=requirement_clause,
            operator="one_of",
            paths=(
                TechnicalRequirementPath(
                    path_id="semi_industrial_route",
                    axis="mineral_processing_test_option",
                    required_value="semi_industrial",
                    status=(
                        "satisfied"
                        if selected.key == "semi_industrial"
                        else "alternative_not_selected"
                    ),
                ),
                TechnicalRequirementPath(
                    path_id="industrial_route",
                    axis="mineral_processing_test_option",
                    required_value="industrial",
                    status=(
                        "satisfied"
                        if selected.key == "industrial"
                        else "alternative_not_selected"
                    ),
                ),
            ),
            overall_status="satisfied",
            evidence_refs=(
                (GOVERNED_STANDARD_NO, requirement_clause),
                (GOVERNED_STANDARD_NO, selected.source_clause),
            ),
            condition_triggered=True,
            relation_basis="explicit_or_alternative_not_hierarchy_coverage",
        )

    comparison = parse_explicit_level_comparison(question)
    if not comparison["applicable"]:
        return None
    actual_level = _level_by_key(str(comparison["actual_level_key"]))
    required_level = _level_by_key(str(comparison["required_level_key"]))
    evidence_refs = (
        (GOVERNED_STANDARD_NO, requirement_clause),
        (GOVERNED_STANDARD_NO, actual_level.source_clause),
        (GOVERNED_STANDARD_NO, required_level.source_clause),
    )

    physical_alternative = bool(
        requirement_clause == "6.5.4"
        and "或" in text
        and any(term in text for term in ("物化详测", "物化性能详细测试研究", "物化性能详测"))
    )
    level_status = (
        "satisfied" if actual_level.rank >= required_level.rank else "not_satisfied"
    )
    if physical_alternative:
        return TechnicalSufficiencyDecision(
            actual_level_key=actual_level.key,
            requirement_clause=requirement_clause,
            operator="any_of",
            paths=(
                TechnicalRequirementPath(
                    path_id="expanded_continuous_route",
                    axis=GOVERNED_AXIS,
                    required_value=required_level.key,
                    status=level_status,
                ),
                TechnicalRequirementPath(
                    path_id="physical_property_route",
                    axis="physical_property_study_level",
                    required_value="detailed_test_research",
                    status="not_evidenced",
                ),
            ),
            overall_status=(
                "satisfied" if level_status == "satisfied" else "not_satisfied"
            ),
            evidence_refs=evidence_refs,
            relation_basis=(
                "source_verified_test_edge_and_independent_physical_property_axis"
            ),
        )

    return TechnicalSufficiencyDecision(
        actual_level_key=actual_level.key,
        requirement_clause=requirement_clause,
        operator="single",
        paths=(
            TechnicalRequirementPath(
                path_id="test_level_route",
                axis=GOVERNED_AXIS,
                required_value=required_level.key,
                status=level_status,
            ),
        ),
        overall_status=level_status,
        evidence_refs=evidence_refs,
        relation_basis="source_verified_adjacent_test_level_edge",
    )


def _edge_is_source_verified(
    actual: TechnicalTestLevel,
    required: TechnicalTestLevel,
    definition_rows: Mapping[str, Mapping[str, Any]],
) -> bool:
    edge = GOVERNED_ADJACENT_EDGES.get(frozenset((actual.key, required.key)))
    if not edge:
        return False
    supporting = definition_rows.get(str(edge["supporting_level"]))
    if not supporting:
        return False
    supporting_text = _row_text(supporting)
    return all(compact(marker) in supporting_text for marker in edge["markers"])


def deterministic_level_chain_plan(
    question: str,
    candidate_order: Sequence[str],
    row_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Select the governed three-role package inside an existing Candidate."""
    comparison = parse_explicit_level_comparison(question)
    trace: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "algorithm_snapshot_id": ALGORITHM_SNAPSHOT_ID,
        **comparison,
        "triggered": False,
        "selection": comparison["reason"],
        "candidate_only": True,
        "candidate_rescored": False,
        "gold_used_for_selection": False,
        "case_id_used_for_selection": False,
        "selected_ids": [],
        "roles": {},
    }
    if not comparison["applicable"]:
        return trace

    candidate = list(dict.fromkeys(candidate_order))
    if len(candidate) != len(candidate_order):
        return {**trace, "selection": "duplicate_candidate_order_rejected"}
    if any(unit_id not in row_by_id for unit_id in candidate):
        return {**trace, "selection": "candidate_row_missing_rejected"}

    actual = _level_by_key(str(comparison["actual_level_key"]))
    required = _level_by_key(str(comparison["required_level_key"]))
    truth_branch = truth_table_requirement_clause(question)
    trace["truth_table_branch"] = truth_branch
    if not truth_branch["resolved"]:
        return {**trace, "selection": truth_branch["reason"]}

    requirement_matches = [
        unit_id
        for unit_id in candidate
        if _row_is_eligible(row_by_id[unit_id])
        and row_by_id[unit_id].get("standard_no") == GOVERNED_STANDARD_NO
        and str(row_by_id[unit_id].get("clause_no") or "").strip()
        == truth_branch["clause_no"]
    ]
    trace["eligible_requirement_ids"] = requirement_matches
    if not requirement_matches:
        return {**trace, "selection": "no_candidate_requirement_clause"}
    if len(requirement_matches) != 1:
        return {**trace, "selection": "ambiguous_candidate_requirement_clause"}

    requirement_id = requirement_matches[0]
    requirement_row = row_by_id[requirement_id]
    if not any(
        compact(name) in _row_text(requirement_row)
        for name in _level_names(required)
    ):
        return {
            **trace,
            "selection": "truth_table_clause_does_not_state_required_level",
        }
    document_id = requirement_row.get("document_id")

    def definition_id(level: TechnicalTestLevel) -> str | None:
        matches = [
            unit_id
            for unit_id in candidate
            if _row_is_eligible(row_by_id[unit_id])
            and row_by_id[unit_id].get("standard_no") == GOVERNED_STANDARD_NO
            and row_by_id[unit_id].get("document_id") == document_id
            and str(row_by_id[unit_id].get("clause_no") or "").strip()
            == level.source_clause
        ]
        return matches[0] if len(matches) == 1 else None

    actual_id = definition_id(actual)
    required_id = definition_id(required)
    if not actual_id or not required_id:
        return {
            **trace,
            "selection": "candidate_missing_unambiguous_level_definition",
        }
    definition_rows = {
        actual.key: row_by_id[actual_id],
        required.key: row_by_id[required_id],
    }
    if not _edge_is_source_verified(actual, required, definition_rows):
        return {
            **trace,
            "selection": "governed_edge_source_markers_not_verified",
        }

    roles = {
        "requirement_clause": [requirement_id],
        "actual_level_definition": [actual_id],
        "required_level_definition": [required_id],
    }
    selected = list(
        dict.fromkeys(value for values in roles.values() for value in values)
    )
    return {
        **trace,
        "triggered": True,
        "selection": "candidate_three_role_level_chain_reserved",
        "source_document_id": document_id,
        "roles": roles,
        "selected_ids": selected,
    }


def _with_trace(base_runtime: Mapping[str, Any], trace: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base_runtime)
    traces = deepcopy(dict(base_runtime.get("traces") or {}))
    traces[TRACE_KEY] = dict(trace)
    result["traces"] = traces
    return result


def apply_to_fixed20_runtime(
    *,
    question: str,
    base_runtime: Mapping[str, Any],
    row_by_id: Mapping[str, Mapping[str, Any]],
    fixed_pool: Callable[[Sequence[str], Sequence[str]], list[str]],
    base_pool_key: str,
) -> dict[str, Any]:
    """Apply the T090 reservation and otherwise return the base Final unchanged."""
    pools = base_runtime.get("pools")
    if not isinstance(pools, Mapping) or base_pool_key not in pools:
        raise ValueError("base runtime does not contain the fixed20 pool")
    base_pool = list(pools[base_pool_key])
    candidate_order = list(base_runtime.get("candidate_order") or [])
    existing_reservations = list(base_runtime.get("reservations") or [])
    if len(base_pool) != FINAL_POOL_SIZE or len(set(base_pool)) != FINAL_POOL_SIZE:
        raise ValueError("base runtime Final is not exact20")
    if len(candidate_order) < FINAL_POOL_SIZE or len(set(candidate_order)) != len(
        candidate_order
    ):
        raise ValueError("base runtime Candidate order is invalid")
    candidate_set = set(candidate_order)
    if any(
        unit_id not in candidate_set
        for unit_id in (*base_pool, *existing_reservations)
    ):
        raise ValueError("base runtime pool or reservation lies outside Candidate")
    if fixed_pool(existing_reservations, candidate_order) != base_pool:
        raise ValueError("base runtime Final is not reproducible")

    trace = deterministic_level_chain_plan(question, candidate_order, row_by_id)
    if not trace["triggered"]:
        return _with_trace(base_runtime, trace)

    reservations = list(
        dict.fromkeys(existing_reservations + list(trace["selected_ids"]))
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
        or any(unit_id not in candidate_set for unit_id in final_pool)
    ):
        raise ValueError("T090 fixed20 invariant failed")

    result = _with_trace(base_runtime, trace)
    result_pools = dict(pools)
    result_pools[base_pool_key] = final_pool
    result["pools"] = result_pools
    result["reservations"] = reservations
    return result


def fail_closed_runtime(
    base_runtime: Mapping[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    """Attach a body-free audit reason while retaining the base pool exactly."""
    return _with_trace(
        base_runtime,
        {
            "schema_version": SCHEMA_VERSION,
            "algorithm_snapshot_id": ALGORITHM_SNAPSHOT_ID,
            "triggered": False,
            "selection": "t090_rule_error_fail_closed",
            "error_type": reason,
            "candidate_only": True,
            "candidate_rescored": False,
            "gold_used_for_selection": False,
            "case_id_used_for_selection": False,
            "selected_ids": [],
            "roles": {},
        },
    )
