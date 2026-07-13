# OpenAPI Quickstart

This document is for developers who call the public QA API. It does not describe the private `/knowledge/*` service.

## Public Endpoints

Public deployments should expose only:

- `GET /health`
- `POST /api/ask`
- `POST /api/research/tasks`
- `GET /api/research/tasks/{task_id}`
- `GET /api/research/tasks/{task_id}/result`
- `POST /api/research/tasks/{task_id}/cancel`
- `GET /api/standards`
- `POST /api/feedback`
- `GET /api/usage`

Do not expose `/knowledge/*` to public clients. It is an internal knowledge-base service used by the QA backend.

Browser users register with an invitation plus an email verification code, log in, and create their own API Key from the `/developer` page. Web questions and API calls share the same account daily quota.

## Interactive Docs

When the API server is running, FastAPI provides:

```text
http://127.0.0.1:18080/docs
http://127.0.0.1:18080/redoc
http://127.0.0.1:18080/openapi.json
```

Use these URLs for local development only. Production deployments should decide whether `/docs` and `/redoc` remain public.

## Authentication

Use either header form with the API Key shown once by the developer console:

```text
X-API-Key: kb_live_xxx
```

or:

```text
Authorization: Bearer kb_live_xxx
```

The generated key is not stored in plaintext. `API_KEYS` and the older JSON registry remain available only for internal regression and compatibility.

## Ask

```bash
curl -sS -X POST http://127.0.0.1:18080/api/ask \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: kb_live_xxx' \
  -d '{"question":"哪个标准规定了金矿基本工程间距？"}'
```

Important response fields:

- `status`: `answered`, `queued_for_enrichment`, `out_of_scope`, `insufficient_evidence`, or `clarification_required`.
- `answer`: final user-facing answer.
- `sources`: capped evidence snippets and source links.
- `retrieval`: full-text, vector, graph, and web hit counts.
- `knowledge_gap_task`: present only when an in-scope question lacks usable evidence.
- `request_id`: exact request identifier for quota accounting and feedback.
- `quota`: whether this request consumed a use and the remaining daily count.

When `status=clarification_required`, the request has not consumed quota and has not searched the private KB. Present `clarification.options` to the user, then submit the selected option's complete `question` as a new request:

```json
{
  "status": "clarification_required",
  "quota_cost": 0,
  "clarification": {
    "interpreted_question": "采空区怎么处理？",
    "reason": "不同处理目标会对应不同标准和条款范围。",
    "allow_free_text": true,
    "options": [
      {
        "option_id": "option_1",
        "label": "稳定性评价",
        "question": "采空区稳定性评价应依据哪些标准？"
      },
      {
        "option_id": "option_2",
        "label": "积水治理",
        "question": "采空区积水治理应依据哪些标准？"
      }
    ]
  },
  "quota": {
    "consumed": false,
    "consumed_units": 0
  }
}
```

## Deep Research

Create an asynchronous cross-document task. A new task costs 3 quota units:

```bash
curl -sS -X POST http://127.0.0.1:18080/api/research/tasks \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: kb_live_xxx' \
  -d '{
    "question":"不同矿种规范对矿体无限外推所依据的间距有哪些代表性差异？"
  }'
```

If the deep question still requires clarification, task creation returns HTTP 200 with `status=clarification_required`; no task ID is created and no quota is reserved. A confirmed deep question returns HTTP 202 and starts the normal research workflow.

Poll progress:

```bash
curl -sS http://127.0.0.1:18080/api/research/tasks/research_xxx \
  -H 'X-API-Key: kb_live_xxx'
```

Read the final comparison matrix, evidence, coverage, and quota settlement:

```bash
curl -sS http://127.0.0.1:18080/api/research/tasks/research_xxx/result \
  -H 'X-API-Key: kb_live_xxx'
```

To upgrade the same basic answer, pass its `request_id` as `source_request_id` together with the same `session_id` and question. The server validates ownership and reserves only 2 additional units, so the question costs 3 units in total.

## Standards Catalog

```bash
curl -sS 'http://127.0.0.1:18080/api/standards?standard_no=DZ/T%200205-2020&page_size=5' \
  -H 'X-API-Key: kb_live_xxx'
```

Use this endpoint to check whether a standard is already available to the QA service. It does not expose the raw knowledge base.

## Feedback

Clients should store the `session_id` returned by `/api/ask` and submit feedback:

```bash
curl -sS -X POST http://127.0.0.1:18080/api/feedback \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: kb_live_xxx' \
  -d '{
    "session_id":"session-id-from-ask",
    "request_id":"request-id-from-ask",
    "rating":"unsatisfied",
    "question":"哪个标准规定了金矿基本工程间距？",
    "reason":"wrong_clause",
    "comment":"引用条款需要复核"
  }'
```

## Usage

```bash
curl -sS http://127.0.0.1:18080/api/usage \
  -H 'X-API-Key: kb_live_xxx'
```

The response is scoped to the owning account. All API keys created by that account share its daily quota with browser questions.

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

Out-of-scope questions return HTTP 200 with `status=out_of_scope`; they are rejected before KB retrieval or model reasoning and do not consume the daily quota. Rate limiting and audit logging still apply.

An exhausted daily quota returns HTTP 429 with `detail.code=DAILY_QUOTA_EXCEEDED`. The response includes the current quota snapshot.
