from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from . import __version__
from .knowledge_store import DEFAULT_DB_PATH, KnowledgeStore


def db_path_from_env() -> Path:
    return Path(os.getenv("KNOWLEDGE_DB_PATH", str(DEFAULT_DB_PATH)))


store = KnowledgeStore(db_path_from_env())
app = FastAPI(title="geowiki Private Knowledge Service", version=__version__)


@app.get("/knowledge/health")
async def health() -> dict[str, Any]:
    return store.health()


@app.post("/knowledge/search")
async def search(payload: dict[str, Any]) -> dict[str, Any]:
    return await run_in_threadpool(store.search, payload)


@app.get("/knowledge/standards")
async def standards(
    q: str | None = None,
    standard_no: str | None = None,
    status: str | None = None,
    text_access: str | None = None,
    visibility: str | None = None,
    document_type: str | None = None,
    validation_status: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    return store.standards(
        {
            "q": q,
            "standard_no": standard_no,
            "status": status,
            "text_access": text_access,
            "visibility": visibility,
            "document_type": document_type,
            "validation_status": validation_status,
            "page": page,
            "page_size": page_size,
        }
    )


@app.get("/knowledge/documents/{document_id}")
async def document(document_id: str) -> dict[str, Any]:
    item = store.document(document_id)
    if not item:
        raise HTTPException(status_code=404, detail="document not found")
    return item


@app.get("/knowledge/chunks/{chunk_id}")
async def chunk(chunk_id: str, include_full_text: bool = False) -> dict[str, Any]:
    item = store.chunk(chunk_id, include_full_text=include_full_text)
    if not item:
        raise HTTPException(status_code=404, detail="chunk not found")
    return item


@app.post("/knowledge/candidates")
async def create_candidate(payload: dict[str, Any]) -> dict[str, Any]:
    return store.create_candidate(payload)


@app.get("/knowledge/candidates")
async def candidates(page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=100)) -> dict[str, Any]:
    return store.candidates(page=page, page_size=page_size)
