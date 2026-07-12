from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from usearch.index import Index


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.config import get_settings  # noqa: E402
from mining_qa.embedding_provider import embedding_config  # noqa: E402
from mining_qa.knowledge_store import DEFAULT_DB_PATH, connect  # noqa: E402


def main() -> int:
    settings = get_settings()
    config = embedding_config(settings)
    parser = argparse.ArgumentParser(description="Build the private dense USEARCH index.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--index", type=Path, default=Path(settings.ann_index_path))
    parser.add_argument("--manifest", type=Path, default=Path(settings.ann_manifest_path))
    parser.add_argument("--model", default=config.model)
    parser.add_argument("--connectivity", type=int, default=24)
    parser.add_argument("--expansion-add", type=int, default=128)
    parser.add_argument("--expansion-search", type=int, default=settings.ann_expansion_search)
    args = parser.parse_args()

    with connect(args.db) as conn:
        rows = conn.execute(
            """
            select chunk_id,dimensions,vector_json,updated_at
            from chunk_embeddings
            where vector_model = ?
            order by chunk_id
            """,
            (args.model,),
        ).fetchall()
    if not rows:
        raise RuntimeError(f"No dense embeddings found for model {args.model!r}")

    dimensions = int(rows[0]["dimensions"])
    if any(int(row["dimensions"]) != dimensions for row in rows):
        raise RuntimeError("Dense embedding dimensions are inconsistent")

    vectors = np.empty((len(rows), dimensions), dtype=np.float32)
    chunk_ids: list[str] = []
    max_updated_at = ""
    for index, row in enumerate(rows):
        vector = np.asarray(json.loads(row["vector_json"]), dtype=np.float32)
        if vector.shape != (dimensions,):
            raise RuntimeError(f"Unexpected vector shape for {row['chunk_id']}: {vector.shape}")
        vectors[index] = vector
        chunk_ids.append(str(row["chunk_id"]))
        max_updated_at = max(max_updated_at, str(row["updated_at"] or ""))

    args.index.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    ann = Index(
        ndim=dimensions,
        metric="cos",
        dtype="f16",
        connectivity=max(2, args.connectivity),
        expansion_add=max(2, args.expansion_add),
        expansion_search=max(2, args.expansion_search),
    )
    labels = np.arange(len(rows), dtype=np.uint64)
    ann.add(labels, vectors, threads=0, log=True)
    ann.save(args.index)

    manifest = {
        "version": 1,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(args.db),
        "index_path": str(args.index),
        "model": args.model,
        "dimensions": dimensions,
        "dtype": "f16",
        "count": len(rows),
        "max_updated_at": max_updated_at,
        "connectivity": max(2, args.connectivity),
        "expansion_add": max(2, args.expansion_add),
        "expansion_search": max(2, args.expansion_search),
        "chunk_ids": chunk_ids,
    }
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "model": args.model,
                "dimensions": dimensions,
                "count": len(rows),
                "connectivity": max(2, args.connectivity),
                "expansion_add": max(2, args.expansion_add),
                "expansion_search": max(2, args.expansion_search),
                "index": str(args.index),
                "manifest": str(args.manifest),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
