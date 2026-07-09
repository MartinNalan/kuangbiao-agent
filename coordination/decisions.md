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
- Uploaded third-party documents are private by default and enter the public knowledge base only after admin approval.
- Standard status priority: national standards prefer national standard official platforms; natural-resource industry standards prefer `nrsis.org.cn`; conflicts must be preserved and surfaced.
- Community edition uses AGPL-3.0.
- Enterprise edition uses a commercial license and may live in a private repository or outside GitHub.
- Public repositories must not include real standard PDFs, OCR full text, prebuilt standard knowledge bases, or prebuilt standard vector indexes.

## Pending

- Whether MVP needs a minimal admin account or upload review waits until V1.
- Whether standard catalog query needs Excel export.
- User upload file size limit.
