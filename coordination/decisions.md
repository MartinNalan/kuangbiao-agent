# Decisions

Confirmed cross-agent decisions should be recorded here.

## Confirmed

### 2026-07-08

- Product Chinese name is tentatively `矿标智询`; project code remains `Mining Knowledge QA`.
- MVP knowledge base prioritizes schema, standard catalog query, and Elasticsearch full-text search.
- ChromaDB vector search and Neo4j graph search are later enhancements.
- `deepseek-v4-flash` is used for generation, not embedding.
- Embedding provider must be configurable.
- OCR candidate tool is PaddleOCR / PP-StructureV3 / TableRecognitionPipelineV2.
- Uploaded third-party documents are private by default and may enter the controlled service-visible knowledge scope only after admin approval; the knowledge base itself is never public.
- Standard status priority: national standards prefer national standard official platforms; natural-resource industry standards prefer `nrsis.org.cn`; conflicts must be preserved and surfaced.
- Community edition uses AGPL-3.0.
- Enterprise edition uses a commercial license and may live in a private repository or outside GitHub.
- Public repositories must not include real standard PDFs, OCR full text, prebuilt standard knowledge bases, or prebuilt standard vector indexes.

### 2026-07-09

- MVP knowledge service may use SQLite + FTS5 first, as long as `/knowledge/search` and `/knowledge/standards` keep the agreed response contract; Elasticsearch/OpenSearch remains a scale/production upgrade path.
- Knowledge API should be a standalone FastAPI service under `/knowledge/*`; QA backend calls it through `KNOWLEDGE_BASE_URL` and must not read KB files directly.
- MVP knowledge service is trusted local/internal. External API auth, rate limiting, and usage logging are owned by the QA backend. KB schema should still store `visibility`, `owner_user_id`, `organization_id`, and `review_status`.
- Search and chunk APIs return capped evidence quotes by default. Full chunk text is only for trusted internal/local calls when explicitly requested.
- Upload/candidate/review workflows are schema/stub level for MVP; governed standards search and catalog come first.
- First governed-standard ingestion should include all governed outputs, but mark them `visibility=internal` by default, not externally public cloud content.
- Knowledge base assets are a protected moat: raw files, OCR text, chunk text, indexes, DB files, catalog internals, and KB APIs must not be publicly exposed. Commercialization exposes only the controlled QA API output.

## Pending

- Whether MVP needs a minimal admin account or upload review waits until V1.
- Whether standard catalog query needs Excel export.
- User upload file size limit.
