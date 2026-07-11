# API Spec

## API Boundary

Cloud deployments should expose only the controlled QA API surface:

- `GET /health`
- `POST /api/ask`
- `GET /api/standards`
- `POST /api/feedback`
- `GET /api/usage`

The same origin also provides browser account routes under `/api/auth/*`, `/api/account/*`, and `/api/conversations/*`. Administrator routes under `/api/admin/*` require an authenticated admin browser session. None of these routes exposes the private knowledge base.

The knowledge service routes under `/knowledge/*` are internal backend-to-backend contracts. They must not be exposed as public internet APIs because they can reveal knowledge-base structure, internal metadata, chunks, candidates, or full-text retrieval behavior. Public clients should receive only the capped evidence snippets, source links, usage data, and task identifiers returned by `/api/*`.

When the API server is running, interactive OpenAPI docs are available at `/docs`, `/redoc`, and `/openapi.json`. Developer-oriented examples are in `docs/OPENAPI_QUICKSTART.md`.

Registered users create API keys from the developer console. User API keys, browser sessions, passwords, invitation codes, and email verification codes are stored only as hashes in the application database. Plaintext API keys and invitation codes are shown only once. Legacy `API_KEYS` and the JSON registry remain an internal compatibility path and should be disabled for public clients.

## Browser Registration and Login

- `POST /api/auth/email-code`: validates the invitation and sends a six-digit code to the supplied email address.
- `POST /api/auth/register`: verified email, display name, password, invitation code, and email code.
- `POST /api/auth/login`: creates an HttpOnly browser session cookie.
- `POST /api/auth/logout`: revokes the current session.
- `GET /api/auth/me`: returns `authenticated=false` when no valid browser session exists.

New invite-only users receive the configured daily request limit, defaulting to 10 requests per day in `Asia/Shanghai`.

Send a code only after the user has supplied a valid invitation:

```json
POST /api/auth/email-code
{
  "email": "user@example.com",
  "invite_code": "KB-XXXX-XXXX-XXXX"
}
```

Complete registration with the same email and invitation:

```json
POST /api/auth/register
{
  "email": "user@example.com",
  "display_name": "测试用户",
  "password": "a-strong-password",
  "invite_code": "KB-XXXX-XXXX-XXXX",
  "email_code": "123456"
}
```

Verification codes expire after 10 minutes by default. Sending is limited by per-email cooldown and a rolling daily cap. Production uses AgentMail with the `geowiki` inbox; API tokens and verification secrets exist only in `.env`.

## POST /api/ask

Authentication:

```text
X-API-Key: your-api-key
```

or:

```text
Authorization: Bearer your-api-key
```

### Request

```json
{
  "question": "哪个标准规定了金矿基本工程间距？",
  "session_id": "optional-session-id",
  "filters": {
    "domain": "mineral_resources",
    "document_types": ["standard", "specification"]
  }
}
```

### Response

```json
{
  "answer": "根据当前知识库检索结果，...",
  "session_id": "session-id",
  "request_id": "req_xxx",
  "status": "answered",
  "sources": [
    {
      "title": "文件名称",
      "standard_no": "GB/T 17766-2020",
      "chapter": "章节或条款号",
      "page": 12,
      "quote": "相关原文片段",
      "score": 0.92,
      "source_type": "official_fulltext",
      "text_access": "pdf_text",
      "url": "https://example.com/source"
    }
  ],
  "retrieval": {
    "full_text_hits": 5,
    "vector_hits": 8,
    "graph_hits": 2,
    "web_hits": 1
  },
  "limitations": {
    "has_clause_level_evidence": true,
    "notes": []
  },
  "knowledge_gap_task": null,
  "confidence": "medium",
  "quota": {
    "date": "2026-07-11",
    "daily_limit": 10,
    "bonus": 0,
    "effective_limit": 10,
    "used": 1,
    "reserved": 0,
    "remaining": 9,
    "consumed": true
  }
}
```

