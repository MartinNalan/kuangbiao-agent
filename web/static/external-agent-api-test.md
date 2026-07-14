# External Agent API Test

Base URL:

```text
http://172.168.206.253:18080
```

API key:

```text
<your-api-key>
```

Use only:

- `GET /health`
- `POST /api/ask`
- `GET /api/standards`
- `POST /api/feedback`
- `GET /api/usage`

Do not call `/knowledge/*`; it is an internal knowledge-base service.

## Health

```bash
curl -sS http://172.168.206.253:18080/health
```

## Ask

```bash
curl -sS -X POST http://172.168.206.253:18080/api/ask \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your-api-key>" \
  -d '{"question":"我是一个大型的金矿，我的储量报告评审应该去哪个机构？"}'
```

Save the returned `session_id`.

If the response has `status="clarification_required"`, submit the returned structured selection without rebuilding the question locally:

```bash
curl -sS -X POST http://172.168.206.253:18080/api/ask \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your-api-key>" \
  -d '{"clarification_id":"clarify_xxx","option_id":"option_1"}'
```

## Catalog

```bash
curl -sS "http://172.168.206.253:18080/api/standards?standard_no=DZ/T%200205-2020&page_size=5" \
  -H "X-API-Key: <your-api-key>"
```

## Feedback

Replace `SESSION_ID_FROM_ASK`.

```bash
curl -sS -X POST http://172.168.206.253:18080/api/feedback \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your-api-key>" \
  -d '{
    "session_id":"SESSION_ID_FROM_ASK",
    "rating":"satisfied",
    "reason":"other",
    "comment":"LAN external-agent test succeeded",
    "question":"我是一个大型的金矿，我的储量报告评审应该去哪个机构？"
  }'
```

`rating`: `satisfied` or `unsatisfied`.

`reason`: `wrong_standard`, `wrong_clause`, `missing_evidence`, `quote_too_long`, `answer_too_vague`, `format_issue`, or `other`.

## Usage

```bash
curl -sS http://172.168.206.253:18080/api/usage \
  -H "X-API-Key: <your-api-key>"
```

## Python

```python
import requests

base_url = "http://172.168.206.253:18080"
headers = {"X-API-Key": "<your-api-key>"}

response = requests.post(
    f"{base_url}/api/ask",
    headers=headers,
    json={"question": "大型金矿基本工程间距是多少？"},
    timeout=90,
)
response.raise_for_status()
print(response.json())
```
