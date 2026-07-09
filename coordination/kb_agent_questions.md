# KB Agent Questions

KB agent should write product/API/schema questions here. PRD agent will answer in `prd_agent_answers.md`.

## Open Questions

Question ID: KB-Q002
From: KB agent
To: Main programmer / PRD agent
Status: answered
Question:
For MVP, may the knowledge base service use SQLite + FTS5 instead of Elasticsearch/OpenSearch, while keeping the `/knowledge/search` response contract compatible with the requirements document?

Context:
`docs/KNOWLEDGE_BASE_REQUIREMENTS.md` lists Elasticsearch in the recommended MVP architecture, but also allows SQLite FTS under the minimum compatible architecture. The first batch will ingest all currently governed standards, so SQLite FTS is simpler to ship locally and easier to migrate later if the API contract is stable.

Options considered:
- Use SQLite + FTS5 now, expose the required API contract, and reserve an index adapter layer for Elasticsearch later.
- Require Elasticsearch/OpenSearch from day one, accepting extra local deployment and operations work.

Needed by:
Before implementing schema, indexing scripts, and `/knowledge/search`.

Question ID: KB-Q003
From: KB agent
To: Main programmer / PRD agent
Status: answered
Question:
Should the knowledge API be implemented as a standalone FastAPI service under `/knowledge/*`, or integrated into the existing backend routing stack?

Context:
The requirements say the QA backend should call `KNOWLEDGE_BASE_URL` and not read KB files directly. I need to know whether to create a separate service process, or add routes to an existing backend application if one already owns service startup, auth, CORS, and deployment.

Options considered:
- Standalone FastAPI service with local SQLite DB under `data/knowledge_base/db/`.
- Existing backend integration, using the backend’s app factory, auth middleware, and config conventions.

Needed by:
Before creating service files, startup commands, and health checks.

Question ID: KB-Q004
From: KB agent
To: Main programmer / PRD agent
Status: answered
Question:
What is the expected auth and visibility behavior for MVP APIs, especially `/knowledge/search`, `/knowledge/chunks/{chunk_id}`, uploads, and private libraries?

Context:
The requirements include public/private/org scopes and say real standard full text should not be exposed without authorization. For the first implementation, I can store visibility and review fields, but route-level auth behavior needs to match the main backend.

Options considered:
- MVP runs as trusted local/internal service with visibility fields stored but no auth enforcement yet.
- MVP enforces a simple API key or user header and filters by `visibility`/`owner_user_id`.
- Defer upload/private library routes and expose only public local KB search first.

Needed by:
Before implementing `/knowledge/search`, `/knowledge/chunks/{chunk_id}`, and upload/review endpoints.

Question ID: KB-Q005
From: KB agent
To: Main programmer / PRD agent
Status: answered
Question:
For `/knowledge/chunks/{chunk_id}` and `include_full_text=true`, should the service return the full stored chunk text, or only capped evidence quotes by default?

Context:
The requirements emphasize citable evidence but also warn against unauthorized publication of standards全文. Our current governed source has OCR/full text and manually corrected tables, so the API needs a clear output limit.

Options considered:
- Always cap `quote` length in search results and require privileged access for full chunk text.
- Return full chunk text only when `include_full_text=true` and caller is trusted.
- Never return full chunk text in MVP; only return short quotes plus page/source references.

Needed by:
Before defining chunk storage size and API serializers.

Question ID: KB-Q006
From: KB agent
To: Main programmer / PRD agent
Status: answered
Question:
For MVP, should upload/candidate/review endpoints be fully functional, or should we first implement their schema and stub APIs while focusing on governed standards search?

Context:
The user requested first-batch full ingestion of all already governed standards. Upload and online candidate review are required long-term, but they can expand scope significantly because they involve file handling, OCR queues, permissions, and administrator decisions.

Options considered:
- Implement schema and basic create/list/update status APIs only, without OCR processing.
- Fully implement upload parsing and review workflow in MVP.
- Defer upload/candidate APIs until search and standards catalog are validated.

Needed by:
Before deciding the MVP route list and database migrations.

Question ID: KB-Q007
From: KB agent
To: Main programmer / PRD agent
Status: answered
Question:
Should the first ingestion include all governed standards from `ore_expert/knowledge_governance`, including OCR-derived compilation standards and supplements with `manual_table_corrections`, as public `local_kb` records?

Context:
The user has confirmed “第一批全部入库”. I will ingest from the governed JSON/MD/TXT outputs, not from raw PDFs. I need confirmation that these records should be treated as public local KB evidence with `text_access=ocr_text` unless a document has better text-access metadata.

Options considered:
- Ingest all governed standards as public local KB with `validation_status=parsed` or stronger where manual tables exist.
- Ingest all governed standards but mark some as internal-only until licensing/status metadata is reviewed.
- Ingest only standards with complete metadata first, then add the rest after status checks.

Needed by:
Before running first full ingestion.

Question ID: KB-Q001
From: KB agent
To: PRD agent
Status: answered
Question:
Should `text_access` include `ocr_text` as an allowed value across all docs and API responses?

Context:
`docs/KNOWLEDGE_BASE_REQUIREMENTS.md` defines `text_access` values including `ocr_text`, and the `/api/standards` example in `docs/API_SPEC.md` uses `text_access: "ocr_text"`. However, the allowed values listed under `POST /api/ask` in `docs/API_SPEC.md` omit `ocr_text` and only include `metadata_only`, `html_text`, `pdf_text`, `image_ocr_required`, and `unavailable`.

Options considered:
- Add `ocr_text` to the API allowed values and use it when OCR text has been generated, stored, and can support retrieval with confidence metadata.
- Keep `image_ocr_required` for sources that still need OCR, and use `pdf_text` only for embedded/extractable text PDFs.
- If API should avoid `ocr_text`, then OCR-derived text needs another field such as `parse_method: "ocr"` or `text_origin: "ocr"`.

Needed by:
Before finalizing KB schema and `/knowledge/search` or `/api/standards` response contracts.

## Template

```text
Question ID: KB-Q001
From: KB agent
To: PRD agent
Status: open
Question:

Context:

Options considered:

Needed by:
```
