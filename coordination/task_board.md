# Task Board

## Current Priority

1. Continue improving retrieval ranking and clause parsing with real questions.

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
| T011 | KB agent | done | Build an intent-enhanced retrieval layer for high-value policy authority questions. Start with `储量评审备案权限/负责机构` cases: map user wording such as `采矿证`, `去哪个机构`, `谁负责`, `哪一级部门` to canonical terms such as `采矿许可证`, `评审备案范围和权限`, `自然资源主管部门负责`. | synonym/intent rules in KB retrieval code or config, updated regression |
| T012 | KB agent | done | Extract and store high-value authority relations from policy clauses into the lightweight KG. Required first relation: `自然资规〔2023〕6号` 第十条: `自然资源部 -> 负责 -> 本级已颁发勘查许可证或采矿许可证的矿产资源储量评审备案`; `省级自然资源主管部门 -> 负责 -> 其他矿产资源储量评审备案`; include clause id, source URL/document id, and quote. | `kg_entities`, `kg_relations`, build script updates |
| T013 | KB agent | done | Add regression coverage for policy authority retrieval: the question `我的采矿证是自然资源部颁发的，我的储量评审应该去哪个机构` must rank `自然资规〔2023〕6号` 第十条 in the top 3 `/knowledge/search` results, with a short quote containing `自然资源部负责本级已颁发勘查许可证或采矿许可证`. | `scripts/run_kb_regression.py` |
| T014 | KB agent | done | Hydrate official source URLs for policy/system documents, not only standards. At minimum, `自然资规〔2023〕6号《自然资源部关于深化矿产资源管理改革若干事项的意见》` and all MNR `official_fulltext` policy documents returned by `/knowledge/search` must include stable official `url` and `source_platform`; answers citing policy clauses should render clickable source links just like standards. Add regression coverage for policy authority questions to assert the top cited policy source has a non-empty official URL. | `src/mining_qa/knowledge_store.py`, MNR policy manifests/DB hydration, `scripts/run_kb_regression.py` |
| T015 | KB agent | done | Add a maintainable `domain_lexicon` capability for intent-aware retrieval. First stage may be a SQLite table, JSON/YAML config, or equivalent build artifact, but it must preserve user expression, canonical term, intent label, positive expansions, negative terms, evidence-required patterns, priority, and status. Seed at least the high-value entries documented in `docs/KNOWLEDGE_BASE_REQUIREMENTS.md` section 6.8, especially `采矿证/采矿许可证`, `储量报告评审/矿产资源储量评审备案`, `大型金矿` as background not authority basis, `沙金/砂金/金属砂矿类`, `工程距离/勘查工程间距`, and `矿体外推/资源量类型与矿体外推`. Add regression cases showing that authority questions are routed to policy authority evidence and unrelated oil/gas or coalbed methane evidence is downranked for solid-mineral questions. | `src/mining_qa/domain_lexicon.json`, retrieval normalization in `knowledge_store.py`, `scripts/run_kb_regression.py`, runbook/status docs |
| T016 | KB agent | done | Treat `/home/nalanmading/下载/0. 继续有效文件.xls` as the authoritative allowlist for previously web-crawled MNR files published before 2026. Completely remove every in-scope pre-2026 crawled document whose normalized document number is absent from the workbook, remove all active derived data, and prevent future re-ingestion. | `scripts/govern_mnr_policy_allowlist.py`, refreshed private `data/knowledge_base/`, deletion manifest, regression results, `coordination/kb_agent_status.md` |
| T017 | KB agent | done | Ingest the 40 structured MNR mineral-service guides under `/home/nalanmading/下载/2. geowiki` as a distinct official service-guide corpus, preserving sections, application-material tables, official source links, attachment links, and flowchart links. | idempotent ingest script, refreshed private `data/knowledge_base/`, service-guide manifest, regression results, `coordination/kb_agent_status.md` |
| T018 | KB agent | done | Parse and index `自然资规〔2023〕4号` attachment 4, `采矿权申请资料清单及要求.doc`, as structured evidence linked to the parent policy and relevant service guides. | attachment parser, structured material chunks/tables, refreshed private indexes, regressions, `coordination/kb_agent_status.md` |

