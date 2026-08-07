from __future__ import annotations

"""Source-governed technical-sufficiency decisions for the T092 runtime.

The frozen T090 compiler remains authoritative for its accepted ``single``
and ``any_of`` decisions.  Its broad ``one_of`` branch is never reused here:
T092 recompiles that branch through the stricter T091 v3 fact, target, source,
and citation checks.

This module intentionally stores only standard/clause identifiers and SHA-256
bindings.  Normative sentences remain in the private governed corpus.
"""

from collections.abc import Mapping, Sequence
import hashlib
import json
import re
from typing import Any
import unicodedata

from .technical_level_evidence_chain import (
    TechnicalRequirementPath,
    TechnicalSufficiencyDecision,
    compile_technical_sufficiency_decision as _compile_t090_decision,
    deterministic_level_chain_plan as _deterministic_t090_plan,
    truth_table_requirement_clause,
)


ALGORITHM_SNAPSHOT_ID = "t092-technical-level-chain-explicit-alternative-v2"
RULE_SCHEMA_VERSION = "geowiki-technical-sufficiency-decision-t092.v1"
TRACE_KEY = "technical_sufficiency_decision_t092"

GOVERNED_STANDARD_NO = "DZ/T 0340-2020"
GOVERNED_REQUIREMENT_CLAUSE = "6.5.4"
GOVERNED_REQUIREMENT_CITATION_SHA256 = (
    "c402804f0eee6f226ddfdae9b145a32233eddd0f0d6e09327f55ed1289c8ee0a"
)
OPTION_CONFIG: dict[str, dict[str, str]] = {
    "semi_industrial": {
        "label": "半工业试验",
        "clause": "5.2.4",
        "citation_text_sha256": (
            "000d438d50d54d39a3fcaa4017b3827a8e99f1e710b24ebe37987372c465d40a"
        ),
    },
    "industrial": {
        "label": "工业试验",
        "clause": "5.2.5",
        "citation_text_sha256": (
            "6810c10e4e94502bd6c97e4d9a501173810fb23345c78c21ec40621132d915c9"
        ),
    },
}


def _compact(value: Any) -> str:
    return "".join(str(value or "").split())


def _normalize_question_text(value: Any) -> str:
    return _compact(
        unicodedata.normalize("NFKC", str(value or ""))
        .replace("—", "-")
        .replace("–", "-")
    )


def _value(item: Any, *names: str) -> Any:
    for name in names:
        if isinstance(item, Mapping) and name in item:
            value = item.get(name)
        else:
            value = getattr(item, name, None)
        if value is not None:
            return value
    return None


def _items(values: Mapping[str, Any] | Sequence[Any]) -> list[Any]:
    if isinstance(values, Mapping):
        if any(
            key in values
            for key in ("standard_no", "clause_no", "chapter", "citation_text", "quote")
        ):
            return [values]
        return list(values.values())
    return list(values)


def _row_is_eligible(row: Mapping[str, Any]) -> bool:
    return bool(row.get("search_eligible") and row.get("citation_eligible"))


