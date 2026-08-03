# External Agent API Test Guide

This guide tests the public GeoWiki API through Nginx. Replace the placeholders locally; never commit a production host, IP address, or API key.

```bash
export GEOWIKI_BASE_URL="https://<your-geowiki-host>"
export GEOWIKI_API_KEY="<your-api-key>"
```

Use only `/health` and `/api/*`. The private `/knowledge/*` routes are deliberately unavailable through the public origin.

## Health

```bash
curl -sS "${GEOWIKI_BASE_URL}/health"
```

## Ask

```bash
curl -sS -X POST "${GEOWIKI_BASE_URL}/api/ask" \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: ${GEOWIKI_API_KEY}" \
  -d '{"question":"金矿勘查Ⅰ类型的推荐工程间距是多少？"}'
```

The public browser and API use one question endpoint. A direct question returns an answer response. If a browser-session request is automatically routed to comprehensive research, the response contains `task_id`; poll `/api/research/tasks/{task_id}` and then read `/result`.

If the response has `status="clarification_required"`, submit the returned identifiers without reconstructing the question locally:

```bash
curl -sS -X POST "${GEOWIKI_BASE_URL}/api/ask" \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: ${GEOWIKI_API_KEY}" \
  -d '{"clarification_id":"clarify_xxx","option_id":"option_1"}'
```

## Catalog

```bash
curl -sS "${GEOWIKI_BASE_URL}/api/standards?standard_no=DZ/T%200205-2020&page_size=5" \
  -H "X-API-Key: ${GEOWIKI_API_KEY}"
```

## Feedback and usage

```bash
curl -sS -X POST "${GEOWIKI_BASE_URL}/api/feedback" \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: ${GEOWIKI_API_KEY}" \
  -d '{"session_id":"SESSION_ID_FROM_ASK","rating":"satisfied","reason":"other","comment":"External API test succeeded"}'

curl -sS "${GEOWIKI_BASE_URL}/api/usage" \
  -H "X-API-Key: ${GEOWIKI_API_KEY}"
```

Do not retry aggressively, do not call `/knowledge/*`, and do not record returned credentials or private evidence in shared logs.
