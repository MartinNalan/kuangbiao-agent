from typing import Annotated

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .agent import MiningQAAgent
from .config import PROJECT_ROOT, get_settings
from .knowledge_client import KnowledgeClient
from .schemas import AskRequest, AskResponse, StandardsResponse


app = FastAPI(title="Mining Knowledge QA", version="0.1.0")
app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "web" / "static"), name="static")


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
    }


@app.post("/api/ask", response_model=AskResponse)
async def ask(request: AskRequest) -> AskResponse:
    agent = MiningQAAgent(get_settings())
    return await agent.ask(request)


@app.get("/api/standards", response_model=StandardsResponse)
async def standards(
    q: Annotated[str | None, Query()] = None,
    standard_no: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    text_access: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> StandardsResponse:
    client = KnowledgeClient(get_settings())
    return await client.standards(
        {
            "q": q,
            "standard_no": standard_no,
            "status": status,
            "text_access": text_access,
            "page": page,
            "page_size": page_size,
        }
    )