## T018 Execution Contract

### Dependency and source

- Start only after the T016 cleanup and T017 service-guide ingestion/index rebuild are complete.
- Parent document: `policy-d4869b5b5bf8804f`, `自然资规〔2023〕4号《自然资源部关于进一步完善矿产资源勘查开采登记管理的通知》`.
- Authoritative local attachment: `data/knowledge_base/raw/mnr_policy/attachments/policy-d4869b5b5bf8804f/采矿权申请资料清单及要求.doc.doc`.
- Official attachment URL: `https://f.mnr.gov.cn/202305/P020230512660474974800.doc`.
- Parent official URL: `https://f.mnr.gov.cn/202305/t20230512_2786192.html`.

### Parsing and schema requirements

- Parse the legacy Word attachment with a structured document/table parser. Do not infer missing rows from model knowledge.
- Preserve the four application types declared by the parent policy: new establishment, extension, change, and cancellation.
- Create a stable attachment document or governed child artifact linked to the parent with a relation such as `ATTACHMENT_OF` / `IMPLEMENTS_MATERIAL_LIST_FOR`.
- Store each application type as a separately retrievable section. Preserve table headers, material names, required form/copy rules, conditions, notes, and any conditional branches as structured table data.
- For the extension section, create concise row-level or logically grouped evidence chunks so `采矿证延续需要提交什么材料？` can return the complete applicable list without mixing new-establishment, change, or cancellation materials.
- Keep `visibility='internal'`, preserve the official attachment URL and parent URL, and expose only short directly relevant evidence quotes through `/knowledge/search`.
- Link matching T017 service guides to this attachment as supporting sources without duplicating contradictory material rows. If a guide and attachment differ by jurisdiction or date, preserve both scope/date fields and report the difference for review.

### Retrieval and acceptance checks

- `采矿证延续需要提交什么材料？` must rank the attachment's extension section and/or the matching official service guide above `自然资规〔2023〕6号` and unrelated old replies.
- `采矿证办理应该依据哪个文件？` must rank parent `自然资规〔2023〕4号` and may cite attachment 4/service-guide details as supporting evidence.
- Assert that new-establishment, extension, change, and cancellation rows remain isolated by application type.
- Returned sources must contain clickable official URLs and identify whether the evidence comes from the parent policy, attachment 4, or a service guide.
- Rebuild FTS, local vectors, configured dense embeddings, and KG relations only after inserting the attachment evidence. Run `PRAGMA integrity_check`, KB regression, and the two questions above through `/api/ask`.
- Do not deploy the refreshed private DB directly. Report `cloud_sync_required=true` for main-agent review.

### Required completion report

- Append a dated T018 entry to `coordination/kb_agent_status.md` with parser used, section/table/row counts, attachment-parent/service-guide relations, index deltas, integrity result, regression results, and `cloud_sync_required=true`.
- Record any ambiguous merged cells, unreadable fields, or conflicts in `coordination/kb_agent_questions.md`; otherwise state `Blocked: None`.

## T016 Execution Contract

### Authoritative input

- Workbook: `/home/nalanmading/下载/0. 继续有效文件.xls`.
- Sheet: `Sheet1`.
- Header row: row 3; relevant columns are `文件名称` and `文号`.
- Verified workbook shape at task issue time: 222 non-empty data rows, all 222 rows have a document number, document-number years range from 1986 through 2024.
- Use the isolated `.xls` interpreter `/home/nalanmading/.venvs/codex/bin/python` with `xlrd`; do not install legacy Excel packages into the system Python or project venv.

### Cleanup scope

- Only classify documents previously crawled from the web for the MNR policy/law corpus, normally identified by `source_type='official_fulltext'` together with the MNR policy source trace/platform/category.
- Apply deletion only to documents with a confirmed publication date earlier than `2026-01-01`.
- Preserve every document published on or after `2026-01-01`; this workbook rule does not decide their validity.
- Do not touch governed technical standards, OCR compilation standards, supplemental standards, user uploads, candidates, or unrelated local documents.
- If `documents.standard_no` is empty, first recover the official `文号` from bibliographic/source metadata or the crawled source. Completion requires resolving the publication date and document number for every in-scope record; do not silently classify unknown metadata as deleted or retained.

