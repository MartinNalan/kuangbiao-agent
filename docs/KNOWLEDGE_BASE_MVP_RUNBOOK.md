# Knowledge Base MVP Runbook

## Scope

This MVP implements the local/internal knowledge base service required by `docs/KNOWLEDGE_BASE_REQUIREMENTS.md`.

It uses:

- SQLite + FTS5 for structured storage and full-text retrieval.
- Clause-level chunking for governed standards and MNR policy/law documents.
- Local deterministic hashed Chinese character n-gram vectors for MVP vector retrieval.
- SQLite `kg_entities` and `kg_relations` tables for the lightweight graph MVP.
- Hybrid full-text + vector + graph ranking in `mining_qa.knowledge_store`.
- A standalone FastAPI service under `/knowledge/*`.
- Governed standard JSON outputs from `/home/nalanmading/My-project/ore_expert/knowledge_governance`.
- MNR policy/law HTML and attachments downloaded from the official policy/law database category `矿产资源管理`.
- Local data under `data/knowledge_base/`, which is ignored by Git.

## Data Layout

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
```

## Rebuild The KB

```bash
cd /home/nalanmading/My-project/my-1st-agent
PYTHONPATH=src .venv/bin/python scripts/ingest_governed_standards.py
PYTHONPATH=src .venv/bin/python scripts/ingest_mnr_mineral_policies.py
PYTHONPATH=src .venv/bin/python scripts/rebuild_clause_chunks.py
PYTHONPATH=src .venv/bin/python scripts/build_sqlite_kg.py
PYTHONPATH=src .venv/bin/python scripts/build_chunk_vectors.py
```

Current expanded KB result:

- Documents: 388
- Chunks: 28,104
- MNR policy/law documents: 307
- Policy clause chunks: 2,593
- Standard/specification clause chunks: 20,910
- Manual table chunks: 716
- Policy documents with zero chunks: 0
- Empty standard/specification clause numbers: 1,792/20,910 (8.57%)
- Local hashed vectors: 24,219
- SQLite KG entities: 25,307
- SQLite KG relations: 45,919
- High-value policy authority relations: `自然资规〔2023〕6号` 第十条 contains two `RESPONSIBLE_FOR` relations for `自然资源部` and `省级自然资源主管部门`.

## Verify The KB

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
- `GET /knowledge/standards`
- `GET /knowledge/documents/{document_id}`
- `GET /knowledge/chunks/{chunk_id}`
- `POST /knowledge/candidates`
- `GET /knowledge/candidates`

Search and chunk APIs return capped evidence text by default. Full chunk text is stored internally and only returned when `include_full_text=true` is explicitly passed to trusted local/internal calls.
