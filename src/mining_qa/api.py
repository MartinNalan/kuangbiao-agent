from typing import Annotated
from time import perf_counter

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .agent import MiningQAAgent
from .auth import require_api_key
from .config import PROJECT_ROOT, get_settings
from .knowledge_client import KnowledgeClient
from .rate_limit import RateLimiter
from .schemas import AskRequest, AskResponse, StandardsResponse
from .usage_log import UsageLogger
from .usage_stats import UsageStats


app = FastAPI(title="Mining Knowledge QA", version="0.1.0")
app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "web" / "static"), name="static")
usage_logger = UsageLogger()
usage_stats = UsageStats()
rate_limiter = RateLimiter()


def authenticated_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> str:
    return require_api_key(get_settings(), x_api_key=x_api_key, authorization=authorization)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(PROJECT_ROOT / "web" / "index.html")


@app.get("/health")
async def health() -> dict[str, object]:
    settings = get_settings()
    return {
        "ok": True,
        "model": settings.openai_model,
        "knowledge_base_enabled": bool(settings.knowledge_base_url),
        "api_auth_enabled": bool(settings.allowed_api_keys),
        "rate_limit_enabled": settings.rate_limit_enabled,
        "rate_limit_per_minute": settings.rate_limit_per_minute,
        "rate_limit_backend": rate_limiter.last_backend,
    }


async def enforce_rate_limit(api_key: str) -> None:
    result = await rate_limiter.check(api_key, get_settings())
    rate_limiter.raise_if_limited(result)


@app.post("/api/ask", response_model=AskResponse)
async def ask(
    request: AskRequest,
    http_request: Request,
    api_key: Annotated[str, Depends(authenticated_api_key)],
) -> AskResponse:
    started = perf_counter()
    await enforce_rate_limit(api_key)
    agent = MiningQAAgent(get_settings())
    response = await agent.ask(request)
    usage_logger.write(
        {
            "api_key": api_key,
            "endpoint": "/api/ask",
            "method": "POST",
            "client_host": http_request.client.host if http_request.client else None,
            "question_chars": len(request.question),
            "status": response.status,
            "confidence": response.confidence,
            "has_clause_level_evidence": response.limitations.has_clause_level_evidence,
            "source_count": len(response.sources),
            "web_hits": response.retrieval.web_hits,
            "knowledge_gap_task_id": response.knowledge_gap_task.task_id if response.knowledge_gap_task else None,
            "duration_ms": round((perf_counter() - started) * 1000, 2),
        }
    )
    return response


@app.get("/api/standards", response_model=StandardsResponse)
async def standards(
    http_request: Request,
    api_key: Annotated[str, Depends(authenticated_api_key)],
    q: Annotated[str | None, Query()] = None,
    standard_no: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    text_access: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> StandardsResponse:
    started = perf_counter()
    await enforce_rate_limit(api_key)
    client = KnowledgeClient(get_settings())
    response = await client.standards(
        {
            "q": q,
            "standard_no": standard_no,
            "status": status,
            "text_access": text_access,
            "page": page,
            "page_size": page_size,
        }
    )
    usage_logger.write(
        {
            "api_key": api_key,
            "endpoint": "/api/standards",
            "method": "GET",
            "client_host": http_request.client.host if http_request.client else None,
            "query": q,
            "standard_no": standard_no,
            "result_count": len(response.items),
            "duration_ms": round((perf_counter() - started) * 1000, 2),
        }
    )
    return response


@app.get("/api/usage")
async def usage(api_key: Annotated[str, Depends(authenticated_api_key)]) -> dict[str, object]:
    await enforce_rate_limit(api_key)
    settings = get_settings()
    return {
        "scope": "current_api_key",
        "rate_limit": {
            "enabled": settings.rate_limit_enabled,
            "limit_per_minute": settings.rate_limit_per_minute,
            "backend": rate_limiter.last_backend,
        },
        "usage": usage_stats.summarize(api_key),
    }
