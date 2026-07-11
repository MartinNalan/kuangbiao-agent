from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.config import get_settings  # noqa: E402
from mining_qa.embedding_provider import EmbeddingProvider, embedding_config  # noqa: E402
from mining_qa.knowledge_store import DEFAULT_DB_PATH, connect, utc_now  # noqa: E402


CHUNK_TYPES = (
    "clause",
    "policy_clause",
    "service_guide_section",
    "attachment_overview",
    "application_material_section",
    "application_material_row",
    "table",
)


def chunk_text(row) -> str:
    return "\n".join(x for x in [row["title"], row["standard_no"], row["section_path"], row["text"]] if x)


def batched(items: list, batch_size: int):
    for index in range(0, len(items), batch_size):
        yield items[index : index + batch_size]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build API-backed dense embeddings for KB chunks.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--limit", type=int, default=None, help="Optional max chunks to embed for smoke testing")
    parser.add_argument("--reset", action="store_true", help="Delete existing embeddings for the configured model first")
    args = parser.parse_args()

    settings = get_settings()
    config = embedding_config(settings)
    if not config.enabled:
        raise SystemExit("Embedding provider is not configured. Fill DASHSCOPE_API_KEY or EMBEDDING_API_KEY in .env.")

    provider = EmbeddingProvider(config, timeout_seconds=settings.request_timeout_seconds)
    now = utc_now()
    with connect(Path(args.db)) as conn:
        conn.executescript(
            """
            create table if not exists chunk_embeddings (
              chunk_id text not null,
              vector_model text not null,
              provider text not null,
              dimensions integer not null,
              vector_json text not null,
              updated_at text not null,
              primary key (chunk_id, vector_model)
            );
            create index if not exists idx_chunk_embeddings_model on chunk_embeddings(vector_model);
            """
        )
        if args.reset:
            conn.execute("delete from chunk_embeddings where vector_model = ?", (config.model,))
        sql = """
            select c.chunk_id, c.title, c.standard_no, c.section_path, c.text
            from chunks c
            where c.chunk_type in ({})
              and c.validation_status != 'empty_source_section'
              and not exists (
                select 1 from chunk_embeddings e
                where e.chunk_id = c.chunk_id and e.vector_model = ?
              )
            order by c.document_id, c.page_start, c.chunk_id
        """.format(",".join("?" for _ in CHUNK_TYPES))
        params = [*CHUNK_TYPES, config.model]
        if args.limit is not None:
            sql += " limit ?"
            params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()

        inserted = 0
        for batch in batched(rows, config.batch_size):
            texts = [chunk_text(row) for row in batch]
            vectors = provider.embed(texts)
            out = []
            for row, vector in zip(batch, vectors):
                out.append(
                    (
                        row["chunk_id"],
                        config.model,
                        config.provider,
                        len(vector),
                        json.dumps(vector, separators=(",", ":")),
                        now,
                    )
                )
            conn.executemany(
                """
                insert or replace into chunk_embeddings(
                  chunk_id, vector_model, provider, dimensions, vector_json, updated_at
                ) values (?, ?, ?, ?, ?, ?)
                """,
                out,
            )
            conn.commit()
            inserted += len(out)
            print({"embedded": inserted, "remaining": max(0, len(rows) - inserted), "model": config.model})
    print({"source_chunks": len(rows), "embeddings": inserted, "model": config.model, "provider": config.provider})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
