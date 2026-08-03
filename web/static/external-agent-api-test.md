# External Agent API Test

Replace the placeholders locally. Do not save a production host, IP address, or API key in this file.

```bash
export GEOWIKI_BASE_URL="https://<your-geowiki-host>"
export GEOWIKI_API_KEY="<your-api-key>"
```

Public routes:

- `GET /health`
- `POST /api/ask`
- `POST /api/research/tasks`
- `GET /api/research/tasks/{task_id}`
- `GET /api/research/tasks/{task_id}/result`
- `POST /api/research/tasks/{task_id}/cancel`
- `GET /api/standards`
- `POST /api/feedback`
- `GET /api/usage`

The browser has one question entry and the server selects direct verification or comprehensive research. Do not call `/knowledge/*`; it is a private backend service.

```bash
curl -sS "${GEOWIKI_BASE_URL}/health"

curl -sS -X POST "${GEOWIKI_BASE_URL}/api/ask" \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: ${GEOWIKI_API_KEY}" \
  -d '{"question":"金矿勘查Ⅰ类型的推荐工程间距是多少？"}'

curl -sS "${GEOWIKI_BASE_URL}/api/standards?standard_no=DZ/T%200205-2020&page_size=5" \
  -H "X-API-Key: ${GEOWIKI_API_KEY}"
```
