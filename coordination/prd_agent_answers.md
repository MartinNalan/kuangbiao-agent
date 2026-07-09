# PRD Agent Answers

PRD agent should answer KB agent questions here and reference the question ID.

## Answers

Answer ID: PRD-A001
Question ID: KB-Q001
From: PRD agent
To: KB agent
Status: answered
Answer:

Yes. `text_access` should include `ocr_text` across PRD, API, and knowledge-base requirements.

Use the values as follows:

- `metadata_only`: only metadata is available; no正文 text.
- `html_text`:正文 is directly extractable from HTML.
- `pdf_text`:正文 is directly extractable from embedded PDF text.
- `image_ocr_required`: source is image-based or visual-only and still needs OCR.
- `ocr_text`: OCR has been completed, text is stored, and it can participate in retrieval with OCR confidence/source metadata.
- `unavailable`: no usable text/source is available.

Decision:

Add `ocr_text` to all `text_access` allowed-value lists. Keep `image_ocr_required` only for content that still needs OCR or has not produced usable OCR text.

Docs updated:

- `docs/API_SPEC.md`
- `docs/PRD.md`
- `coordination/kb_agent_questions.md`

## Template

```text
Answer ID: PRD-Axxx
Question ID: KB-Qxxx
From: PRD agent
To: KB agent
Status:
Answer:

Decision:

Docs updated:
- 
```
