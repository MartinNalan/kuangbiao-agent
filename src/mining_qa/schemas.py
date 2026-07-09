from typing import Any, Literal

from pydantic import BaseModel, Field


SourceType = Literal[
    "local_kb",
    "official_metadata",
    "official_fulltext",
    "official_visual",
    "third_party_candidate",
    "unavailable",
]

TextAccess = Literal[
    "metadata_only",
    "html_text",
    "pdf_text",
    "image_ocr_required",
    "ocr_text",
    "unavailable",
]


class AskFilters(BaseModel):
    domain: str | None = None
    document_types: list[str] = Field(default_factory=list)


class AskRequest(BaseModel):
    question: str
    session_id: str | None = None
    filters: AskFilters = Field(default_factory=AskFilters)


class KnowledgeGapTask(BaseModel):
    task_id: str
    status: Literal["queued", "searching", "ocr_pending", "ocr_running", "review_pending", "approved_for_kb", "rejected", "done"] = "queued"
    type: Literal["knowledge_gap"] = "knowledge_gap"
    message: str


class Source(BaseModel):
    title: str
    standard_no: str | None = None
    chapter: str | None = None
    page: int | None = None
    quote: str | None = None
    score: float | None = None
    source_type: SourceType = "unavailable"
    text_access: TextAccess = "unavailable"
    url: str | None = None
    validation_status: str | None = None


class RetrievalStats(BaseModel):
    full_text_hits: int = 0
    vector_hits: int = 0
    graph_hits: int = 0
    web_hits: int = 0


class Limitations(BaseModel):
    has_clause_level_evidence: bool = False
    notes: list[str] = Field(default_factory=list)


class AskResponse(BaseModel):
    answer: str
    session_id: str
    status: Literal["answered", "insufficient_evidence", "out_of_scope", "queued_for_enrichment"] = "answered"
    sources: list[Source] = Field(default_factory=list)
    retrieval: RetrievalStats = Field(default_factory=RetrievalStats)
    limitations: Limitations = Field(default_factory=Limitations)
    knowledge_gap_task: KnowledgeGapTask | None = None
    confidence: Literal["low", "medium", "high"] = "low"


class StandardItem(BaseModel):
    document_id: str
    title: str
    standard_no: str | None = None
    document_type: str | None = None
    status: str | None = None
    source_type: SourceType = "unavailable"
    text_access: TextAccess = "unavailable"
    validation_status: str | None = None
    can_answer: bool = False
    publish_date: str | None = None
    implementation_date: str | None = None
    ingestion_time: str | None = None


class Pagination(BaseModel):
    page: int = 1
    page_size: int = 20
    total: int = 0


class StandardsResponse(BaseModel):
    items: list[StandardItem] = Field(default_factory=list)
    pagination: Pagination = Field(default_factory=Pagination)


class KnowledgeSearchRequest(BaseModel):
    query: str
    filters: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class KnowledgeSearchResponse(BaseModel):
    results: list[dict[str, Any]] = Field(default_factory=list)
    retrieval: dict[str, int] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)