The service atomically reserves one request before processing. `answered`, `insufficient_evidence`, and `queued_for_enrichment` consume it. `out_of_scope` and `system_error` release the reservation without consumption. Browser sessions and every API Key owned by the same user share one account quota.

`status` suggested values:

- `answered`: 已根据证据回答。
- `insufficient_evidence`: 问题属于服务范围，但没有条款级证据。
- `out_of_scope`: 问题不属于矿产资源标准规范相关服务范围。
- `queued_for_enrichment`: 已返回证据不足，同时创建补库任务。

When `status=out_of_scope`, the service must not call KB retrieval, web supplement, OCR, multimodal parsing, or long LLM reasoning. It returns a fixed refusal message, does not create a knowledge-gap task, and does not consume the daily quota. Account/IP/API-Key rate limiting and audit logging still apply.

`source_type` allowed values:

- `local_kb`
- `official_metadata`
- `official_fulltext`
- `official_visual`
- `third_party_candidate`
- `unavailable`

`text_access` allowed values:

- `metadata_only`
- `html_text`
- `pdf_text`
- `image_ocr_required`
- `ocr_text`
- `unavailable`

When `has_clause_level_evidence` is `false`, the answer must not present a normative conclusion as if it came from standard正文.

When local KB evidence is insufficient, `/api/ask` may call the web supplement module:

1. Use the LLM only to extract likely standard numbers/names for search.
2. Verify candidates against official platforms such as `std.samr.gov.cn` and `nrsis.org.cn`.
3. Return official metadata or reader links in `sources`.
4. Keep `has_clause_level_evidence=false` unless retrievable正文 evidence is available.
5. If OCR or page parsing is triggered, store the result as a candidate record first; do not add it to the service-visible KB scope until admin approval.

MVP default: synchronous web supplement is disabled unless `ENABLE_SYNC_WEB_SUPPLEMENT=true`. The default insufficient-evidence path should return quickly and create a knowledge-gap task.

If the question is in scope but no clause-level evidence is available, the response may include:

```json
{
  "knowledge_gap_task": {
    "task_id": "kgap_20260709_0001",
    "status": "queued",
    "type": "knowledge_gap",
    "message": "已记录为知识库缺口任务，后台将低优先级补充官方来源和 OCR 候选。"
  }
}
```

Knowledge-gap tasks are only created for in-scope questions. Out-of-scope questions must not be collected as demand signals.

## POST /api/feedback

### Request

```json
{
  "session_id": "session-id",
  "rating": "unsatisfied",
  "question": "关于矿体外推所依据的距离，是否存在不同标准规定不一致的情况？",
  "reason": "answer_too_vague",
  "comment": "需要明确列出不同标准采用的距离基准"
}
```

`rating` values:

- `satisfied`
- `unsatisfied`

`reason` suggested values:

- `wrong_standard`
- `wrong_clause`
- `missing_evidence`
- `quote_too_long`
- `answer_too_vague`
- `format_issue`
- `other`

API clients should store the `session_id` returned by `/api/ask` and submit feedback against that ID. This lets the service collect blind spots for later targeted retrieval, chunking, and prompt/rule improvements.

Clients should also send the returned `request_id` so feedback can be tied to the exact answer within a multi-message conversation.

### Response

```json
{
  "ok": true,
  "feedback_id": "fb_xxx",
  "review_lane": "kb_review",
  "status": "open"
}
```

Unsatisfied feedback is classified into `product`, `kb_review`, or `manual_review`. Administrators can list it with `GET /api/admin/feedback` and update workflow state through `POST /api/admin/feedback/{feedback_id}/status`. Knowledge-content changes require KB/admin review; the feedback endpoint never writes directly into the private KB.

## GET /api/standards

查询知识库中是否已有某个标准。

Authentication is the same as `/api/ask`.

### Request Query

```text
q=方解石
standard_no=DZ/T 0321-2018
status=current
text_access=ocr_text
page=1
page_size=20
```

### Response

