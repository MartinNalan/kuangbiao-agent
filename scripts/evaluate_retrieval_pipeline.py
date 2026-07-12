from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.config import get_settings  # noqa: E402
from mining_qa.knowledge_store import DEFAULT_DB_PATH, KnowledgeStore  # noqa: E402
from mining_qa.query_understanding import apply_semantic_plan, understand_query  # noqa: E402
from mining_qa.retrieval_planner import RetrievalPlanner  # noqa: E402


DEFAULT_BENCHMARK = PROJECT_ROOT / "tests" / "fixtures" / "retrieval_benchmark.json"


def standard_numbers(result: dict[str, Any]) -> list[str]:
    return list(
        dict.fromkeys(
            str(hit.get("standard_no") or "")
            for hit in result.get("results") or []
            if hit.get("standard_no")
        )
    )


def expected_found(expected: list[str], actual: list[str]) -> bool:
    normalized = {value.upper().replace(" ", "") for value in actual}
    return all(value.upper().replace(" ", "") in normalized for value in expected)


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="Evaluate deterministic, rewritten, and controlled multi-query retrieval.")
    parser.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--skip-model", action="store_true")
    args = parser.parse_args()

    cases = json.loads(args.benchmark.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not 20 <= len(cases) <= 30:
        raise RuntimeError("Retrieval benchmark must contain 20 to 30 cases")

    settings = get_settings()
    planner = RetrievalPlanner(settings)
    store = KnowledgeStore(args.db)
    results = []
    for case in cases:
        question = str(case["question"])
        expected_intent = str(case["expected_intent"])
        expected = [str(value) for value in case.get("expected_standard_nos") or []]
        deterministic_plan = apply_semantic_plan(understand_query(question), None)
        deterministic = store.search(
            {
                "query": question,
                "retrieval_plan": deterministic_plan.to_payload(),
                "options": {"top_k": 20, "retrieval_round": 1},
            }
        )

        if args.skip_model:
            planned_plan = deterministic_plan
            variants = ()
            planner_used = False
            planner_error = None
        else:
            planned = await planner.plan(question, understand_query(question))
            planned_plan = planned.plan
            variants = planned.query_variants
            planner_used = planned.used
            planner_error = planned.error
        rewritten = store.search(
            {
                "query": question,
                "retrieval_plan": planned_plan.to_payload(),
                "options": {"top_k": 20, "retrieval_round": 1},
            }
        )

        merged_standards = standard_numbers(rewritten)
        variant_reports = []
        for variant in variants[:3]:
            variant_plan = replace(
                planned_plan,
                retrieval_query=variant.query,
                exhaustive_search=True,
            )
            variant_result = store.search(
                {
                    "query": question,
                    "retrieval_plan": variant_plan.to_payload(),
                    "options": {"top_k": 20, "retrieval_round": 2},
                }
            )
            variant_standards = standard_numbers(variant_result)
            merged_standards = list(dict.fromkeys((*merged_standards, *variant_standards)))
            variant_reports.append(
                {
                    "target": variant.target,
                    "new_standard_count": len(set(variant_standards) - set(standard_numbers(rewritten))),
                    "direct_evidence_hits": int(variant_result["retrieval"].get("direct_evidence_hits") or 0),
                }
            )

        deterministic_standards = standard_numbers(deterministic)
        rewritten_standards = standard_numbers(rewritten)
        results.append(
            {
                "id": case["id"],
                "intent_ok": planned_plan.intent == expected_intent,
                "deterministic_expected_found": expected_found(expected, deterministic_standards),
                "rewritten_expected_found": expected_found(expected, rewritten_standards),
                "multi_query_expected_found": expected_found(expected, merged_standards),
                "planner_used": planner_used,
                "planner_error": planner_error,
                "query_changed": planned_plan.normalized_query != deterministic_plan.normalized_query,
                "variant_count": len(variants),
                "variant_reports": variant_reports,
                "mmr_used": bool(rewritten["retrieval"].get("mmr_used")),
                "duplicate_ratio_before": rewritten["retrieval"].get("duplicate_ratio_before"),
                "duplicate_ratio_after": rewritten["retrieval"].get("duplicate_ratio_after"),
            }
        )

    await planner.llm.aclose()
    count = len(results)
    output = {
        "ok": all(item["intent_ok"] and item["multi_query_expected_found"] for item in results),
        "case_count": count,
        "intent_accuracy": round(sum(item["intent_ok"] for item in results) / count, 4),
        "deterministic_expected_recall": round(sum(item["deterministic_expected_found"] for item in results) / count, 4),
        "rewritten_expected_recall": round(sum(item["rewritten_expected_found"] for item in results) / count, 4),
        "multi_query_expected_recall": round(sum(item["multi_query_expected_found"] for item in results) / count, 4),
        "planner_usage_rate": round(sum(item["planner_used"] for item in results) / count, 4),
        "mmr_trigger_count": sum(item["mmr_used"] for item in results),
        "cases": results,
    }
    rendered = json.dumps(output, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if output["ok"] else 1


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
