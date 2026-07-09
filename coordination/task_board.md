# Task Board

## Current Priority

1. KB agent builds the first knowledge-base ingestion and schema draft.
2. PRD agent reviews KB schema/API questions and keeps docs aligned.
3. Both agents keep standard-source and OCR constraints consistent with project docs.

## Tasks

| ID | Owner | Status | Task | Output |
| --- | --- | --- | --- | --- |
| T001 | KB agent | pending | Draft knowledge-base schema for standards, clauses, pages, OCR, uploads, and reviews. | `docs/KB_SCHEMA_DRAFT.md` |
| T002 | KB agent | pending | Produce sample parsed JSON from one PDF standard. | `docs/KB_SAMPLE_PARSE.json` |
| T003 | KB agent | pending | Draft `/knowledge/search` and `/knowledge/standards` implementation plan. | `docs/KB_SEARCH_API_DRAFT.md` |
| T004 | KB agent | pending | Report PDF recognition/OCR progress and blockers. | `coordination/kb_agent_status.md` |
| T005 | PRD agent | pending | Review KB schema/API draft after KB agent produces it. | PRD/API/KB docs updates |

## Status Values

- `pending`
- `in_progress`
- `blocked`
- `review_needed`
- `done`

## Notes

- MVP knowledge base should prioritize schema, standard catalog query, and Elasticsearch full-text search.
- ChromaDB vector search and Neo4j graph search are reserved for later phases.
- Embedding provider must be configurable and must not use `deepseek-v4-flash`.
