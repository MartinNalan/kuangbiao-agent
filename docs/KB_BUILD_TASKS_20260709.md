# KB Build Tasks 2026-07-09

## Goal

Extend the current local/internal knowledge base from governed technical standards to a broader mineral-resources knowledge base with:

1. Full mineral-resources policy/law download and clause-level ingestion.
2. Clause-level chunks for existing standards/specifications.
3. Lightweight SQLite knowledge graph.
4. Hybrid full-text + vector + graph retrieval ranking.

All real text, downloaded files, indexes, DBs, and generated manifests stay under:

```text
data/knowledge_base/
```

This directory is ignored by Git.

## Task Checklist

| ID | Status | Task | Output |
|---|---|---|---|
| KB-T001 | done | Download and ingest all MNR `矿产资源管理` policy/law files with clause-level chunks. | `scripts/ingest_mnr_mineral_policies.py`, `data/knowledge_base/raw/mnr_policy/`, `data/knowledge_base/manifests/mnr_mineral_policy_manifest.*` |
| KB-T002 | done | Rebuild clause-level chunks for already ingested standards/specifications. | `scripts/rebuild_clause_chunks.py`, SQLite `chunks.chunk_type='clause'` |
| KB-T003 | done | Build lightweight SQLite knowledge graph. | `scripts/build_sqlite_kg.py`, SQLite `kg_entities`, `kg_relations` |
| KB-T004 | done | Add hybrid full-text + vector + graph ranking. | `scripts/build_chunk_vectors.py`, SQLite `chunk_vectors`, `knowledge_store.py` hybrid search |
| KB-T005 | done | Verify with regression queries and update coordination docs. | `scripts/run_kb_regression.py`, `coordination/kb_agent_status.md` |

## Completion Snapshot

Date: 2026-07-10 08:45 +0800

- MNR policy/law unique detail pages parsed and ingested: 307.
- MNR attachments downloaded: 82 files.
- Total KB documents: 388.
- Total chunks: 28,104.
- Policy clause chunks: 2,593.
- Standard/specification clause chunks: 20,910.
- Empty standard/specification clause numbers reduced from 2,359/18,516 (12.74%) to 1,792/20,910 (8.57%).
- Policy documents with zero chunks: 0.
- MNR policy documents with official URLs: 307/307.
- Local hashed vectors: 24,219.
- SQLite KG entities: 25,307.
- SQLite KG relations: 45,919.
- Domain lexicon: JSON-backed first stage at `src/mining_qa/domain_lexicon.json`, seeded with high-value intent entries from requirements section 6.8.
- Added high-value policy authority KG relations for `自然资规〔2023〕6号` 第十条: `自然资源部 RESPONSIBLE_FOR 本级已颁发勘查许可证或采矿许可证的矿产资源储量评审备案` and `省级自然资源主管部门 RESPONSIBLE_FOR 其他矿产资源储量评审备案`.
- Regression commands passed: `KB_URL=http://127.0.0.1:18181 API_URL=http://127.0.0.1:18180 PYTHONPATH=src .venv/bin/python scripts/run_kb_regression.py` and `KB_URL=http://127.0.0.1:18181 API_URL=http://127.0.0.1:18180 PYTHONPATH=src .venv/bin/python scripts/run_api_regression.py`.

## Current Policy Source

Entry page:

```text
https://f.mnr.gov.cn/
```

Mineral-resources category:

```text
https://f.mnr.gov.cn/579/585/index_3553.html
```

Pagination:

```text
index_3553.html
index_3553_1.html
...
```

The category page reports 310 items and `countPage=21`, while the crawler parsed 307 unique policy detail pages from the pagination. The user described “330多个”; scripts trust parsed pages and report the actual count.

## Implementation Notes

- Policy/law documents should be inserted as `document_type in ('law', 'regulation', 'policy_document', 'department_rule')`.
- Use `source_type='official_fulltext'`, `text_access='html_text'`, `visibility='internal'`.
- Download HTML and attachments. Attachments are stored but not necessarily parsed in the first pass.
- Clause-level chunks should preserve source URL and metadata. Web policies have no page number; standard/specification clauses keep page ranges when available.
- Keep existing page/table chunks as fallback. Clause chunks are added, not used as replacements.
- Vector MVP can use deterministic local hashed character n-gram vectors to avoid external embedding dependencies. This is a replaceable adapter and not the final embedding model.
- Graph MVP uses SQLite tables first. Neo4j remains a later upgrade.
- `domain_lexicon` currently uses a versionable JSON config. Intent matching should use user expressions and canonical terms; positive expansions are retrieval recall terms and should not by themselves trigger an intent.
