"""Generate controlled DeepSeek query-analysis JSON without running retrieval."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.config import get_settings  # noqa: E402
from mining_qa.llm_client import LLMClient  # noqa: E402
from mining_qa.query_analysis import (  # noqa: E402
    QUERY_ANALYSIS_SYSTEM_PROMPT,
    QueryAnalysis,
    analyses_to_jsonl,
    compile_query_analysis,
    query_analysis_json_schema,
    safe_log_record,
    stable_query_id,
)


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "knowledge_base_v4" / "evaluation" / "query_analysis_v1"


def load_cases(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        payload = json.loads(text)
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = payload.get("items") or payload.get("cases") or []
        else:
            rows = []
    output: list[dict[str, str]] = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        question = str(row.get("question") or row.get("original_question") or "").strip()
        if not question:
            continue
        output.append(
            {
                "query_id": str(
                    row.get("query_id")
                    or row.get("id")
                    or row.get("gold_id")
                    or stable_query_id(question)
                ),
                "question": question,
            }
        )
    if not output:
        raise ValueError(f"no query cases found in {path}")
    return output


def private_output_path(path: Path) -> bool:
    resolved = path.resolve()
    project = PROJECT_ROOT.resolve()
    if project not in resolved.parents:
        return True
    private_root = (PROJECT_ROOT / "data").resolve()
    return resolved == private_root or private_root in resolved.parents


def quality_summary(analyses: list[QueryAnalysis]) -> dict[str, Any]:
    failures = [item for item in analyses if item.validation.status != "pass"]
    return {
        "schema_version": "query_analysis.v1",
        "case_count": len(analyses),
        "validation_pass_count": len(analyses) - len(failures),
        "validation_fail_count": len(failures),
        "confirmation_required_count": sum(item.confirmation.required for item in analyses),
        "sensitive_data_detected_count": sum(item.privacy.sensitive_data_detected for item in analyses),
        "original_route_preserved_count": sum(
            item.retrieval_plan.original_query == item.original_question
            and item.retrieval_plan.original_route_independent
            for item in analyses
        ),
        "hard_filter_scope_violation_count": sum(
            hard_filter.scope != "exact_route_only"
            for item in analyses
            for hard_filter in item.retrieval_plan.hard_filters
        ),
        "semantic_rewrite_fallback_count": sum(
            "semantic_rewrite_fell_back_to_original" in item.validation.warnings
            for item in analyses
        ),
        "failed_cases": [
            {
                "query_id": item.query_id,
                "errors": item.validation.errors,
                "warnings": item.validation.warnings,
            }
            for item in failures
        ],
    }


def render_report(summary: dict[str, Any], analyses: list[QueryAnalysis]) -> str:
    lines = [
        "# Query Analysis v1 Quality Report",
        "",
        "This report evaluates query rewriting and anchor compilation only. No retrieval was run.",
        "",
        f"- Cases: {summary['case_count']}",
        f"- Validation passed: {summary['validation_pass_count']}",
        f"- Validation failed: {summary['validation_fail_count']}",
        f"- User confirmation required: {summary['confirmation_required_count']}",
        f"- Sensitive-data flags: {summary['sensitive_data_detected_count']}",
        f"- Independent original route preserved: {summary['original_route_preserved_count']}/{summary['case_count']}",
        f"- Hard-filter scope violations: {summary['hard_filter_scope_violation_count']}",
        f"- Semantic rewrites safely reverted to original: {summary['semantic_rewrite_fallback_count']}",
        "",
        "## Cases",
        "",
        "| ID | Validation | Anchors | Candidates | Confirm | Warnings |",
        "| --- | --- | ---: | ---: | --- | ---: |",
    ]
    for item in analyses:
        candidates = sum(anchor.state == "candidate" for anchor in item.anchors)
        lines.append(
            f"| {item.query_id} | {item.validation.status} | {len(item.anchors)} | "
            f"{candidates} | {'yes' if item.confirmation.required else 'no'} | "
            f"{len(item.validation.warnings)} |"
        )
    lines.extend(
        [
            "",
            "## Boundaries",
            "",
            "- Original questions are stored only in the private evaluation JSONL.",
            "- The safe-log JSONL contains hashes and structural metadata only.",
            "- Source preferences are soft and never exclude a governed material type.",
            "- Model inferences remain parallel-search candidates until user confirmation.",
            "- No FTS, BM25, vector, ANN, graph, corpus, or cloud operation was performed.",
            "",
        ]
    )
    return "\n".join(lines)


async def generate(cases: list[dict[str, str]], *, model: str, batch_size: int) -> list[QueryAnalysis]:
    settings = get_settings().model_copy(update={"openai_model": model})
    client = LLMClient(settings)
    if not client.enabled:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    output: list[QueryAnalysis] = []
    try:
        for offset in range(0, len(cases), batch_size):
            batch = cases[offset : offset + batch_size]
            request = {
                "items": [
                    {"query_id": case["query_id"], "original_question": case["question"]}
                    for case in batch
                ]
            }
            raw = await client.complete_json(
                [
                    {"role": "system", "content": QUERY_ANALYSIS_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
                ],
                max_tokens=7000,
            )
            payload = json.loads(raw)
            items = payload.get("items") or []
            by_id = {
                str(item.get("query_id")): item
                for item in items
                if isinstance(item, dict) and item.get("query_id")
            }
            for case in batch:
                model_item = by_id.get(case["query_id"])
                if model_item is None:
                    model_item = {
                        "query_id": case["query_id"],
                        "rewritten_question": "",
                        "question_type": ["uncertain"],
                        "anchors": [],
                        "ambiguities": [],
                        "search_hypotheses": [],
                        "source_preferences": {"primary": [], "supplementary": [], "exclude_by_type": []},
                        "lexical_terms": [],
                        "semantic_subqueries": [],
                        "evidence_requirements": [],
                        "self_check": {
                            "preserved_original_facts": [],
                            "missing_original_facts": ["model_output_missing"],
                            "added_assumptions": [],
                            "answer_generated": False,
                        },
                    }
                output.append(
                    compile_query_analysis(
                        case["question"],
                        model_item,
                        query_id=case["query_id"],
                        model=model,
                    )
                )
    finally:
        await client.aclose()
    return output


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    source = value.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="JSON/JSONL cases with id/query_id and question")
    source.add_argument("--question", help="Analyze one question")
    value.add_argument("--query-id")
    value.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    value.add_argument("--model", default="deepseek-v4-flash")
    value.add_argument("--batch-size", type=int, default=4)
    value.add_argument(
        "--include-ids",
        help="optional comma-separated query IDs to process from --input",
    )
    value.add_argument("--allow-project-raw-output", action="store_true")
    value.add_argument("--print-schema", action="store_true")
    return value


def main() -> None:
    args = parser().parse_args()
    if args.print_schema:
        print(json.dumps(query_analysis_json_schema(), ensure_ascii=False, indent=2))
        return
    if args.input:
        cases = load_cases(args.input)
    else:
        question = str(args.question or "").strip()
        cases = [{"query_id": args.query_id or stable_query_id(question), "question": question}]
    if args.include_ids:
        requested = {value.strip() for value in args.include_ids.split(",") if value.strip()}
        available = {case["query_id"] for case in cases}
        missing = sorted(requested - available)
        if missing:
            raise SystemExit(f"requested query IDs are absent from input: {missing}")
        cases = [case for case in cases if case["query_id"] in requested]
    output_dir = args.output_dir
    if not args.allow_project_raw_output and not private_output_path(output_dir):
        raise SystemExit("raw query analyses inside the project must be written under data/")
    output_dir.mkdir(parents=True, exist_ok=True)

    analyses = asyncio.run(generate(cases, model=args.model, batch_size=max(1, args.batch_size)))
    raw_path = output_dir / "query_analyses_v1.jsonl"
    safe_path = output_dir / "query_analyses_safe_log_v1.jsonl"
    summary_path = output_dir / "query_analysis_quality_report_v1.json"
    report_path = output_dir / "query_analysis_quality_report_v1.md"
    schema_path = output_dir / "query_analysis_schema_snapshot_v1.json"

    raw_path.write_text(analyses_to_jsonl(analyses), encoding="utf-8")
    safe_path.write_text(
        "".join(json.dumps(safe_log_record(item), ensure_ascii=False) + "\n" for item in analyses),
        encoding="utf-8",
    )
    summary = quality_summary(analyses)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(render_report(summary, analyses), encoding="utf-8")
    schema_path.write_text(
        json.dumps(query_analysis_json_schema(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({**summary, "output_dir": str(output_dir)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
