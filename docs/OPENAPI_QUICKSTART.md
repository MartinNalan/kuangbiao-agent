# OpenAPI Quickstart

This document is for developers who call the public QA API. It does not describe the private `/knowledge/*` service.

## Public Endpoints

Public or commercial deployments should expose only:

- `GET /health`
- `POST /api/ask`
- `GET /api/standards`
- `POST /api/feedback`
- `GET /api/usage`

Do not expose `/knowledge/*` to public clients. It is an internal knowledge-base service used by the QA backend.

## Interactive Docs

When the API server is running, FastAPI provides:

```text
http://127.0.0.1:18080/docs
http://127.0.0.1:18080/redoc
http://127.0.0.1:18080/openapi.json
```

Use these URLs for local development only. Production deployments should decide whether `/docs` and `/redoc` remain public.

## Authentication

Use either header form:

```text
X-API-Key: dev-local-key
```

or:

```text
Authorization: Bearer dev-local-key
```

`dev-local-key` is a local development placeholder. Do not use it in production.

## Ask

```bash
curl -sS -X POST http://127.0.0.1:18080/api/ask \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-local-key' \
  -d '{"question":"哪个标准规定了金矿基本工程间距？"}'
```

Important response fields:

- `status`: `answered`, `queued_for_enrichment`, `out_of_scope`, or `insufficient_evidence`.
- `answer`: final user-facing answer.
- `sources`: capped evidence snippets and source links.
- `retrieval`: full-text, vector, graph, and web hit counts.
- `knowledge_gap_task`: present only when an in-scope question lacks usable evidence.

## Standards Catalog

```bash
curl -sS 'http://127.0.0.1:18080/api/standards?standard_no=DZ/T%200205-2020&page_size=5' \
  -H 'X-API-Key: dev-local-key'
```

Use this endpoint to check whether a standard is already available to the QA service. It does not expose the raw knowledge base.

## Feedback

Clients should store the `session_id` returned by `/api/ask` and submit feedback:

```bash
curl -sS -X POST http://127.0.0.1:18080/api/feedback \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-local-key' \
  -d '{
    "session_id":"session-id-from-ask",
    "rating":"unsatisfied",
    "question":"哪个标准规定了金矿基本工程间距？",
    "reason":"wrong_clause",
    "comment":"引用条款需要复核"
  }'
```

## Usage

```bash
curl -sS http://127.0.0.1:18080/api/usage \
  -H 'X-API-Key: dev-local-key'
```

The response is scoped to the current API key.

## Error Patterns

Missing or invalid API key:

```json
{
  "detail": {
    "code": "UNAUTHORIZED",
    "message": "Missing or invalid API key."
  }
}
```

Rate limited:

```json
{
  "detail": {
    "code": "RATE_LIMITED",
    "message": "API rate limit exceeded.",
    "limit_per_minute": 30,
    "current_count": 31,
    "backend": "redis",
    "retry_after_seconds": 20
  }
}
```

Out-of-scope questions return HTTP 200 with `status=out_of_scope`; they are rejected before KB retrieval or model reasoning.