_STANDARD_NO_PATTERN = re.compile(
    r"(?<![A-Z0-9])(?:[A-Z]{1,6}(?:/[A-Z]{1,6})?)"
    r"\s*\d+(?:\.\d+)*-\d{4}(?!\d)",
    flags=re.IGNORECASE,
)
_OPTION_PATTERN = r"(?:半工业试验|工业试验)"
_MULTIPLE_OPTION_PATTERN = (
    r"(?:半工业试验(?:、|,|/|和|及|以及|与|或|或者)工业试验|"
    r"工业试验(?:、|,|/|和|及|以及|与|或|或者)半工业试验)"
)
_FACT_PREFIX_FORBIDDEN = re.compile(
    r"(?:如果|若|假如|假设|一旦|计划|拟|准备|将要|预计|打算|可能|"
    r"声称|据称|宣称|自称|被认为|假定|设想|尚不能确认|不能确认|"
    r"无法确认|未证实|未经证实|未核实|未经核实|没有证据|缺乏证据|"
    r"有待确认|待确认|不确定|不清楚|存疑|基本|大体|大致|原则上|"
    r"并非|并不是|不代表|否认|否定|不承认|是否)"
)
_FACT_SUFFIX_CONFLICT = re.compile(
    r"(?:但|不过|然而|可|经核查|实际|事实上)?.{0,12}"
    r"(?:并未完成|未完成|没有完成|不能认定|不能确认|无法确认|"
    r"尚待核实|有待核实|待核实|未经核实|尚不清楚|不属实|"
    r"尚未全部完成|未全部完成|只做了?一部分|仅做了?一部分|"
    r"不算完成|不符合.{0,8}(?:定义|要求|条件)|"
    r"(?:试验|结果|结论)?.{0,6}(?:被认定无效|无效|作废|撤销))"
)
_CONDITION_SUFFIX_CONFLICT = re.compile(
    r"(?:但|不过|然而|可|经核查|复核后|实际|事实上)?.{0,16}"
    r"(?:并未确认(?:需要追加试验)?|未确认(?:需要追加试验)?|"
    r"没有确认(?:需要追加试验)?|不能认定(?:需要追加试验)?|"
    r"不能确认(?:需要追加试验)?|无法确认(?:需要追加试验)?|"
    r"(?:该)?条件未触发|尚未触发|不需要追加试验|"
    r"(?:追加试验|该条件|必要性).{0,5}"
    r"(?:尚待核实|有待核实|待核实|未经核实|尚不清楚))"
)
_COMPOUND_OBJECT_SUFFIX = re.compile(
    r"^(?:的)?(?:报告|报告编制|方案|计划|准备|前期|部分|一部分|"
    r"大部分|主体|主要|相关|阶段|阶段性|资料|材料|申请|合同|样品|"
    r"设计|论证|设备|场地|手续|工作|项目|验收|(?:约)?\d+(?:\.\d+)?%)"
)


def _major_segment(text: str, start: int, end: int) -> tuple[str, str]:
    left = max((text.rfind(mark, 0, start) for mark in ";。!?"), default=-1) + 1
    right_positions = [
        position
        for mark in ";。!?"
        if (position := text.find(mark, end)) >= 0
    ]
    right = min(right_positions) if right_positions else len(text)
    return text[left:start], text[end:right]


def _direct_fact_match(
    text: str, match: re.Match[str], *, fact_kind: str
) -> bool:
    prefix, suffix = _major_segment(text, match.start(), match.end())
    if fact_kind == "condition":
        prefix = prefix.rsplit(",", 1)[-1]
    if _FACT_PREFIX_FORBIDDEN.search(prefix):
        return False
    conflict = (
        _CONDITION_SUFFIX_CONFLICT
        if fact_kind == "condition"
        else _FACT_SUFFIX_CONFLICT
    )
    if conflict.search(suffix):
        return False
    return not re.match(r"^(?:了)?吗", suffix)


def _completion_match_is_direct(text: str, match: re.Match[str]) -> bool:
    if not _direct_fact_match(text, match, fact_kind="completion"):
        return False
    prefix, _ = _major_segment(text, match.start(), match.end())
    local_prefix = prefix.rsplit(",", 1)[-1]
    if not local_prefix:
        return True
    if re.fullmatch(
        r"(?:(?:并|且|同时)|(?:本项目|该项目|本工程|企业|我方|矿山|申请人)|"
        r"(?:经核实|经确认))*",
        local_prefix,
    ):
        return True
    return bool(
        re.search(
            r"(?:已确认|已认定|条件已触发).{0,16}"
            r"(?:需要追加试验|追加试验条件)?(?:并|且|同时)$",
            local_prefix,
        )
    )


def _completion_boundary_is_valid(text: str, match: re.Match[str]) -> bool:
    suffix = text[match.end() :]
    return not suffix or bool(
        re.match(r"^(?:[,;。!?]|但|不过|然而|后|并|且|同时)", suffix)
    )


