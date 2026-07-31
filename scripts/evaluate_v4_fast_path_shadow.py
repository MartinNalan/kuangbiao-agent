#!/usr/bin/env python3
"""Evaluate the deterministic fast-path gate after selection, then score it."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from statistics import mean
from typing import Any

from mining_qa.fast_path import evaluate_fast_path_shadow


ROOT = Path(__file__).resolve().parents[1]
V4_ROOT = ROOT / "data" / "knowledge_base_v4"
DEFAULT_SOURCE_ROOT = V4_ROOT / "evaluation" / "t084_direct_fast_candidate_gate_v1"
DEFAULT_OUTPUT_ROOT = V4_ROOT / "evaluation" / "t084_fast_path_closure_shadow_v1"


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(stable_json(value), encoding="utf-8")
    os.replace(temporary, path)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def render_report(result: dict[str, Any]) -> str:
    metrics = result["metrics"]
    lines = [
        "# T084 确定性快速通道证据闭合影子评价",
        "",
        f"- 影子放行：`{metrics['eligible_count']}/{metrics['case_count']}`；"
        f"放行题Gold通过：`{metrics['eligible_gold_pass']}/{metrics['eligible_count']}`；"
        f"错误放行：`{metrics['false_admission_count']}`。",
        f"- 放行题平均端到端时延：`{metrics['eligible_average_ms']:.0f} ms`。",
        "- 判定器不读取Gold问题答案或必要证据；Gold只在放行决定完成后用于误放统计。",
        "- 本轮只评价既有本地影子输出，不启用生产快速通道，不连接或修改云端。",
        "",
        "| ID | 影子放行 | Gold通过 | 时延(ms) | 原因 |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for row in result["cases"]:
        lines.append(
            f"| {row['case_id']} | {'是' if row['eligible'] else '否'} | "
            f"{'是' if row['gold_passed'] else '否'} | {row['elapsed_ms']:.0f} | "
            f"{', '.join(row['reasons']) or '证据闭合且确定性渲染'} |"
        )
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> dict[str, Any]:
    source_root = args.source_root.resolve()
    source = json.loads((source_root / "results.json").read_text(encoding="utf-8"))
    traces = [
        json.loads(line)
        for line in (source_root / "retrieval_traces.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    validation = source.get("validation_cases") or []
    if len(validation) != 22 or len(traces) < 22:
        raise RuntimeError("T084 direct candidate contract must contain 22 validation cases")

    rows = []
    rejection_reasons: Counter[str] = Counter()
    for case, trace in zip(validation, traces[:22], strict=True):
        decision = evaluate_fast_path_shadow(case, trace)
        rejection_reasons.update(decision.reasons)
        rows.append(
            {
                "case_id": case["case_id"],
                "elapsed_ms": case["elapsed_ms"],
                "eligible": decision.eligible,
                "reasons": list(decision.reasons),
                "gold_passed": bool(case.get("passed")),
            }
        )

    eligible = [row for row in rows if row["eligible"]]
    false_admissions = [row for row in eligible if not row["gold_passed"]]
    result = {
        "schema_version": "geowiki-v4-fast-path-closure-shadow.v1",
        "created_at": utc_now(),
        "status": "accepted_for_further_shadow_only" if not false_admissions else "rejected",
        "source_root": str(source_root),
        "selection_contract": {
            "gold_visible_to_gate": False,
            "question_model_disabled": True,
            "planner_model_disabled": True,
            "requires_deterministic_renderer": True,
            "production_activation_authorized": False,
            "cloud_sync_required": False,
        },
        "metrics": {
            "case_count": len(rows),
            "eligible_count": len(eligible),
            "eligible_gold_pass": sum(row["gold_passed"] for row in eligible),
            "false_admission_count": len(false_admissions),
            "eligible_average_ms": round(mean(row["elapsed_ms"] for row in eligible), 3) if eligible else 0.0,
            "rejection_reasons": dict(sorted(rejection_reasons.items())),
        },
        "cases": rows,
    }
    write_atomic(args.results.resolve(), result)
    args.report.resolve().parent.mkdir(parents=True, exist_ok=True)
    args.report.resolve().write_text(render_report(result), encoding="utf-8")
    return result


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    value.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    value.add_argument("--results", type=Path, default=DEFAULT_OUTPUT_ROOT / "results.json")
    value.add_argument("--report", type=Path, default=DEFAULT_OUTPUT_ROOT / "report.md")
    return value


if __name__ == "__main__":
    result = run(parser().parse_args())
    print(stable_json({"status": result["status"], "metrics": result["metrics"]}), end="")
