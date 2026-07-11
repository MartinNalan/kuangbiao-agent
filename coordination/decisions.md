# Decisions

Confirmed cross-agent decisions should be recorded here.

## Confirmed

### 2026-07-08

- Product display name is `geowiki`; the Chinese subtitle is `一款专注地质领域的百科全搜`.
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

### 2026-07-11

- `/home/nalanmading/下载/0. 继续有效文件.xls` is the authoritative validity allowlist for previously web-crawled MNR files published before 2026. Pre-2026 crawled files absent from the workbook must be removed from the active knowledge base and all derived retrieval artifacts, and the ingest flow must prevent them from being reintroduced. Files published in 2026 or later are outside this cleanup rule.
- The 40 numbered Markdown files under `/home/nalanmading/下载/2. geowiki` form a separate official MNR mineral-service-guide corpus and must be ingested as structured, internally visible service-guide documents. `_INDEX.md` is a corpus manifest only. These guides are outside the pre-2026 normative-document allowlist cleanup rule.

## Pending

- Whether MVP needs a minimal admin account or upload review waits until V1.
- Whether standard catalog query needs Excel export.
- User upload file size limit.
