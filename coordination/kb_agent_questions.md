# KB Agent Questions

KB agent should write product/API/schema questions here. PRD agent will answer in `prd_agent_answers.md`.

## Open Questions

No open questions yet.

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
