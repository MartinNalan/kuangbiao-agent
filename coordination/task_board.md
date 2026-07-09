# Task Board

## Current Priority

1. Review the expanded local/internal KB and connect it through `KNOWLEDGE_BASE_URL`.
2. Continue improving retrieval ranking and clause parsing with real questions.
3. Keep standard-source, OCR, and licensing constraints consistent with project docs.

## Tasks

| ID | Owner | Status | Task | Output |
| --- | --- | --- | --- | --- |
| T001 | KB agent | done | Implement knowledge-base schema for standards, chunks, tables, candidates, and ingest runs. | `src/mining_qa/knowledge_store.py` |
| T002 | KB agent | done | Ingest all governed standards into the first local/internal KB. | `data/knowledge_base/` |
| T003 | KB agent | done | Implement `/knowledge/search` and `/knowledge/standards`. | `src/mining_qa/knowledge_service.py` |
| T004 | KB agent | done | Report OCR/table governance and KB MVP status. | `coordination/kb_agent_status.md` |
| T005 | PRD agent | done | Review implemented KB MVP behavior and docs alignment. | `docs/API_SPEC.md`, `docs/ARCHITECTURE.md`, API regression updates |
| T006 | KB agent | done | Add real-KB regression tests for search/catalog/candidate endpoints. | `scripts/run_kb_regression.py` |
| T007 | KB agent | done | Rebuild clause-level chunks for standards, especially mineral-body extrapolation clauses such as 5.4, 8.2.3, 8.2.6, 9.2.6 and G.1; preserve accurate `clause_no`, section path, page, and 1-3 sentence evidence quotes. | `scripts/rebuild_clause_chunks.py`, refreshed `data/knowledge_base/` |
| T008 | KB agent | done | Tighten raw `/knowledge/search` evidence output: return only 1-3 directly relevant sentences in `quote`, avoid long surrounding OCR text, and preserve `include_full_text=true` only for trusted internal review. | `src/mining_qa/knowledge_store.py`, updated regression checks |
| T009 | KB agent | done | Improve clause metadata completeness: reduce empty `clause_no` for clause chunks, especially standards/specifications where section numbers are present in OCR text; keep section path, page range, and source URL aligned. | `scripts/rebuild_clause_chunks.py`, refreshed `data/knowledge_base/` |
| T010 | KB agent | done | Expand `/knowledge/health` with operational index counts for validation: `vector_count`, `kg_entity_count`, `kg_relation_count`, plus existing document/chunk/candidate counts. | `src/mining_qa/knowledge_store.py`, `src/mining_qa/knowledge_service.py` |

## Status Values

- `pending`
- `in_progress`
- `blocked`
- `review_needed`
- `done`

## Notes

- MVP knowledge base uses SQLite + FTS5 first; Elasticsearch/OpenSearch is a later scale upgrade path.
- MVP vector retrieval uses local deterministic hashed character n-gram vectors in SQLite; ChromaDB/FAISS is a later scale/quality upgrade path.
- MVP graph retrieval uses lightweight SQLite KG tables; Neo4j is a later graph upgrade path.
- Embedding provider must be configurable and must not use `deepseek-v4-flash`.
