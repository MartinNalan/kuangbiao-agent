# API Spec

## POST /api/ask

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
  "confidence": "medium"
}
```

`source_type` allowed values:

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
5. If OCR or page parsing is triggered, store the result as a candidate record first; do not add it to the public KB until admin approval.

## POST /api/feedback

### Request

```json
{
  "session_id": "session-id",
  "message_id": "answer-id",
  "rating": "useful",
  "comment": "引用准确"
}
```

### Response

```json
{
  "ok": true
}
```

## GET /api/standards

查询知识库中是否已有某个标准。

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

用户申请将上传资料提交管理员审核。审核通过后才能进入公共知识库。

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
  "decision": "approved_public",
  "comment": "来源和版本已核验"
}
```

### Response

```json
{
  "ok": true,
  "status": "approved_public"
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

候选数据只有在管理员审核为 `approved_for_kb` 后，才能进入正式知识库、全文索引、向量索引或知识图谱。

## Knowledge Service Contract

后端调用知识库服务时，建议先约定统一接口：

```text
POST /knowledge/search
GET /knowledge/standards
```

返回候选证据，不直接返回最终自然语言答案。
