from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, PrivateAttr


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
    _retrieval_question: str | None = PrivateAttr(default=None)

    @property
    def retrieval_question(self) -> str:
        return self._retrieval_question or self.question


class QuotaInfo(BaseModel):
    date: str
    daily_limit: int
    bonus: int
    effective_limit: int
    used: int
    reserved: int
    remaining: int
    consumed: bool = False
    consumed_units: int = 0


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
    source_platform: str | None = None
    source_role: str | None = None
    validation_status: str | None = None


class RetrievalStats(BaseModel):
    full_text_hits: int = 0
    vector_hits: int = 0
    graph_hits: int = 0
    web_hits: int = 0
    direct_evidence_hits: int = 0
    retrieval_rounds: int = 0
    planner_used: bool = False
    reranker_used: bool = False
    ann_used: bool = False
    query_count: int = 0
    multi_query_used: bool = False
    multi_query_count: int = 0
    mmr_used: bool = False
    planner_ms: float = 0.0
    knowledge_ms: float = 0.0
    reranker_ms: float = 0.0
    mmr_ms: float = 0.0
    synthesis_ms: float = 0.0
    total_ms: float = 0.0


class Limitations(BaseModel):
    has_clause_level_evidence: bool = False
    notes: list[str] = Field(default_factory=list)


class AskResponse(BaseModel):
    answer: str
    session_id: str
    request_id: str | None = None
    status: Literal["answered", "insufficient_evidence", "out_of_scope", "queued_for_enrichment"] = "answered"
    sources: list[Source] = Field(default_factory=list)
    retrieval: RetrievalStats = Field(default_factory=RetrievalStats)
    limitations: Limitations = Field(default_factory=Limitations)
    knowledge_gap_task: KnowledgeGapTask | None = None
    confidence: Literal["low", "medium", "high"] = "low"
    quota: QuotaInfo | None = None
    mode: Literal["basic", "deep"] = "basic"
    quota_cost: int = 1
    mode_recommendation: Literal["deep"] | None = None
    mode_recommendation_reason: str | None = None


ResearchTaskStatus = Literal[
    "queued",
    "planning",
    "retrieving",
    "analyzing",
    "completed",
    "partial",
    "insufficient_evidence",
    "failed",
    "cancelled",
]


class ResearchTaskCreateRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None
    filters: AskFilters = Field(default_factory=AskFilters)
    source_request_id: str | None = None


class ResearchProgress(BaseModel):
    stage: ResearchTaskStatus = "queued"
    percent: int = Field(default=0, ge=0, le=100)
    message: str = "任务已进入队列。"
    examined_documents: int = 0
    total_documents: int = 0
    evidence_documents: int = 0


class ResearchCoverage(BaseModel):
    examined_documents: int = 0
    total_documents: int = 0
    evidence_documents: int = 0
    candidate_truncated: bool = False
    knowledge_snapshot: str | None = None
    notes: list[str] = Field(default_factory=list)


class ResearchTaskResponse(BaseModel):
    task_id: str
    request_id: str
    question: str
    session_id: str
    status: ResearchTaskStatus
    mode: Literal["deep"] = "deep"
    quota_cost: int = 3
    reserved_quota_units: int = 3
    progress: ResearchProgress = Field(default_factory=ResearchProgress)
    result_available: bool = False
    quota: QuotaInfo | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None


class ResearchResult(BaseModel):
    task_id: str
    request_id: str
    question: str
    session_id: str
    answer: str
    status: Literal["completed", "partial", "insufficient_evidence"]
    mode: Literal["deep"] = "deep"
    quota_cost: int = 3
    reserved_quota_units: int = 3
    sources: list[Source] = Field(default_factory=list)
    limitations: Limitations = Field(default_factory=Limitations)
    coverage: ResearchCoverage = Field(default_factory=ResearchCoverage)
    confidence: Literal["low", "medium", "high"] = "low"
    quota: QuotaInfo | None = None


