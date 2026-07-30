from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from . import __version__
from .knowledge_store import DEFAULT_DB_PATH, KnowledgeStore
from .v4_candidate_store import V4CandidateStore
from .v4_retrieval_store import V4KnowledgeStore
from .v4_retrieval_store_v2 import (
    DEFAULT_RUNTIME_MANIFEST,
    ResilientV4KnowledgeStore,
)


def db_path_from_env() -> Path:
    return Path(os.getenv("KNOWLEDGE_DB_PATH", str(DEFAULT_DB_PATH)))


def runtime_version_from_env() -> str:
    return os.getenv("KNOWLEDGE_RUNTIME_VERSION", "v3").strip().lower() or "v3"


def build_store(
    runtime_version: str | None = None,
    *,
    query_embedder: Any | None = None,
) -> KnowledgeStore | V4KnowledgeStore:
    version = (runtime_version or runtime_version_from_env()).strip().lower()
    if version == "v3":
        return KnowledgeStore(db_path_from_env())
    if version != "v4":
        raise RuntimeError(f"unsupported KNOWLEDGE_RUNTIME_VERSION: {version}")
    candidate_path = Path(
        os.getenv(
            "V4_CANDIDATE_DB_PATH",
            str(
                Path(__file__).resolve().parents[2]
                / "data"
                / "knowledge_base_v4"
                / "runtime_private"
                / "candidates.sqlite"
            ),
        )
    )
    candidate_store = V4CandidateStore(candidate_path)
    manifest_path = Path(
        os.getenv("V4_RUNTIME_MANIFEST", str(DEFAULT_RUNTIME_MANIFEST))
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runtime_id = str(manifest.get("runtime_id") or "")
    if runtime_id in {
        "v4-hybrid-fixed20-resilient-v2",
        "v4-hybrid-fixed20-p1fix-v3",
        "v4-hybrid-fixed20-p1fix-v4",
    }:
        store_type = ResilientV4KnowledgeStore
    elif runtime_id == "v4-hybrid-fixed20-v1":
        store_type = V4KnowledgeStore
    else:
        raise RuntimeError(f"unsupported v4 runtime manifest: {runtime_id}")
    return store_type(
        manifest_path,
        query_embedder=query_embedder,
        legacy_admin_store=candidate_store,
    )


store = build_store()


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        yield
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


app = FastAPI(
    title="geowiki Private Knowledge Service",
    version=__version__,
    lifespan=lifespan,
)


@app.get("/knowledge/health")
async def health() -> dict[str, Any]:
    return store.health()


@app.post("/knowledge/search")
async def search(payload: dict[str, Any]) -> dict[str, Any]:
    return await run_in_threadpool(store.search, payload)


@app.post("/knowledge/research/corpus")
async def research_corpus(payload: dict[str, Any]) -> dict[str, Any]:
    return await run_in_threadpool(store.research_corpus, payload)


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
