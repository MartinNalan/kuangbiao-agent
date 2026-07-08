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
      "chapter": "章节或条款号",
      "page": 12,
      "quote": "相关原文片段",
      "score": 0.92
    }
  ],
  "retrieval": {
    "full_text_hits": 5,
    "vector_hits": 8,
    "graph_hits": 2
  },
  "confidence": "medium"
}
```

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

## Knowledge Service Contract

后端调用知识库服务时，建议先约定统一接口：

```text
POST /knowledge/search
```

返回候选证据，不直接返回最终自然语言答案。
