from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path
from time import perf_counter

from mining_qa.config import Settings
from mining_qa.prompt_registry import PROMPT_REGISTRY, PROMPT_REGISTRY_VERSION, registry_manifest
from mining_qa.question_resolution import QuestionResolver


async def evaluate_live(
    cases: list[dict[str, object]],
    settings: Settings,
    concurrency: int,
) -> dict[str, object]:
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is required for --live")
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def evaluate_case(item: dict[str, object]) -> dict[str, object]:
        async with semaphore:
            resolver = QuestionResolver(settings)
            started = perf_counter()
            try:
                result = await resolver.resolve(str(item["question"]))
            finally:
                await resolver.aclose()
            elapsed_ms = round((perf_counter() - started) * 1000, 2)
        actual = (
            result.plan.classification.primary_intent
            if result.plan.classification
            else "unknown"
        )
        expected = str(item["intent"])
        return {
            "id": item["id"],
            "expected": expected,
            "actual": actual,
            "matched": actual == expected,
            "model_used": result.model_used,
            "error": result.error,
            "elapsed_ms": elapsed_ms,
        }

    results = await asyncio.gather(*(evaluate_case(item) for item in cases))
    latencies = sorted(float(item["elapsed_ms"]) for item in results)
    percentile_index = max(0, min(len(latencies) - 1, int((len(latencies) - 1) * 0.95)))
    return {
        "accuracy": round(sum(bool(item["matched"]) for item in results) / len(results), 4),
        "model_used": sum(bool(item["model_used"]) for item in results),
        "errors": sum(bool(item["error"]) for item in results),
        "p95_ms": latencies[percentile_index],
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate geowiki prompt calibration coverage.")
    parser.add_argument(
        "--cases",
        default="tests/fixtures/prompt_calibration_cases.json",
        help="Path to the versioned calibration case list.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run the cases through the configured question-resolution model and report classification accuracy.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Concurrent live model calls; default 1 keeps the calibration run deterministic.",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print live aggregate metrics without the per-case result list.",
    )
    parser.add_argument("--offset", type=int, default=0, help="First calibration case offset.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of calibration cases to run.")
    args = parser.parse_args()
    path = Path(args.cases)
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise SystemExit("calibration cases must be a JSON array")
    if not 30 <= len(cases) <= 50:
        raise SystemExit("calibration set must contain 30 to 50 cases")

    selected_cases = cases[max(0, args.offset) :]
    if args.limit > 0:
        selected_cases = selected_cases[: args.limit]
    if not selected_cases:
        raise SystemExit("no calibration cases selected")

    seen_ids: set[str] = set()
    stage_counts: Counter[str] = Counter()
    intent_counts: Counter[str] = Counter()
    for item in selected_cases:
        if not isinstance(item, dict):
            raise SystemExit("each calibration item must be an object")
        case_id = str(item.get("id") or "")
        stage = str(item.get("stage") or "")
        intent = str(item.get("intent") or "")
        question = str(item.get("question") or "")
        if not case_id or case_id in seen_ids or not question:
            raise SystemExit(f"invalid calibration item: {item}")
        if stage not in PROMPT_REGISTRY:
            raise SystemExit(f"unknown prompt stage: {stage}")
        seen_ids.add(case_id)
        stage_counts[stage] += 1
        intent_counts[intent] += 1

    report = {
        "prompt_registry_version": PROMPT_REGISTRY_VERSION,
        "cases": len(selected_cases),
        "stages": dict(stage_counts),
        "intents": dict(intent_counts),
        "manifest": registry_manifest(),
        "note": "This offline command validates calibration coverage. Model quality comparison is run in a controlled canary with the same cases and captured traces.",
    }
    if args.live:
        live_result = asyncio.run(
            evaluate_live(selected_cases, Settings(), args.concurrency)
        )
        if args.summary:
            live_result = {key: value for key, value in live_result.items() if key != "results"}
        report["live_evaluation"] = live_result
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
