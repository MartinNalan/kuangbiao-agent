from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


DETERMINISTIC_GENERATION_REASONS = {
    "deterministic_answer_template",
    "deterministic_definition_template",
}
GOVERNED_LOCAL_SOURCE_TYPES = {
    "local_kb",
    "official_fulltext",
    "official_visual",
}


@dataclass(frozen=True)
class FastPathDecision:
    eligible: bool
    reasons: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_fast_path_shadow(
    case: dict[str, Any],
    trace: dict[str, Any],
) -> FastPathDecision:
    """Decide whether an already-run deterministic candidate was safe to expose.

    This gate deliberately receives no Gold evidence or expected answer. Gold
    may be applied only after this decision to measure false admissions.
    """

    reasons: list[str] = []
    plan = trace.get("plan") or {}
    classification = plan.get("classification") or {}
    planner = trace.get("planner") or {}
    reranker = trace.get("reranker") or {}
    generation = trace.get("generation") or {}
    limitations = case.get("limitations") or {}
    sources = case.get("sources") or []

    if case.get("status") != "answered" or not str(case.get("answer") or "").strip():
        reasons.append("candidate_did_not_produce_visible_answer")
    if not plan.get("retrieval_allowed", True):
        reasons.append("retrieval_not_allowed")
    if classification.get("missing_slots") or classification.get("ambiguities"):
        reasons.append("unresolved_question_state")
    if plan.get("authority_role_ambiguous"):
        reasons.append("authority_role_ambiguous")
    if planner.get("used"):
        reasons.append("planner_model_used")
    if reranker.get("used"):
        reasons.append("reranker_model_used")
    if generation.get("used"):
        reasons.append("answer_model_used")
    if generation.get("reason") not in DETERMINISTIC_GENERATION_REASONS:
        reasons.append("no_deterministic_renderer_contract")
    if not limitations.get("has_clause_level_evidence"):
        reasons.append("no_clause_level_evidence")
    if not sources:
        reasons.append("no_sources")

    for index, source in enumerate(sources):
        prefix = f"source_{index + 1}"
        if not source.get("chapter") or not str(source.get("quote") or "").strip():
            reasons.append(f"{prefix}_not_clause_citable")
        if source.get("effective_status") != "current":
            reasons.append(f"{prefix}_not_current")
        if source.get("validation_status") != "pass":
            reasons.append(f"{prefix}_not_governance_passed")
        if source.get("source_type") not in GOVERNED_LOCAL_SOURCE_TYPES:
            reasons.append(f"{prefix}_not_local_governed_evidence")

    return FastPathDecision(eligible=not reasons, reasons=tuple(dict.fromkeys(reasons)))
