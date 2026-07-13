from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(PROJECT_ROOT / ".env"), extra="ignore")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://api.deepseek.com", alias="OPENAI_BASE_URL")
    openai_model: str = Field(default="deepseek-v4-flash", alias="OPENAI_MODEL")
    knowledge_base_url: str = Field(default="", alias="KNOWLEDGE_BASE_URL")
    enable_sync_web_supplement: bool = Field(default=False, alias="ENABLE_SYNC_WEB_SUPPLEMENT")
    api_keys: str = Field(default="", alias="API_KEYS")
    api_key_registry_path: str = Field(default="", alias="API_KEY_REGISTRY_PATH")
    redis_url: str = Field(default="redis://127.0.0.1:6379/0", alias="REDIS_URL")
    rate_limit_enabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")
    rate_limit_per_minute: int = Field(default=30, alias="RATE_LIMIT_PER_MINUTE")
    request_timeout_seconds: float = Field(default=60.0, alias="REQUEST_TIMEOUT_SECONDS")
    dashscope_api_key: str = Field(default="", alias="DASHSCOPE_API_KEY")
    embedding_provider: str = Field(default="", alias="EMBEDDING_PROVIDER")
    embedding_api_key: str = Field(default="", alias="EMBEDDING_API_KEY")
    embedding_base_url: str = Field(default="", alias="EMBEDDING_BASE_URL")
    embedding_model: str = Field(default="", alias="EMBEDDING_MODEL")
    embedding_dimensions: int = Field(default=0, alias="EMBEDDING_DIMENSIONS")
    embedding_batch_size: int = Field(default=10, alias="EMBEDDING_BATCH_SIZE")
    query_planner_enabled: bool = Field(default=True, alias="QUERY_PLANNER_ENABLED")
    evidence_reranker_enabled: bool = Field(default=True, alias="EVIDENCE_RERANKER_ENABLED")
    question_resolution_enabled: bool = Field(default=True, alias="QUESTION_RESOLUTION_ENABLED")
    question_resolution_max_tokens: int = Field(
        default=500,
        ge=100,
        le=1000,
        alias="QUESTION_RESOLUTION_MAX_TOKENS",
    )
    question_resolution_min_confidence: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        alias="QUESTION_RESOLUTION_MIN_CONFIDENCE",
    )
    query_planner_max_tokens: int = Field(default=600, alias="QUERY_PLANNER_MAX_TOKENS")
    evidence_reranker_max_tokens: int = Field(default=800, alias="EVIDENCE_RERANKER_MAX_TOKENS")
    answer_max_tokens: int = Field(default=1000, alias="ANSWER_MAX_TOKENS")
    definition_answer_max_tokens: int = Field(default=1600, alias="DEFINITION_ANSWER_MAX_TOKENS")
    research_planner_max_tokens: int = Field(default=1000, alias="RESEARCH_PLANNER_MAX_TOKENS")
    research_analysis_max_tokens: int = Field(default=1800, alias="RESEARCH_ANALYSIS_MAX_TOKENS")
    research_analysis_batch_size: int = Field(
        default=4,
        ge=1,
        le=8,
        alias="RESEARCH_ANALYSIS_BATCH_SIZE",
    )
    research_answer_max_tokens: int = Field(default=2200, alias="RESEARCH_ANSWER_MAX_TOKENS")
    structured_temperature: float = Field(default=0.0, ge=0.0, le=0.3, alias="STRUCTURED_TEMPERATURE")
    answer_temperature: float = Field(default=0.2, ge=0.0, le=0.3, alias="ANSWER_TEMPERATURE")
    research_answer_temperature: float = Field(default=0.1, ge=0.0, le=0.2, alias="RESEARCH_ANSWER_TEMPERATURE")
    max_retrieval_rounds: int = Field(default=2, alias="MAX_RETRIEVAL_ROUNDS")
    controlled_multi_query_enabled: bool = Field(default=True, alias="CONTROLLED_MULTI_QUERY_ENABLED")
    controlled_multi_query_max: int = Field(default=2, ge=1, le=3, alias="CONTROLLED_MULTI_QUERY_MAX")
    mmr_enabled: bool = Field(default=True, alias="MMR_ENABLED")
    mmr_lambda: float = Field(default=0.7, ge=0.3, le=0.95, alias="MMR_LAMBDA")
    mmr_duplicate_trigger: float = Field(default=0.8, ge=0.5, le=1.0, alias="MMR_DUPLICATE_TRIGGER")
    ann_search_enabled: bool = Field(default=True, alias="ANN_SEARCH_ENABLED")
    ann_expansion_search: int = Field(default=64, ge=2, le=512, alias="ANN_EXPANSION_SEARCH")
    vector_fallback_scan_limit: int = Field(default=100, alias="VECTOR_FALLBACK_SCAN_LIMIT")
    research_max_documents: int = Field(default=60, ge=5, le=200, alias="RESEARCH_MAX_DOCUMENTS")
    research_document_concurrency: int = Field(default=4, ge=1, le=12, alias="RESEARCH_DOCUMENT_CONCURRENCY")
    research_global_concurrency: int = Field(default=1, ge=1, le=4, alias="RESEARCH_GLOBAL_CONCURRENCY")
    ann_index_path: str = Field(
        default=str(PROJECT_ROOT / "data" / "knowledge_base" / "indexes" / "dense.usearch"),
        alias="ANN_INDEX_PATH",
    )
    ann_manifest_path: str = Field(
        default=str(PROJECT_ROOT / "data" / "knowledge_base" / "indexes" / "dense_manifest.json"),
        alias="ANN_MANIFEST_PATH",
    )
    retrieval_trace_enabled: bool = Field(default=True, alias="RETRIEVAL_TRACE_ENABLED")
    retrieval_trace_path: str = Field(
        default=str(PROJECT_ROOT / "data" / "app" / "retrieval_traces.jsonl"),
        alias="RETRIEVAL_TRACE_PATH",
    )
    domain_lexicon_runtime_path: str = Field(
        default=str(PROJECT_ROOT / "data" / "app" / "domain_lexicon_runtime.json"),
        alias="DOMAIN_LEXICON_RUNTIME_PATH",
    )
    app_db_path: str = Field(
        default=str(PROJECT_ROOT / "data" / "app" / "application.sqlite"),
        alias="APP_DB_PATH",
    )
    auth_required: bool = Field(default=True, alias="AUTH_REQUIRED")
    registration_enabled: bool = Field(default=True, alias="REGISTRATION_ENABLED")
    session_cookie_name: str = Field(default="kb_session", alias="SESSION_COOKIE_NAME")
    session_cookie_secure: bool = Field(default=False, alias="SESSION_COOKIE_SECURE")
    session_ttl_hours: int = Field(default=168, alias="SESSION_TTL_HOURS")
    daily_quota_default: int = Field(default=10, alias="DAILY_QUOTA_DEFAULT")
    quota_timezone: str = Field(default="Asia/Shanghai", alias="QUOTA_TIMEZONE")
    email_verification_enabled: bool = Field(default=True, alias="EMAIL_VERIFICATION_ENABLED")
    email_verification_secret: str = Field(default="", alias="EMAIL_VERIFICATION_SECRET")
    email_code_ttl_minutes: int = Field(default=10, alias="EMAIL_CODE_TTL_MINUTES")
    email_code_cooldown_seconds: int = Field(default=60, alias="EMAIL_CODE_COOLDOWN_SECONDS")
    email_code_daily_limit: int = Field(default=5, alias="EMAIL_CODE_DAILY_LIMIT")
    email_debug: bool = Field(default=False, alias="EMAIL_DEBUG")
    email_provider: str = Field(default="agentmail", alias="EMAIL_PROVIDER")
    agentmail_api_key: str = Field(default="", alias="AGENTMAIL_API_KEY")
    agentmail_inbox_id: str = Field(default="", alias="AGENTMAIL_INBOX_ID")
    agentmail_base_url: str = Field(default="https://api.agentmail.to/v0", alias="AGENTMAIL_BASE_URL")
    agentmail_proxy_url: str = Field(default="", alias="AGENTMAIL_PROXY_URL")
    public_base_url: str = Field(default="", alias="PUBLIC_BASE_URL")

    @property
    def chat_completions_url(self) -> str:
        return self.openai_base_url.rstrip("/") + "/chat/completions"

    @property
    def allowed_api_keys(self) -> set[str]:
        return {key.strip() for key in self.api_keys.split(",") if key.strip()}

    @property
    def email_verification_ready(self) -> bool:
        if not self.email_verification_enabled or not self.email_verification_secret:
            return False
        if self.email_debug:
            return True
        return bool(
            self.email_provider == "agentmail"
            and self.agentmail_api_key
            and self.agentmail_inbox_id
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
