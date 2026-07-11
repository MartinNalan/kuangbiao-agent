# External Agent API Test Guide

This document is for a fresh external agent or another LAN machine to test the public API boundary of the mining standards QA service.

Do not call `/knowledge/*`. Those routes are internal knowledge-base routes and are not part of the customer-facing API.

## Connection

Base URL:

```text
http://172.168.206.253:18080
```

Test API key:

```text
agent-client-test-20260710
```

Authentication header:

```text
X-API-Key: agent-client-test-20260710
```

Alternative bearer form:

```text
Authorization: Bearer agent-client-test-20260710
```

## Health Check

```bash
curl -sS http://172.168.206.253:18080/health
```

Expected: JSON with `"ok": true`.

## Ask A Question

```bash
curl -sS -X POST http://172.168.206.253:18080/api/ask \
  -H "Content-Type: application/json" \
  -H "X-API-Key: agent-client-test-20260710" \
  -d '{"question":"我是一个大型的金矿，我的储量报告评审应该去哪个机构？"}'
```

Expected behavior:

- The response should answer only with mineral-resource standards/policy scope.
- The answer should cite `自然资规〔2023〕6号`.
- The response should include a `session_id`.
- Save the `session_id` if you want to submit feedback.

## Query Standard Catalog

```bash
curl -sS "http://172.168.206.253:18080/api/standards?standard_no=DZ/T%200205-2020&page_size=5" \
  -H "X-API-Key: agent-client-test-20260710"
```

Expected: catalog items for `DZ/T 0205-2020`.

## Submit Feedback

Replace `SESSION_ID_FROM_ASK` with the `session_id` returned by `/api/ask`.

```bash
curl -sS -X POST http://172.168.206.253:18080/api/feedback \
  -H "Content-Type: application/json" \
  -H "X-API-Key: agent-client-test-20260710" \
  -d '{
    "session_id":"SESSION_ID_FROM_ASK",
    "rating":"satisfied",
    "reason":"other",
    "comment":"LAN external-agent test succeeded",
    "question":"我是一个大型的金矿，我的储量报告评审应该去哪个机构？"
  }'
```

Expected: feedback recorded message.

Allowed `rating` values:

- `satisfied`
- `unsatisfied`

Allowed `reason` values:

- `wrong_standard`
- `wrong_clause`
- `missing_evidence`
- `quote_too_long`
- `answer_too_vague`
- `format_issue`
- `other`

## Check Current API Key Usage

```bash
curl -sS http://172.168.206.253:18080/api/usage \
  -H "X-API-Key: agent-client-test-20260710"
```

Expected: usage counters for this test key.

## Python Example

```python
import requests

base_url = "http://172.168.206.253:18080"
headers = {"X-API-Key": "agent-client-test-20260710"}

response = requests.post(
    f"{base_url}/api/ask",
    headers=headers,
    json={"question": "大型金矿基本工程间距是多少？"},
    timeout=90,
)
response.raise_for_status()
data = response.json()
print(data["answer"])
print("session_id:", data["session_id"])
```

## Scope Rules For The External Agent

- Use only `/health` and `/api/*`.
- Do not call `/knowledge/*`.
- Do not ask general chat questions such as `1+1=几`; this service should reject out-of-scope questions.
- Preserve returned `session_id` for feedback.
- Do not retry aggressively. The current LAN test limit is 100 requests per minute per key.