### Matching rule

- Build an allowlist from workbook `文号`, normalizing Unicode width, whitespace, bracket variants, and dash variants before exact comparison.
- A pre-2026 crawled document is retained only when its normalized official document number exactly matches an allowlist number.
- Use title comparison only as a diagnostic cross-check. Do not use fuzzy title similarity to override a document-number mismatch.
- Record duplicate document numbers, one-to-many matches, title conflicts, and metadata repairs in the report.

### Required deletion

- Produce a dry-run metadata-only manifest before mutation, including document ID, title, document number, publication date, source URL, deletion reason, and counts of dependent records. Do not copy deleted full text into the manifest.
- Create one private timestamped SQLite rollback backup outside the serving path before the destructive transaction and report its path.
- Remove excluded records from the active `documents`, `chunks`, `chunks_fts`, `chunk_vectors`, `chunk_embeddings`, and all KG relations/entities derived only from deleted documents or chunks.
- Remove or update associated active raw HTML, downloaded attachments, processed artifacts, and MNR manifests when they are exclusively owned by deleted documents. Do not remove a file still referenced by a retained document.
- Update document/chunk/table counts and any registries so deleted records cannot appear in `/knowledge/search`, `/knowledge/standards`, direct chunk lookup, vector search, graph retrieval, or future rebuilds.
- Modify the MNR ingest/governance flow so a later crawl cannot re-ingest an excluded pre-2026 document. The same workbook-derived allowlist and cutoff rule must be applied before insertion.

### Acceptance checks

- Run `PRAGMA integrity_check` and report `ok`.
- Assert that every remaining in-scope pre-2026 crawled document has a normalized document number present in the 222-row allowlist.
- Assert zero remaining chunks, FTS rows, local vectors, dense embeddings, or KG evidence references for deleted document/chunk IDs.
- Assert 2026-and-later crawled documents are unchanged by this rule.
- Keep and regression-test `自然资规〔2023〕6号`, which is present in the workbook; the policy-authority question must continue to cite its tenth section correctly.
- Run the existing KB/API regression suites and add a cleanup-specific repeatable validation command or test.
- Do not deploy the refreshed DB to the cloud directly. Mark `cloud_sync_required=true` in the completion report so the PRD/main agent can review and synchronize it.

### Required completion report

Append a dated T016 completion entry to `coordination/kb_agent_status.md` containing:

- Workbook row count and normalized allowlist count.
- In-scope crawled-document count before cleanup.
- Retained, deleted, metadata-repaired, ambiguous, and 2026+ untouched counts.
- Deleted document IDs/titles/document numbers in a metadata-only manifest path.
- Before/after counts for documents, chunks, FTS, local vectors, dense embeddings, KG entities, and KG relations.
- Raw/attachment/artifact files deleted or retained because of shared references.
- Rollback backup path, integrity-check result, regression commands/results, and `cloud_sync_required=true`.
- Any blocker or unresolved ambiguity in `coordination/kb_agent_questions.md`; otherwise explicitly state `Blocked: None`.

## T017 Execution Contract

### Authoritative input

- Source directory: `/home/nalanmading/下载/2. geowiki`.
- Verified source shape at task issue time: 41 Markdown files totaling about 488 KiB.
- Ingest exactly 40 numbered guide files, `01_*.md` through `40_*.md`.
- Treat `_INDEX.md` as the corpus manifest/catalog only; do not expose it as a substantive answer source or duplicate its full item list into ordinary evidence results.
- `_INDEX.md` declares 40 items, 920 standard sections, 46 attachment links, 40 flowchart links, and 40 application-material tables.

### Relationship to T016

- T016 and T017 may be analyzed and scripted in parallel, but SQLite mutations and index rebuilds must be serialized.
- Execute the T016 cleanup transaction first, then ingest T017, then perform one final coherent rebuild/cleanup of FTS, local vectors, dense embeddings, and KG.
- T017 guides are explicitly outside the T016 deletion scope even though their URL dates are before 2026. They are a separate `www.mnr.gov.cn/bsznxxk/fwzn` service-guide corpus, not the MNR normative policy/law corpus governed by the valid-document workbook.