class FeedbackRequest(BaseModel):
    session_id: str
    request_id: str | None = None
    rating: Literal["satisfied", "unsatisfied"]
    question: str | None = None
    comment: str | None = None
    reason: Literal[
        "wrong_standard",
        "wrong_clause",
        "missing_evidence",
        "quote_too_long",
        "answer_too_vague",
        "format_issue",
        "other",
    ] | None = None


class FeedbackResponse(BaseModel):
    ok: bool = True
    message: str = "feedback recorded"
    feedback_id: str | None = None
    review_lane: Literal["no_action", "product", "kb_review", "manual_review"] | None = None
    status: Literal["open", "in_progress", "kb_review", "resolved", "dismissed", "closed"] | None = None


class FeedbackStatusUpdateRequest(BaseModel):
    status: Literal["open", "in_progress", "kb_review", "resolved", "dismissed", "closed"]
    resolution_note: str | None = Field(default=None, max_length=1000)


class LexiconCandidateRequest(BaseModel):
    target_lexicon_id: str | None = Field(default=None, max_length=120)
    user_expression: str = Field(min_length=2, max_length=120)
    canonical_term: str = Field(min_length=2, max_length=200)
    intent_label: str = Field(min_length=2, max_length=80)
    domain: str = Field(min_length=2, max_length=80)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    positive_expansions: list[str] = Field(default_factory=list, max_length=40)
    negative_terms: list[str] = Field(default_factory=list, max_length=40)
    evidence_required_patterns: list[str] = Field(default_factory=list, max_length=30)
    required_context_terms: list[str] = Field(default_factory=list, max_length=30)
    forbidden_context_terms: list[str] = Field(default_factory=list, max_length=30)
    positive_examples: list[str] = Field(default_factory=list, max_length=20)
    negative_examples: list[str] = Field(default_factory=list, max_length=20)
    match_type: Literal["phrase", "exact"] = "phrase"
    domain_gate_enabled: bool = False
    intent_trigger_enabled: bool = True
    priority: int = Field(default=50, ge=0, le=100)
    risk_level: Literal["low", "medium", "high"] = "medium"
    status: Literal["draft", "pending"] = "draft"
    source_type: Literal["manual", "user_feedback", "query_mining", "kb_schema"] = "manual"
    source_reference: str | None = Field(default=None, max_length=1000)
    review_note: str | None = Field(default=None, max_length=1000)


class LexiconReviewRequest(BaseModel):
    action: Literal["approve", "reject"]
    note: str = Field(min_length=2, max_length=1000)


class LexiconEntryStatusRequest(BaseModel):
    status: Literal["active", "disabled"]
    note: str = Field(min_length=2, max_length=1000)


class LexiconPreviewRequest(BaseModel):
    candidate_id: str | None = Field(default=None, max_length=120)
    query: str = Field(min_length=1, max_length=1000)
    candidate: LexiconCandidateRequest


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
    url: str | None = None
    source_platform: str | None = None


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
    retrieval: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)


class EmailCodeRequest(BaseModel):
    email: EmailStr
    invite_code: str = Field(min_length=6, max_length=64)


class RegisterRequest(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=8, max_length=128)
    invite_code: str = Field(min_length=6, max_length=64)
    email_code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class LoginRequest(BaseModel):
    account: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=8, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class InvitationCreateRequest(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    max_uses: int = Field(default=1, ge=1, le=100)
    expires_in_days: int | None = Field(default=30, ge=1, le=365)


class DailyLimitUpdateRequest(BaseModel):
    daily_limit: int = Field(ge=1, le=100_000)
    reason: str = Field(min_length=2, max_length=200)


class DailyQuotaAdjustmentRequest(BaseModel):
    extra_requests: int = Field(ge=1, le=100_000)
    reason: str = Field(min_length=2, max_length=200)
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class UserStatusRequest(BaseModel):
    status: Literal["active", "suspended"]
