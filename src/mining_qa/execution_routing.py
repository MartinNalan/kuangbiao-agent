from __future__ import annotations

from .query_understanding import QueryPlan


def requires_deep_research(plan: QueryPlan, question: str) -> bool:
    """Choose the exhaustive execution path from a validated query plan.

    The model-assisted resolution layer may select comparison/exhaustive search,
    while the explicit phrases below protect requests whose breadth must not be
    reduced by a weak or unavailable model response.
    """
    if plan.search_mode in {"comparison", "exhaustive"} or plan.exhaustive_search:
        return True
    if plan.intent in {"projection_comparison", "clause_comparison", "cross_document_audit"}:
        return True
    return any(
        term in question
        for term in (
            "逐一检查",
            "逐项对比",
            "全量检查",
            "所有标准",
            "各类标准",
            "各类规范",
            "分矿种规范",
            "哪些规范与",
            "哪些标准与",
            "是否存在不一致",
            "冲突检查",
        )
    )