```json
{
  "items": [
    {
      "document_id": "doc-001",
      "title": "方解石矿地质勘查规范",
      "standard_no": "DZ/T 0321-2018",
      "document_type": "industry_standard",
      "status": "current",
      "source_type": "official_visual",
      "text_access": "ocr_text",
      "validation_status": "parsed",
      "can_answer": true,
      "publish_date": "2018-07-05",
      "implementation_date": "2018-11-01",
      "ingestion_time": "2026-07-08T00:00:00+08:00"
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total": 1
  }
}
```

## GET /api/usage

查询当前账号的每日配额、调整记录、调用量和限流配置。网页会话和该账号创建的全部 API Key 共用同一配额。

Authentication is the same as `/api/ask`.

### Response

```json
{
  "scope": "account",
  "rate_limit": {
    "enabled": true,
    "limit_per_minute": 30,
    "backend": "redis"
  },
  "quota_policy": {
    "mode": "daily_account_quota",
    "timezone": "Asia/Shanghai",
    "web_and_api_keys_shared": true,
    "system_errors_refunded": true,
    "out_of_scope_not_consumed": true
  },
  "usage": {
    "quota": {"daily_limit": 10, "bonus": 0, "effective_limit": 10, "used": 2, "remaining": 8},
    "total_calls": 2,
    "consumed_calls": 2,
    "adjustments": []
  }
}
```

An exhausted daily quota returns HTTP 429 with code `DAILY_QUOTA_EXCEEDED` and the current quota snapshot.

When the per-key rate limit is exceeded, API endpoints return:

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

## Administrator Endpoints

Administrator routes require an authenticated admin browser session.

Set the persistent daily limit:

```json
POST /api/admin/users/{user_id}/daily-limit
{
  "daily_limit": 20,
  "reason": "扩大专项测试范围"
}
```

Add requests for today, or pass an optional `date` in `YYYY-MM-DD` form:

```json
POST /api/admin/users/{user_id}/quota
{
  "extra_requests": 5,
  "reason": "增加本轮测试次数"
}
```

Both operations append an immutable quota-adjustment record containing the operator, target user, date, reason, and count change.

Feedback triage:

```text
GET  /api/admin/feedback?status=open&review_lane=kb_review
POST /api/admin/feedback/{feedback_id}/status
```

The status update body accepts `open`, `in_progress`, `kb_review`, `resolved`, `dismissed`, or `closed`, plus an optional `resolution_note`.

## POST /api/uploads

用户上传资料。上传资料默认进入上传用户的私有库。

### Response

```json
{
  "upload_id": "upload-001",
  "status": "uploaded",
  "visibility": "private"
}
```

## POST /api/uploads/{upload_id}/submit-review

用户申请将上传资料提交管理员审核。审核通过后才能进入受控服务可检索范围；知识库本体不对外公开。

### Response

```json
{
  "review_id": "review-001",
  "status": "review_pending"
}
```

## POST /api/admin/reviews/{review_id}/decision

管理员审核用户上传资料。

### Request

```json
{
  "decision": "approved_for_service",
  "comment": "来源和版本已核验"
}
```

### Response

```json
{
  "ok": true,
  "status": "approved_for_service"
}
```

## Candidate Staging

联网补充、官方阅读器 OCR、网页解析或用户问题触发的新材料，默认进入候选暂存区。

建议由知识库服务提供：

```text
POST /knowledge/candidates
GET /knowledge/candidates
POST /knowledge/candidates/{candidate_id}/decision
```

候选数据只有在管理员审核为 `approved_for_kb` 后，才能进入后台正式知识库、全文索引、向量索引或知识图谱。上述资产仍只供后台服务使用，不作为公开接口暴露。

## Internal Knowledge Service Contract

后端调用知识库服务时，建议先约定统一接口。These routes are internal only and should be bound to localhost, private network, service mesh, or a firewall-protected backend segment:

```text
POST /knowledge/search
GET /knowledge/standards
POST /knowledge/candidates
```

返回候选证据，不直接返回最终自然语言答案。

本仓库提供 `mining_qa.mock_kb:app` 用于模拟上述接口，便于真实知识库接入前进行 API 回归测试。
