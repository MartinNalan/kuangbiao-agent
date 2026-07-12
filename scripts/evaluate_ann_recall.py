from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from time import perf_counter

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.ann_index import get_ann_index  # noqa: E402
from mining_qa.config import get_settings  # noqa: E402
from mining_qa.embedding_provider import EmbeddingProvider, embedding_config  # noqa: E402
from mining_qa.knowledge_store import DEFAULT_DB_PATH, connect  # noqa: E402


DEFAULT_QUERIES = [
    "金矿勘查Ⅰ类型的推荐工程间距",
    "矿体无限外推所依据的工程间距差异",
    "矿产资源储量评审备案由哪个部门负责",
    "详查程度探矿权转采矿权报告条件",
    "采矿许可变更开采方式办理流程",
    "省级颁发采矿许可证的储量评审备案机关",
    "自然资源部颁发探矿许可证的储量评审备案权限",
    "砂金勘查适用哪个行业标准",
    "方解石矿地质勘查规范现行状态",
    "铁矿基本分析项目TFe和mFe",
    "伴生矿产资源量类型如何确定",
    "岩金矿床勘查类型划分因素",
    "固体矿产资源量几何法有限外推",
    "矿体无限外推经验工程间距二分之一尖推",
    "矿体有限外推实际工程间距四分之一平推",
    "资源储量报告真实性由谁负责",
    "采矿权延续登记申请材料",
    "采矿权新立申请资料清单",
    "探矿权首次登记办事指南",
    "矿产资源开采方案办结时限",
    "建设项目压覆矿产资源审批材料",
    "矿区水文地质工程地质环境地质勘查规范",
    "矿山资源储量年度变化管理",
    "矿产资源开发利用方案审查",
    "不同规范矿体外推距离基准比较",
]


def main() -> int:
    settings = get_settings()
    config = embedding_config(settings)
    parser = argparse.ArgumentParser(
        description="Compare USEARCH expansion_search values with exact dense cosine search."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--min-recall", type=float, default=0.8)
    parser.add_argument("--expansion-search", type=int, nargs="+", default=[64, 96, 128])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not config.enabled:
        raise RuntimeError("Embedding provider is not configured")

    ann = get_ann_index(settings.ann_index_path, settings.ann_manifest_path)
    manifest = ann.manifest()
    if manifest is None:
        raise RuntimeError("ANN index is unavailable")

    with connect(args.db) as conn:
        rows = conn.execute(
            """
            select chunk_id, vector_json
            from chunk_embeddings
            where vector_model = ?
            order by chunk_id
            """,
            (manifest.model,),
        ).fetchall()
    row_vectors = {str(row["chunk_id"]): row["vector_json"] for row in rows}
    if any(chunk_id not in row_vectors for chunk_id in manifest.chunk_ids):
        raise RuntimeError("ANN manifest and SQLite embeddings are inconsistent")

    matrix = np.asarray(
        [json.loads(row_vectors[chunk_id]) for chunk_id in manifest.chunk_ids],
        dtype=np.float32,
    )
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix = np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms > 0)
    query_vectors = EmbeddingProvider(
        config,
        timeout_seconds=settings.request_timeout_seconds,
    ).embed(DEFAULT_QUERIES)

    top_k = max(1, min(args.top_k, manifest.count))
    exact_sets = []
    for vector in query_vectors:
        query_array = np.asarray(vector, dtype=np.float32)
        query_norm = float(np.linalg.norm(query_array))
        if query_norm > 0:
            query_array = query_array / query_norm
        scores = matrix @ query_array
        exact_indices = np.argpartition(scores, -top_k)[-top_k:]
        exact_sets.append({manifest.chunk_ids[int(index)] for index in exact_indices})

    candidate_results = []
    for expansion_search in sorted(set(max(2, value) for value in args.expansion_search)):
        recalls = []
        latencies = []
        query_results = []
        for query, vector, exact_ids in zip(DEFAULT_QUERIES, query_vectors, exact_sets, strict=True):
            ann.search(vector, top_k, expansion_search=expansion_search)
            samples = []
            ann_ids = set()
            for _ in range(max(1, args.repeats)):
                started = perf_counter()
                matches = ann.search(vector, top_k, expansion_search=expansion_search)
                samples.append((perf_counter() - started) * 1000)
                ann_ids = {chunk_id for chunk_id, _ in matches}
            recall = len(exact_ids & ann_ids) / top_k
            latency = min(samples)
            recalls.append(recall)
            latencies.append(latency)
            query_results.append(
                {
                    "query": query,
                    "recall_at_k": round(recall, 4),
                    "search_ms": round(latency, 3),
                }
            )
        candidate_results.append(
            {
                "expansion_search": expansion_search,
                "mean_recall": round(float(np.mean(recalls)), 4),
                "minimum_recall": round(float(np.min(recalls)), 4),
                "p50_ms": round(float(np.percentile(latencies, 50)), 3),
                "p95_ms": round(float(np.percentile(latencies, 95)), 3),
                "queries": query_results,
            }
        )

    eligible = [
        result for result in candidate_results if result["mean_recall"] >= args.min_recall
    ]
    recommended = min(
        eligible,
        key=lambda result: (result["p95_ms"], result["expansion_search"]),
        default=None,
    )
    output = {
        "ok": recommended is not None,
        "top_k": top_k,
        "query_count": len(DEFAULT_QUERIES),
        "minimum_required": args.min_recall,
        "index_manifest": {
            "model": manifest.model,
            "count": manifest.count,
            "connectivity": manifest.connectivity,
            "expansion_add": manifest.expansion_add,
            "expansion_search": manifest.expansion_search,
        },
        "recommended_expansion_search": (
            recommended["expansion_search"] if recommended else None
        ),
        "candidates": candidate_results,
    }
    rendered = json.dumps(output, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
