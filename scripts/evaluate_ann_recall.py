from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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
]


def main() -> int:
    settings = get_settings()
    config = embedding_config(settings)
    parser = argparse.ArgumentParser(description="Compare USEARCH ANN results with exact dense cosine search.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--min-recall", type=float, default=0.8)
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
    query_vectors = EmbeddingProvider(
        config,
        timeout_seconds=settings.request_timeout_seconds,
    ).embed(DEFAULT_QUERIES)

    top_k = max(1, min(args.top_k, manifest.count))
    recalls = []
    results = []
    for query, vector in zip(DEFAULT_QUERIES, query_vectors, strict=True):
        query_array = np.asarray(vector, dtype=np.float32)
        scores = matrix @ query_array
        exact_indices = np.argpartition(scores, -top_k)[-top_k:]
        exact_ids = {manifest.chunk_ids[int(index)] for index in exact_indices}
        ann_ids = {chunk_id for chunk_id, _ in ann.search(vector, top_k)}
        recall = len(exact_ids & ann_ids) / top_k
        recalls.append(recall)
        results.append({"query": query, "recall_at_k": round(recall, 4)})

    mean_recall = sum(recalls) / len(recalls)
    output = {
        "ok": mean_recall >= args.min_recall,
        "top_k": top_k,
        "minimum_required": args.min_recall,
        "mean_recall": round(mean_recall, 4),
        "queries": results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