### Document mapping

- Create one stable document per numbered Markdown file, using `source_url` or `source_page_id` as the idempotent identity key. Re-running the ingest must update/replace the same document instead of creating duplicates.
- Use a distinct document type such as `service_guide` / `administrative_service_guide`, `source_type='official_fulltext'`, `text_access='html_text'`, `visibility='internal'`, and a source platform identifying the official Natural Resources Ministry service-guide site.
- Preserve YAML metadata including title, publisher, category, source URL, catalog URL, online-service URL, source page ID, retrieval date, tags, content hash, section/attachment/link counts, and quality/correction metadata.
- `url_date` is derived from the URL and is not an asserted publication date. Store it in bibliographic/source metadata and do not write it into `publish_date` unless an official page explicitly confirms the publication date.
- Keep all 40 guides distinct. Do not merge similarly named older/newer registration and application guides solely because their topics overlap.

### Chunking and structured evidence

- Preserve each official guide heading as a stable section path. Expected guide structure is 23 official sections per guide, including scope, approval basis, accepting/deciding authority, application conditions, application materials, process, time limit, result, consultation, office information, and flowchart.
- Create section-level evidence chunks with concise quotes and no page number. Empty official sections marked `原网页未提供内容` may be retained as metadata but should not rank as answer evidence.
- Parse every GFM application-material table into a structured table chunk with headers, rows, notes, and the parent section path. Do not flatten table columns into ambiguous prose.
- Preserve official attachment and flowchart URLs as link metadata. Do not download or OCR linked attachments as part of T017 unless the user separately assigns that work.
- Preserve transparent link-correction records from the Markdown source and do not silently rewrite additional legal/source links.

### Retrieval and indexing

- Insert all answerable chunks into FTS and update local hash vectors, configured dense embeddings, and KG only after T016 cleanup and T017 ingestion are complete.
- Add useful service-guide entities/relations where evidence supports them, such as `Guide -> APPLIES_TO -> Matter`, `Guide -> ACCEPTED_BY -> Organization`, `Guide -> DECIDED_BY -> Organization`, `Guide -> REQUIRES_MATERIAL -> Material`, and `Guide -> HAS_TIME_LIMIT -> Duration`.
- Ensure guide questions prefer `service_guide` evidence over unrelated standards or policy documents while still allowing cited policy/standard links to remain supporting references.
- Every returned guide source must include its clickable official `source_url` and the correct guide title.

### Acceptance checks

- Assert exactly 40 service-guide documents are active and there are no duplicate `source_url` or `source_page_id` identities.
- Reconcile expected source totals: 920 official sections, 40 application-material tables, 46 attachment links, and 40 flowchart links; explain any parser count difference.
- Add regression questions covering at least:
  - `自然资源部探矿权首次登记需要哪些材料？`
  - `采矿许可变更开采方式怎么办理？`
  - `矿产资源储量评审备案需要提交什么材料？`
  - `矿产资源开采方案的办结时限是多久？`
- Regression answers must cite the matching guide, return only directly relevant sections/table rows, and include the official source URL.
- Run `PRAGMA integrity_check`, existing KB/API regression suites, and T016 cleanup validation after the final index rebuild.
- Do not deploy the refreshed DB to the cloud directly. Include the combined post-T016/T017 DB state and `cloud_sync_required=true` in the completion report.

### Required completion report

Append a dated T017 entry to `coordination/kb_agent_status.md` containing:

- Source file count, accepted/rejected file count, and any metadata/parser repairs.
- Documents, section chunks, table chunks, FTS rows, local vectors, dense embeddings, KG entities, and KG relations added.
- Reconciled section/table/attachment/flowchart totals and duplicate checks.
- Manifest and ingest-script paths, integrity result, regression commands/results, and `cloud_sync_required=true`.
- Confirmation that T017 documents were excluded from the T016 allowlist cleanup scope.
- Any blocker in `coordination/kb_agent_questions.md`; otherwise explicitly state `Blocked: None`.

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