def _completion_claim(text: str) -> dict[str, Any]:
    affirmative_prefix = (
        r"(?:现已|已经|已|确已)(?:分别)?(?:选择并)?(?:完成|做完)(?:了)?"
    )
    affirmative_suffix = r"(?:现已|已经|已|确已)(?:完成|做完)(?:了)?"
    pair_patterns = (
        rf"{affirmative_prefix}{_MULTIPLE_OPTION_PATTERN}",
        rf"{_MULTIPLE_OPTION_PATTERN}(?:均|都|两项均)?{affirmative_suffix}",
        rf"{affirmative_prefix}(?:半工业试验|工业试验),?"
        rf"(?:并且|且|同时)(?:也)?(?:已|已经)?(?:完成|做完)(?:了)?"
        rf"(?:半工业试验|工业试验)",
    )
    for pattern in pair_patterns:
        match = re.search(pattern, text)
        if (
            match
            and _completion_match_is_direct(text, match)
            and _completion_boundary_is_valid(text, match)
        ):
            return {
                "status": "multiple_completed_options",
                "options": ["semi_industrial", "industrial"],
            }

    candidates: list[str] = []
    patterns = (
        re.compile(rf"{affirmative_prefix}(?P<option>{_OPTION_PATTERN})"),
        re.compile(rf"(?P<option>{_OPTION_PATTERN}){affirmative_suffix}"),
    )
    key_by_label = {
        str(config["label"]): key for key, config in OPTION_CONFIG.items()
    }
    for pattern in patterns:
        for match in pattern.finditer(text):
            if not _completion_match_is_direct(text, match):
                continue
            if not _completion_boundary_is_valid(text, match):
                continue
            option_end = match.end("option")
            _, after_option = _major_segment(text, option_end, option_end)
            if _COMPOUND_OBJECT_SUFFIX.match(after_option):
                continue
            if _COMPOUND_OBJECT_SUFFIX.match(text[match.end() :]):
                continue
            candidates.append(key_by_label[match.group("option")])
    selected = list(dict.fromkeys(candidates))
    if len(selected) == 1:
        return {"status": "affirmed_unique", "options": selected}
    if len(selected) > 1:
        return {"status": "multiple_completed_options", "options": selected}
    return {"status": "completion_not_affirmed", "options": []}


