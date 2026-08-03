# Knowledge Base MVP Runbook

## Scope

This runbook contains both the current v4 startup contract and explicitly
labelled legacy-v3 rebuild notes. Current production uses
`v4-hybrid-fixed20-p1fix-v4`: 156 documents, 20,670 content units, 23,250
retrieval leaves, FTS5 and a persisted Qwen 1024-dimensional vector matrix.
It does not use ANN or a knowledge graph. Do not run the legacy rebuild steps
against production.

The historical v3 MVP used:

- SQLite + FTS5 for structured storage and full-text retrieval.
- Clause-level chunking for governed standards and MNR policy/law documents.
- Local deterministic hashed Chinese character n-gram vectors for MVP vector retrieval.
- SQLite `kg_entities` and `kg_relations` tables for the lightweight graph MVP.
- Hybrid full-text + vector + graph ranking in `mining_qa.knowledge_store`.
- JSON-backed `domain_lexicon` for query normalization, intent-aware retrieval, and negative evidence downranking.
- A standalone FastAPI service under `/knowledge/*`.
- Governed standard JSON outputs from `/home/nalanmading/My-project/ore_expert/knowledge_governance`.
- MNR policy/law HTML and attachments downloaded from the official policy/law database category `矿产资源管理`.
- Local data under `data/knowledge_base/`, which is ignored by Git.

## Legacy v3 Data Layout (Historical)

```text
data/knowledge_base/
  db/knowledge_base.sqlite
  logs/mnr_policy_ingest_summary.json
  logs/last_ingest_summary.json
  raw/mnr_policy/
  raw/mnr_policy/attachments/
  manifests/mnr_mineral_policy_manifest.csv
  manifests/mnr_mineral_policy_manifest.json
  manifests/governed_standards_ingest_manifest.csv
  manifests/governed_standards_ingest_manifest.json
src/mining_qa/domain_lexicon.json
```

## Legacy v3 Rebuild (Historical — Do Not Use For Production)

These commands are retained only to reproduce the v3 rollback asset. They do
not build or update the active v4 corpus.

```bash
cd /home/nalanmading/My-project/my-1st-agent
PYTHONPATH=src .venv/bin/python scripts/ingest_governed_standards.py
PYTHONPATH=src .venv/bin/python scripts/ingest_mnr_mineral_policies.py
PYTHONPATH=src .venv/bin/python scripts/rebuild_clause_chunks.py
PYTHONPATH=src .venv/bin/python scripts/build_sqlite_kg.py
PYTHONPATH=src .venv/bin/python scripts/build_chunk_vectors.py
```

Optional API-backed embedding:

```bash
# Smoke test a small paid batch first.
PYTHONPATH=src .venv/bin/python scripts/build_chunk_embeddings.py --limit 20

# Full rebuild after cost/range confirmation.
PYTHONPATH=src .venv/bin/python scripts/build_chunk_embeddings.py --reset
```

This writes dense vectors to `chunk_embeddings` and keeps local hash vectors as fallback.

Historical v3 expanded result:

- Documents: 388
- Chunks: 28,104
- MNR policy/law documents: 307
- Policy clause chunks: 2,593
- Standard/specification clause chunks: 20,910
- Manual table chunks: 716
- Policy documents with zero chunks: 0
- MNR policy documents with official URLs: 307/307
- Empty standard/specification clause numbers: 1,792/20,910 (8.57%)
- Local hashed vectors: 24,219
- SQLite KG entities: 25,307
- SQLite KG relations: 45,919
- High-value policy authority relations: `自然资规〔2023〕6号` 第十条 contains two `RESPONSIBLE_FOR` relations for `自然资源部` and `省级自然资源主管部门`.
- Domain lexicon: first-stage JSON config seeded for `authority_responsibility`, `standard_selection`, `numeric_table_lookup`, and `clause_comparison` intents.

## Legacy v3 Regression

```bash
cd /home/nalanmading/My-project/my-1st-agent
PYTHONPATH=src .venv/bin/python scripts/run_kb_regression.py
```

The regression covers:

- `GET /knowledge/health`.
- Policy-oriented hybrid search: `压覆矿产资源审批需要注意什么`.
- Regulation-oriented hybrid search: `矿产资源法实施条例 战略性矿产资源目录`.
- Standard-oriented hybrid search: `哪个标准规定了金矿基本工程间距？`.
- Policy authority hybrid search: `我的采矿证是自然资源部颁发的，我的储量评审应该去哪个机构`.
- Solid-mineral policy authority ranking: `我是一个大型的金矿，我的储量报告评审应该去哪个机构`, including downranking unrelated oil/gas and coalbed methane evidence.
- `GET /knowledge/standards` policy catalog lookup.
- Main QA API `/api/ask` end-to-end with KB retrieval stats.

## Start The KB Service

```bash
cd /home/nalanmading/My-project/my-1st-agent
PYTHONPATH=src .venv/bin/python -m uvicorn mining_qa.knowledge_service:app --host 127.0.0.1 --port 18081
```

Health check:

```bash
curl http://127.0.0.1:18081/knowledge/health
```

### Current v4 local production runtime

The current production-compatible profile is `hybrid_fixed20_v4` / `v4-hybrid-fixed20-p1fix-v4`. It retains the bounded query-Embedding fallback and duplicate-query single-flight introduced in v2, plus the accepted P1 retrieval and answerability fixes. The historical v1-v3 bundles remain frozen only for replay or rollback. Validate the hash-pinned v4 manifest, then select v4 explicitly:

```bash
cd /home/nalanmading/My-project/my-1st-agent
KNOWLEDGE_RUNTIME_VERSION=v4 \
V4_RUNTIME_MANIFEST=data/knowledge_base_v4/runtime_private/hybrid_fixed20_v4/runtime_manifest.json \
V4_CANDIDATE_DB_PATH=data/knowledge_base_v4/runtime_private/candidates.sqlite \
KNOWLEDGE_REQUEST_TIMEOUT_SECONDS=20 \
V4_EMBEDDING_TIMEOUT_SECONDS=3 \
V4_EMBEDDING_MAX_RETRIES=1 \
PYTHONPATH=src .venv/bin/python -m uvicorn mining_qa.knowledge_service:app --host 127.0.0.1 --port 18081
```

Check `http://127.0.0.1:18081/knowledge/health`; `runtime_id` must be `v4-hybrid-fixed20-p1fix-v4`, with 156 documents, 23,250 retrieval leaves, 23,250 vectors, `ann_available=false`, and zero knowledge-graph entities/relations. The adapter uses the accepted keyword-plus-Qwen-vector fixed-20 route and preserves the existing Knowledge API response contract. Query Embedding has a separate short budget and falls back to keywords on failure; identical concurrent searches are computed once.

Rollback is explicit: stop the v4 process and select the retained v3 database only for a deliberate recovery operation. Do not rely on an omitted environment variable, because current production-compatible examples select v4 explicitly. Local selection is not authorization to edit a cloud environment or restart the online service.

## Connect The QA API

```bash
cd /home/nalanmading/My-project/my-1st-agent
KNOWLEDGE_BASE_URL=http://127.0.0.1:18081 \
API_KEYS=dev-local-key \
RATE_LIMIT_ENABLED=false \
PYTHONPATH=src .venv/bin/python -m uvicorn mining_qa.api:app --host 127.0.0.1 --port 18080
```

Example:

```bash
curl -X POST http://127.0.0.1:18080/api/ask \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-local-key' \
  -d '{"question":"哪个标准规定了金矿基本工程间距？"}'
```

## MVP API

- `GET /knowledge/health`
- `POST /knowledge/search`
- `POST /knowledge/research/corpus`
- `GET /knowledge/standards`
- `GET /knowledge/documents/{document_id}`
- `GET /knowledge/chunks/{chunk_id}`
- `POST /knowledge/candidates`
- `GET /knowledge/candidates`

Search and chunk APIs return capped evidence text by default. Full chunk text is stored internally and only returned when `include_full_text=true` is explicitly passed to trusted local/internal calls.
