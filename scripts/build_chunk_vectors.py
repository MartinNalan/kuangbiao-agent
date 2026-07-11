from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.kb_build_utils import stable_id  # noqa: E402
from mining_qa.knowledge_store import DEFAULT_DB_PATH, connect, utc_now  # noqa: E402


VECTOR_DIM = 512
CHUNK_TYPES = (
    "clause",
    "policy_clause",
    "service_guide_section",
    "attachment_overview",
    "application_material_section",
    "application_material_row",
    "table",
)


def tokens(text: str) -> list[str]:
    text = text.upper()
    words = re.findall(r"[A-Z0-9]+(?:/[A-Z0-9]+)?(?:[-.][A-Z0-9]+)*|[\u4e00-\u9fff]{2,}", text)
    out: list[str] = []
    for word in words:
        out.append(word)
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", word):
            for n in (2, 3, 4):
                out.extend(word[i : i + n] for i in range(0, max(0, len(word) - n + 1)))
    return out


def vectorize(text: str) -> list[tuple[int, float]]:
    counts = Counter(tokens(text))
    if not counts:
        return []
    buckets: dict[int, float] = {}
    for token, count in counts.items():
        idx = int(stable_id(token, prefix="v").split("-")[1][:8], 16) % VECTOR_DIM
        buckets[idx] = buckets.get(idx, 0.0) + 1.0 + math.log(count)
    norm = math.sqrt(sum(v * v for v in buckets.values())) or 1.0
    return sorted((idx, round(value / norm, 6)) for idx, value in buckets.items())


def main() -> int:
    parser = argparse.ArgumentParser(description="Build local hashed chunk vectors.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args()
    now = utc_now()
    with connect(Path(args.db)) as conn:
        conn.executescript(
            """
            create table if not exists chunk_vectors (
              chunk_id text primary key,
              vector_model text not null,
              dimensions integer not null,
              vector_json text not null,
              updated_at text not null
            );
            create index if not exists idx_chunk_vectors_model on chunk_vectors(vector_model);
            """
        )
        conn.execute("delete from chunk_vectors")
        rows = conn.execute(
            """
            select chunk_id, title, standard_no, section_path, text
            from chunks
            where chunk_type in ({})
              and validation_status != 'empty_source_section'
            """.format(
                ",".join("?" for _ in CHUNK_TYPES)
            ),
            CHUNK_TYPES,
        ).fetchall()
        out = []
        for row in rows:
            text = "\n".join(x for x in [row["title"], row["standard_no"], row["section_path"], row["text"]] if x)
            vec = vectorize(text)
            if vec:
                out.append((row["chunk_id"], "local_hash_char_ngram_v1", VECTOR_DIM, json.dumps(vec), now))
        conn.executemany(
            "insert into chunk_vectors(chunk_id, vector_model, dimensions, vector_json, updated_at) values (?, ?, ?, ?, ?)",
            out,
        )
    print({"source_chunks": len(rows), "vectors": len(out), "dimensions": VECTOR_DIM})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