def _condition_claim(text: str) -> dict[str, str]:
    patterns = (
        r"(?:按|按照|依据|依照|参照|根据|基于)第?6\.5\.4条"
        r"(?:已经|已)?进入必要时(?:追加)?试验(?:环节|情形)?",
        r"(?:已经|已)进入必要时(?:追加)?试验(?:环节|情形)?",
        r"(?:已经|已|经)(?:明确)?(?:确认|认定|判定|确定)"
        r"(?:属于)?(?:需要|应当|应|须|需)(?:进行|进入)?"
        r"(?:必要时)?追加试验(?:的)?(?:情形|环节|条件)?",
        r"(?:必要时|追加试验)(?:的)?(?:条件|情形)?"
        r"(?:已经|已)(?:触发|成立|满足|确认)",
        r"经(?:专家)?评审(?:确认|认定)(?:属于)?(?:需要|应当|应|须|需)"
        r"(?:进行)?(?:必要时)?追加试验",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            if _direct_fact_match(text, match, fact_kind="condition"):
                return {"status": "affirmed"}
    return {"status": "condition_not_affirmed"}


def _sufficiency_segments(text: str) -> list[str]:
    segments = [segment for segment in re.split(r"[;。!?]", text) if segment]
    patterns = (
        r"(?:是否|能否|可否|能不能|可不可以).{0,48}(?:满足|符合|达到|命中)",
        r"(?:满足|符合|达到|命中).{0,16}(?:吗|么)$",
    )
    return [
        segment
        for segment in segments
        if any(re.search(pattern, segment) for pattern in patterns)
    ]


def _alternative_target(text: str) -> dict[str, Any]:
    pair_patterns = (
        r"半工业试验(?:或|或者)工业试验",
        r"工业试验(?:或|或者)半工业试验",
        r"半工业试验(?:、|/|和|及|以及|与)工业试验"
        r"(?:任选其一|任选一项|两者择一|二选一)",
        r"工业试验(?:、|/|和|及|以及|与)半工业试验"
        r"(?:任选其一|任选一项|两者择一|二选一)",
    )
    target_segments = _sufficiency_segments(text)
    if len(target_segments) != 1:
        return {"status": "sufficiency_target_not_unique"}
    target = target_segments[0]
    interrogative_count = len(
        re.findall(r"(?:是否|能否|可否|能不能|可不可以)", target)
    )
    if interrogative_count > 1 or re.search(
        r"(?:同时|并且|另外|以及).{0,24}(?:满足|符合|达到|可靠|有效|合规)",
        target,
    ):
        return {"status": "multiple_question_targets_out_of_scope"}
    if re.search(
        r"(?:是否|能否|可否|能不能|可不可以).{0,4}(?:不|未)"
        r"(?:满足|符合|达到|命中)",
        target,
    ):
        return {"status": "negated_sufficiency_question_out_of_scope"}
    whole_qualifier = re.search(
        r"(?:全部|所有|整体|整条|各项|完全|全面|全都|总体|整个|全套)",
        target,
    )
    if whole_qualifier and re.search(
        r"(?:第?6\.5\.4条|要求|规定|工作)", target
    ):
        return {"status": "whole_clause_sufficiency_out_of_scope"}
    if re.search(r"(?:扩大连续试验|物化性能|物化详测|理化性能)", target):
        return {"status": "other_654_path_targeted"}
    explicit_pair = any(re.search(pattern, target) for pattern in pair_patterns)
    membership_target = bool(
        re.search(
            r"(?:选择性|选择要求|选择关系|选项|任选|二选一|择一|其中之一)",
            target,
        )
    )
    clause_selection = bool(
        re.search(r"第?6\.5\.4条", target)
        and re.search(
            r"(?:追加试验.{0,8}(?:选项|选择)|任选(?:项)?|选择性要求|选择要求)",
            target,
        )
    )
    if not ((explicit_pair and membership_target) or clause_selection):
        return {"status": "alternative_target_not_explicit"}
    return {"status": "explicit_one_of_membership", "segment": target}


def parse_explicit_alternative_query(question: str) -> dict[str, Any]:
    """Parse only the T091-reviewed explicit one-of membership relation."""
    text = _normalize_question_text(question)
    base: dict[str, Any] = {
        "schema_version": RULE_SCHEMA_VERSION,
        "applicable": False,
        "source_standard_no": GOVERNED_STANDARD_NO,
        "requirement_clause": GOVERNED_REQUIREMENT_CLAUSE,
        "selected_option": None,
        "selected_definition_clause": None,
    }
    truth = truth_table_requirement_clause(question)
    base["truth_table_branch"] = truth
    expected_slots = {
        "stage": "exploration",
        "resource_scale": "large",
        "ore_selectability": "difficult",
        "clause_no": GOVERNED_REQUIREMENT_CLAUSE,
    }
    if not truth.get("resolved") or any(
        truth.get(key) != value for key, value in expected_slots.items()
    ):
        return {**base, "reason": "not_large_difficult_exploration_branch"}

    explicit_standards = {
        _compact(value).upper() for value in _STANDARD_NO_PATTERN.findall(text)
    }
    governed = _compact(GOVERNED_STANDARD_NO).upper()
    if explicit_standards and explicit_standards != {governed}:
        return {
            **base,
            "reason": "explicit_standard_set_ambiguous_or_other",
            "explicit_standards": sorted(explicit_standards),
        }
    if re.search(r"《[^》]{2,80}》", text):
        return {**base, "reason": "titled_source_out_of_scope"}

    explicit_matrix_clauses = set(re.findall(r"第?(6\.\d+\.\d+)条", text))
    if explicit_matrix_clauses and explicit_matrix_clauses != {
        GOVERNED_REQUIREMENT_CLAUSE
    }:
        return {**base, "reason": "explicit_requirement_clause_mismatch"}
    condition_clause_anchors = set(
        re.findall(
            r"(?:按|按照|依据|依照|参照|根据|基于)"
            r"(?:[A-Z/]+\d+(?:\.\d+)*-\d{4})?"
            r"第?(\d+(?:\.\d+){0,2})条.{0,24}"
            r"(?:进入|确认|认定|判定|触发|需要|追加试验)",
            text,
            flags=re.IGNORECASE,
        )
    )
    if condition_clause_anchors and condition_clause_anchors != {
        GOVERNED_REQUIREMENT_CLAUSE
    }:
        return {
            **base,
            "reason": "condition_source_clause_mismatch",
            "condition_clause_anchors": sorted(condition_clause_anchors),
        }
    if re.search(
        r"(?:高于|覆盖|包含|包涵|涵盖|替代|代替|取代|顶替|优于|"
        r"等同于|相当于|(?:等级|级别|层级).{0,5}(?:更高|高于|高低)|"
        r"(?:更高|高级|低级).{0,4}(?:等级|级别|层级|试验))",
        text,
    ):
        return {**base, "reason": "hierarchy_or_substitution_out_of_scope"}

    target = _alternative_target(text)
    base["target_claim"] = target
    if target["status"] != "explicit_one_of_membership":
        return {**base, "reason": target["status"]}
    condition = _condition_claim(text)
    base["condition_claim"] = condition
    if condition["status"] != "affirmed":
        return {**base, "reason": condition["status"]}
    completion = _completion_claim(text)
    base["completion_claim"] = completion
    if completion["status"] != "affirmed_unique":
        return {
            **base,
            "reason": completion["status"],
            "completed_option_candidates": completion["options"],
        }

    option_key = str(completion["options"][0])
    option = OPTION_CONFIG[option_key]
    return {
        **base,
        "applicable": True,
        "reason": "explicit_alternative_selected_option_membership",
        "selected_option": option_key,
        "selected_option_label": option["label"],
        "selected_definition_clause": option["clause"],
        "condition_triggered": True,
        "relation_type": "one_of_membership_not_hierarchy",
    }


def _strict_one_of_decision(parsed: Mapping[str, Any]) -> TechnicalSufficiencyDecision:
    selected = str(parsed["selected_option"])
    option = OPTION_CONFIG[selected]
    paths = tuple(
        TechnicalRequirementPath(
            path_id=f"{key}_route",
            axis="mineral_processing_test_option",
            required_value=key,
            status="satisfied" if key == selected else "alternative_not_selected",
        )
        for key in ("semi_industrial", "industrial")
    )
    return TechnicalSufficiencyDecision(
        actual_level_key=selected,
        requirement_clause=GOVERNED_REQUIREMENT_CLAUSE,
        operator="one_of",
        paths=paths,
        overall_status="satisfied",
        evidence_refs=(
            (GOVERNED_STANDARD_NO, GOVERNED_REQUIREMENT_CLAUSE),
            (GOVERNED_STANDARD_NO, option["clause"]),
        ),
        condition_triggered=True,
        relation_basis="explicit_or_alternative_not_hierarchy_coverage",
    )


def compile_technical_sufficiency_decision_t092(
    question: str,
) -> TechnicalSufficiencyDecision | None:
    """Return T090 single/any_of decisions or a strict T092 one_of decision."""
    parsed = parse_explicit_alternative_query(question)
    if parsed["applicable"]:
        return _strict_one_of_decision(parsed)

    frozen = _compile_t090_decision(question)
    if frozen is not None and frozen.operator in {"single", "any_of"}:
        return frozen
    # In particular, never fall back to T090's broader one_of parser.
    return None


def decision_payload(decision: TechnicalSufficiencyDecision) -> dict[str, Any]:
    """Return the stable, text-free payload used across service boundaries."""
    return {
        "schema_version": RULE_SCHEMA_VERSION,
        "actual_level_key": decision.actual_level_key,
        "requirement_clause": decision.requirement_clause,
        "operator": decision.operator,
        "paths": [
            {
                "path_id": path.path_id,
                "axis": path.axis,
                "required_value": path.required_value,
                "status": path.status,
            }
            for path in decision.paths
        ],
        "overall_status": decision.overall_status,
        "evidence_refs": [list(ref) for ref in decision.evidence_refs],
        "condition_triggered": decision.condition_triggered,
        "relation_basis": decision.relation_basis,
    }


def decision_sha256(decision: TechnicalSufficiencyDecision) -> str:
    """Hash the stable decision payload without including a query or body text."""
    encoded = json.dumps(
        decision_payload(decision),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _expected_hashes_for_decision(
    decision: TechnicalSufficiencyDecision,
) -> dict[tuple[str, str], str] | None:
    if decision.operator != "one_of":
        return None
    option = OPTION_CONFIG.get(decision.actual_level_key)
    expected_refs = (
        (GOVERNED_STANDARD_NO, GOVERNED_REQUIREMENT_CLAUSE),
        (GOVERNED_STANDARD_NO, str(option["clause"])) if option else ("", ""),
    )
    if option is None or decision.evidence_refs != expected_refs:
        return None
    return {
        expected_refs[0]: GOVERNED_REQUIREMENT_CITATION_SHA256,
        expected_refs[1]: option["citation_text_sha256"],
    }


def _citation_text(item: Any) -> str | None:
    value = _value(item, "citation_text", "quote", "evidence_text", "text")
    return value if isinstance(value, str) else None


def _citation_matches(item: Any, expected: str) -> bool:
    text = _citation_text(item)
    if text is None or hashlib.sha256(text.encode("utf-8")).hexdigest() != expected:
        return False
    declared = _value(item, "citation_text_sha256")
    return declared in (None, "", expected)


def decision_evidence_is_source_verified(
    decision: TechnicalSufficiencyDecision,
    evidence_rows: Mapping[str, Any] | Sequence[Any],
) -> bool:
    """Verify the two source rows required by a strict one_of decision.

    Candidate rows may provide their declared ``citation_text_sha256``. Public
    hit/Source mappings need not expose that private field; their quote is
    re-hashed against the governed constant instead. Non-one_of decisions are
    outside this T092 verifier and return ``False``.
    """
    expected = _expected_hashes_for_decision(decision)
    if expected is None:
        return False
    items = _items(evidence_rows)
    matches: dict[tuple[str, str], list[Any]] = {ref: [] for ref in expected}
    for item in items:
        standard_no = _compact(_value(item, "standard_no")).upper()
        clause = str(
            _value(item, "clause_no", "chapter", "section_path") or ""
        ).strip()
        ref = (standard_no, clause)
        normalized_expected = next(
            (
                candidate
                for candidate in expected
                if _compact(candidate[0]).upper() == ref[0] and candidate[1] == ref[1]
            ),
            None,
        )
        if normalized_expected is not None:
            matches[normalized_expected].append(item)
    if any(len(rows) != 1 for rows in matches.values()):
        return False
    selected = [rows[0] for rows in matches.values()]
    document_ids = {str(_value(item, "document_id") or "") for item in selected}
    if len(document_ids) != 1 or "" in document_ids:
        return False
    return all(
        _citation_matches(matches[ref][0], expected_hash)
        for ref, expected_hash in expected.items()
    )


def deterministic_technical_sufficiency_plan(
    question: str,
    candidate_order: Sequence[str],
    row_by_id: Mapping[str, Mapping[str, Any]],
    *,
    technical_decision: TechnicalSufficiencyDecision | None = None,
    decision_is_bound: bool = False,
) -> dict[str, Any]:
    """Create a JSON-safe, candidate-only plan for the T092 runner.

    ``decision_is_bound`` distinguishes an explicitly verified
    ``not_applicable`` Decision (``technical_decision is None``) from the
    historical direct-runner call shape, which compiles from ``question``.
    The HTTP runtime binds the Decision reconstructed at its trust boundary so
    retrieval rewrites cannot reinterpret the user's original question.
    """
    parsed = parse_explicit_alternative_query(question)
    decision = (
        technical_decision
        if decision_is_bound
        else compile_technical_sufficiency_decision_t092(question)
    )
    trace: dict[str, Any] = {
        "schema_version": RULE_SCHEMA_VERSION,
        "algorithm_snapshot_id": ALGORITHM_SNAPSHOT_ID,
        "triggered": False,
        "selection": parsed.get("reason", "no_governed_decision"),
        "operator": decision.operator if decision else None,
        "evidence_refs": (
            [list(ref) for ref in decision.evidence_refs] if decision else []
        ),
        "selected_ids": [],
        "roles": {},
        "selected_option": parsed.get("selected_option"),
        "decision_sha256": decision_sha256(decision) if decision else None,
        "candidate_only": True,
        "candidate_rescored": False,
        "gold_used_for_selection": False,
        "case_id_used_for_selection": False,
    }
    if decision is None:
        return trace
    if decision_is_bound and decision.operator == "one_of":
        if not parsed.get("applicable") or _strict_one_of_decision(parsed) != decision:
            return {
                **trace,
                "selection": "bound_one_of_decision_question_mismatch",
            }
    if decision.operator in {"single", "any_of"}:
        frozen_decision = _compile_t090_decision(question)
        if frozen_decision != decision or frozen_decision.operator != decision.operator:
            return {
                **trace,
                "selection": "frozen_t090_decision_mismatch",
            }
        frozen_plan = _deterministic_t090_plan(
            question, candidate_order, row_by_id
        )
        if not frozen_plan.get("triggered"):
            return {
                **trace,
                "selection": str(
                    frozen_plan.get("selection")
                    or "frozen_t090_plan_not_triggered"
                ),
                "base_algorithm_snapshot_id": frozen_plan.get(
                    "algorithm_snapshot_id"
                ),
            }
        selected_ids = list(frozen_plan.get("selected_ids") or [])
        if (
            not selected_ids
            or len(selected_ids) != len(set(selected_ids))
            or any(identifier not in row_by_id for identifier in selected_ids)
        ):
            return {
                **trace,
                "selection": "frozen_t090_plan_selected_ids_invalid",
            }
        selected_refs = tuple(
            (
                str(row_by_id[identifier].get("standard_no") or ""),
                str(row_by_id[identifier].get("clause_no") or "").strip(),
            )
            for identifier in selected_ids
        )
        if selected_refs != decision.evidence_refs:
            return {
                **trace,
                "selection": "frozen_t090_plan_refs_mismatch",
                "base_algorithm_snapshot_id": frozen_plan.get(
                    "algorithm_snapshot_id"
                ),
            }
        roles = frozen_plan.get("roles")
        if not isinstance(roles, Mapping):
            return {
                **trace,
                "selection": "frozen_t090_plan_roles_invalid",
            }
        role_ids = list(
            dict.fromkeys(
                identifier
                for identifiers in roles.values()
                if isinstance(identifiers, Sequence)
                and not isinstance(identifiers, (str, bytes))
                for identifier in identifiers
            )
        )
        if role_ids != selected_ids:
            return {
                **trace,
                "selection": "frozen_t090_plan_roles_mismatch",
            }
        return {
            **trace,
            "triggered": True,
            "selection": str(frozen_plan["selection"]),
            "base_algorithm_snapshot_id": frozen_plan.get(
                "algorithm_snapshot_id"
            ),
            "source_document_id": frozen_plan.get("source_document_id"),
            "selected_ids": selected_ids,
            "roles": {str(key): list(value) for key, value in roles.items()},
        }
    if decision.operator != "one_of":
        return {**trace, "selection": "unsupported_decision_operator"}

    candidate = list(candidate_order)
    if len(candidate) != len(set(candidate)):
        return {**trace, "selection": "duplicate_candidate_order_rejected"}
    if any(identifier not in row_by_id for identifier in candidate):
        return {**trace, "selection": "candidate_row_missing_rejected"}

    requirement_ref, definition_ref = decision.evidence_refs
    requirement_matches = [
        identifier
        for identifier in candidate
        if _row_is_eligible(row_by_id[identifier])
        and row_by_id[identifier].get("standard_no") == requirement_ref[0]
        and str(row_by_id[identifier].get("clause_no") or "").strip()
        == requirement_ref[1]
    ]
    if len(requirement_matches) != 1:
        return {
            **trace,
            "selection": (
                "no_candidate_requirement_clause"
                if not requirement_matches
                else "ambiguous_candidate_requirement_clause"
            ),
        }
    requirement_id = requirement_matches[0]
    document_id = row_by_id[requirement_id].get("document_id")
    if not document_id:
        return {**trace, "selection": "requirement_document_missing"}

    definition_matches = [
        identifier
        for identifier in candidate
        if _row_is_eligible(row_by_id[identifier])
        and row_by_id[identifier].get("standard_no") == definition_ref[0]
        and row_by_id[identifier].get("document_id") == document_id
        and str(row_by_id[identifier].get("clause_no") or "").strip()
        == definition_ref[1]
    ]
    if len(definition_matches) != 1:
        return {
            **trace,
            "selection": (
                "no_candidate_selected_option_definition"
                if not definition_matches
                else "ambiguous_candidate_selected_option_definition"
            ),
        }
    definition_id = definition_matches[0]
    selected_rows = [row_by_id[requirement_id], row_by_id[definition_id]]
    if not decision_evidence_is_source_verified(decision, selected_rows):
        return {**trace, "selection": "one_of_source_evidence_not_verified"}

    roles = {
        "explicit_alternative_requirement": [requirement_id],
        "selected_option_definition": [definition_id],
    }
    return {
        **trace,
        "triggered": True,
        "selection": "candidate_explicit_alternative_package_reserved",
        "source_document_id": str(document_id),
        "selected_ids": [requirement_id, definition_id],
        "roles": roles,
    }


__all__ = (
    "ALGORITHM_SNAPSHOT_ID",
    "GOVERNED_REQUIREMENT_CITATION_SHA256",
    "GOVERNED_REQUIREMENT_CLAUSE",
    "GOVERNED_STANDARD_NO",
    "OPTION_CONFIG",
    "RULE_SCHEMA_VERSION",
    "TRACE_KEY",
    "compile_technical_sufficiency_decision_t092",
    "decision_payload",
    "decision_sha256",
    "decision_evidence_is_source_verified",
    "deterministic_technical_sufficiency_plan",
    "parse_explicit_alternative_query",
)
