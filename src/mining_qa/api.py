from __future__ import annotations

import logging
import secrets
from pathlib import Path
from time import perf_counter
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .account_store import (
    ActiveResearchTaskError,
    AccountStoreError,
    DailyQuotaExceededError,
    DuplicateAccountError,
    EmailCodeCooldownError,
    EmailCodeDailyLimitError,
    InvalidCredentialsError,
    InvalidEmailCodeError,
    InvalidInviteError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from .agent import MiningQAAgent
from .api_keys import ApiKeyRegistry
from .auth import (
    Principal,
    get_account_store,
    require_admin_principal,
    require_session_principal,
    resolve_principal,
)
from .config import PROJECT_ROOT, get_settings
from .email_sender import EmailDeliveryError, send_verification_email
from .domain_gate import DomainGate
from .feedback_log import FeedbackLogger
from .knowledge_client import KnowledgeClient
from .lexicon_governance import (
    LexiconGovernanceError,
    LexiconRecordNotFoundError,
    LexiconReviewError,
    get_lexicon_governance_store,
)
from .query_understanding import contextualize_follow_up
from .question_resolution import (
    QuestionResolution,
    QuestionResolver,
    clarification_answer,
)
from .rate_limit import RateLimiter
from .schemas import (
    ApiKeyCreateRequest,
    AskRequest,
    AskResponse,
    DailyLimitUpdateRequest,
    DailyQuotaAdjustmentRequest,
    EmailCodeRequest,
    FeedbackRequest,
    FeedbackResponse,
    FeedbackStatusUpdateRequest,
    InvitationCreateRequest,
    LexiconCandidateRequest,
    LexiconEntryStatusRequest,
    LexiconPreviewRequest,
    LexiconReviewRequest,
    LoginRequest,
    PasswordChangeRequest,
    QuotaInfo,
    ResearchResult,
    ResearchTaskCreateRequest,
    ResearchTaskResponse,
    RegisterRequest,
    StandardsResponse,
    UserStatusRequest,
)
from .research import research_runner, research_task_response
from .usage_log import UsageLogger
from .usage_stats import UsageStats
from . import __version__


logger = logging.getLogger(__name__)

OPENAPI_TAGS = [
    {"name": "system", "description": "Service health and runtime metadata."},
    {"name": "auth", "description": "Invite-only registration and browser session authentication."},
    {"name": "account", "description": "Current account, API keys, daily quota, and conversation history."},
    {"name": "qa", "description": "Controlled public QA API for mineral-resource standards and policies."},
    {"name": "research", "description": "Persistent deep-research tasks for cross-document review and comparison."},
    {"name": "catalog", "description": "Knowledge-base catalog lookup through the public API boundary."},
    {"name": "feedback", "description": "Answer-quality feedback for retrieval and KB improvement."},
    {"name": "usage", "description": "Account usage, daily quota, and rate-limit status."},
    {"name": "admin", "description": "Invite, account, daily-quota, and answer-feedback administration."},
]

app = FastAPI(
    title="geowiki API",
    version=__version__,
    description=(
        "一款专注地质领域的百科全搜。公网客户端只应使用 "
        "`/api/*` 与 `/health`；私有知识库服务不得暴露到公网。"
    ),
    openapi_tags=OPENAPI_TAGS,
)
app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "web" / "static"), name="static")
usage_logger = UsageLogger()
feedback_logger = FeedbackLogger()
usage_stats = UsageStats()
rate_limiter = RateLimiter()
domain_gate = DomainGate()


def authenticated_principal(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> Principal:
    return resolve_principal(request, x_api_key=x_api_key, authorization=authorization)


def browser_principal(request: Request) -> Principal:
    return require_session_principal(request)


def admin_principal(request: Request) -> Principal:
    return require_admin_principal(request)


async def enforce_rate_limit(rate_limit_key: str) -> None:
    result = await rate_limiter.check(rate_limit_key, get_settings())
    rate_limiter.raise_if_limited(result)


async def resolve_question(
    settings,
    question: str,
    *,
    mode: str,
) -> QuestionResolution:
    resolver = QuestionResolver(settings)
    try:
        return await resolver.resolve(question, mode=mode)
    finally:
        await resolver.aclose()


def clarification_response(
    resolution: QuestionResolution,
    *,
    session_id: str,
    request_id: str,
    mode: str,
    quota: dict[str, object] | None = None,
) -> AskResponse:
    clarification = resolution.clarification
    if clarification is None:
        raise ValueError("clarification response requires clarification data")
    quota_info = None
    if quota is not None:
        snapshot = dict(quota)
        snapshot["consumed"] = False
        snapshot["consumed_units"] = 0
        quota_info = QuotaInfo(**snapshot)
    return AskResponse(
        answer=clarification_answer(clarification),
        session_id=session_id,
        request_id=request_id,
        status="clarification_required",
        confidence="medium",
        quota=quota_info,
        mode="deep" if mode == "deep" else "basic",
        quota_cost=0,
        clarification=clarification,
        limitations={
            "has_clause_level_evidence": False,
            "notes": ["问题尚未确认，本次未执行知识库检索，也未使用问答次数。"],
        },
    )


@app.on_event("startup")
async def recover_research_queue() -> None:
    settings = get_settings()
    try:
        get_lexicon_governance_store(settings).publish_runtime()
    except OSError:
        logger.exception("Unable to publish governed domain lexicon during startup")
    for task_id in get_account_store(settings).recover_research_tasks():
        research_runner.schedule(task_id)


def set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def account_error(error: Exception) -> HTTPException:
    if isinstance(error, DuplicateAccountError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "ACCOUNT_EXISTS", "message": "该邮箱已注册。"},
        )
    if isinstance(error, InvalidInviteError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_INVITE", "message": "邀请码无效、已用完或已过期。"},
        )
    if isinstance(error, InvalidCredentialsError):
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_CREDENTIALS", "message": "账号或密码不正确。"},
        )
    if isinstance(error, InvalidEmailCodeError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_EMAIL_CODE", "message": "邮箱验证码错误或已过期。"},
        )
    if isinstance(error, EmailCodeCooldownError):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "EMAIL_CODE_COOLDOWN",
                "message": f"验证码发送过于频繁，请在 {error.retry_after_seconds} 秒后重试。",
                "retry_after_seconds": error.retry_after_seconds,
            },
            headers={"Retry-After": str(error.retry_after_seconds)},
        )
    if isinstance(error, EmailCodeDailyLimitError):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "EMAIL_CODE_DAILY_LIMIT", "message": "该邮箱今天发送验证码的次数已达上限。"},
        )
    if isinstance(error, DailyQuotaExceededError):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "DAILY_QUOTA_EXCEEDED",
                "message": "今日问答次数已用完，请明日再试或联系管理员调整。",
                "quota": error.quota,
            },
        )
    if isinstance(error, ActiveResearchTaskError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "ACTIVE_RESEARCH_TASK_EXISTS",
                "message": "当前已有一个深度研究任务在排队或运行，请等待其完成后再创建新任务。",
                "task_id": error.task_id,
            },
        )
    if isinstance(error, ResourceNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "请求的记录不存在。"},
        )
    if isinstance(error, PermissionDeniedError):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "无权访问该记录。"},
        )
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": "ACCOUNT_ERROR", "message": "账号操作失败。"},
    )


def lexicon_error(error: Exception) -> HTTPException:
    if isinstance(error, LexiconRecordNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "LEXICON_NOT_FOUND", "message": "词典记录不存在。"},
        )
    if isinstance(error, LexiconReviewError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "LEXICON_REVIEW_ERROR", "message": str(error)},
        )
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": "LEXICON_ERROR", "message": "领域词典操作失败。"},
    )


def spa_index() -> FileResponse:
    return FileResponse(PROJECT_ROOT / "web" / "index.html")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return spa_index()


@app.get("/login", include_in_schema=False)
@app.get("/register", include_in_schema=False)
@app.get("/developer", include_in_schema=False)
@app.get("/usage", include_in_schema=False)
@app.get("/standards", include_in_schema=False)
@app.get("/admin", include_in_schema=False)
@app.get("/admin/lexicon", include_in_schema=False)
async def spa_page() -> FileResponse:
    return spa_index()


@app.get(
    "/health",
    tags=["system"],
    summary="Check service health",
    description="Returns runtime configuration flags without exposing secrets or knowledge-base content.",
)
async def health() -> dict[str, object]:
    settings = get_settings()
    store = get_account_store(settings)
    lexicon_summary = get_lexicon_governance_store(settings).summary()
    registry_path = Path(settings.api_key_registry_path) if settings.api_key_registry_path else None
    return {
        "ok": True,
        "version": app.version,
        "model": settings.openai_model,
        "knowledge_base_enabled": bool(settings.knowledge_base_url),
        "auth_required": settings.auth_required,
        "registration_enabled": settings.registration_enabled,
        "registered_users": store.user_count(),
        "legacy_api_auth_enabled": bool(settings.allowed_api_keys),
        "api_key_registry_enabled": ApiKeyRegistry(registry_path).exists(),
        "rate_limit_enabled": settings.rate_limit_enabled,
        "rate_limit_per_minute": settings.rate_limit_per_minute,
        "rate_limit_backend": rate_limiter.last_backend,
        "quota_mode": "daily_account_quota_units",
        "daily_quota_default": settings.daily_quota_default,
        "quota_timezone": settings.quota_timezone,
        "email_verification_ready": settings.email_verification_ready,
        "qa_modes": {"basic": 1, "deep": 3},
        "research_max_documents": settings.research_max_documents,
        "research_global_concurrency": settings.research_global_concurrency,
        "domain_lexicon": lexicon_summary,
    }


@app.post("/api/auth/email-code", tags=["auth"], summary="Send a registration email code")
async def send_email_code(payload: EmailCodeRequest, request: Request) -> dict[str, object]:
    settings = get_settings()
    if not settings.registration_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "REGISTRATION_DISABLED", "message": "当前未开放注册。"},
        )
    if not settings.email_verification_ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "EMAIL_NOT_CONFIGURED", "message": "邮箱验证服务尚未配置完成。"},
        )

    client_host = request.client.host if request.client else "unknown"
    email = str(payload.email).casefold()
    await enforce_rate_limit(f"email-code:{client_host}:{email}")
    store = get_account_store(settings)
    code = f"{secrets.randbelow(1_000_000):06d}"
    verification_id: str | None = None
    try:
        store.validate_invitation(payload.invite_code)
        verification_id = store.create_email_verification(
            email,
            code,
            settings.email_verification_secret,
            settings.email_code_ttl_minutes,
            settings.email_code_cooldown_seconds,
            settings.email_code_daily_limit,
            client_host,
        )
        await send_verification_email(settings, email, code)
    except EmailDeliveryError as error:
        if verification_id:
            store.cancel_email_verification(verification_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "EMAIL_DELIVERY_FAILED", "message": "验证码邮件发送失败，请稍后重试。"},
        ) from error
    except AccountStoreError as error:
        raise account_error(error) from error

    result: dict[str, object] = {
        "ok": True,
        "message": "验证码已发送，请检查邮箱。",
        "expires_in_seconds": settings.email_code_ttl_minutes * 60,
        "cooldown_seconds": settings.email_code_cooldown_seconds,
    }
    if settings.email_debug:
        result["debug_code"] = code
    return result


@app.post("/api/auth/register", tags=["auth"], summary="Register with an invitation code")
async def register(payload: RegisterRequest, request: Request, response: Response) -> dict[str, object]:
    settings = get_settings()
    if not settings.registration_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "REGISTRATION_DISABLED", "message": "当前未开放注册。"},
        )
    if not settings.email_verification_ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "EMAIL_NOT_CONFIGURED", "message": "邮箱验证服务尚未配置完成。"},
        )
    await enforce_rate_limit(f"auth:{request.client.host if request.client else 'unknown'}")
    store = get_account_store(settings)
    try:
        user = store.register_user(
            str(payload.email),
            payload.password,
            payload.display_name,
            payload.invite_code,
            payload.email_code,
            settings.daily_quota_default,
            settings.email_verification_secret,
        )
        _, token = store.create_session(user["user_id"], settings.session_ttl_hours)
    except AccountStoreError as error:
        raise account_error(error) from error
    set_session_cookie(response, token)
    return {"ok": True, "user": user}


@app.post("/api/auth/login", tags=["auth"], summary="Log in and create a browser session")
async def login(payload: LoginRequest, request: Request, response: Response) -> dict[str, object]:
    settings = get_settings()
    await enforce_rate_limit(f"auth:{request.client.host if request.client else 'unknown'}")
    store = get_account_store(settings)
    try:
        user = store.authenticate_user(payload.account, payload.password)
        _, token = store.create_session(user["user_id"], settings.session_ttl_hours)
    except AccountStoreError as error:
        raise account_error(error) from error
    set_session_cookie(response, token)
    return {"ok": True, "user": user}


@app.post("/api/auth/logout", tags=["auth"], summary="Revoke the current browser session")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        get_account_store(settings).revoke_session(token)
    response.delete_cookie(settings.session_cookie_name, path="/")
    return {"ok": True}


@app.get("/api/auth/me", tags=["auth"], summary="Get the current browser user")
async def me(request: Request) -> dict[str, object]:
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return {
            "authenticated": False,
            "user": None,
            "registration_enabled": settings.registration_enabled,
        }
    user = get_account_store(settings).authenticate_session(token)
    if not user:
        return {
            "authenticated": False,
            "user": None,
            "registration_enabled": settings.registration_enabled,
        }
    return {
        "authenticated": True,
        "user": get_account_store(settings).get_user(user["user_id"]),
        "registration_enabled": settings.registration_enabled,
    }


@app.post("/api/account/password", tags=["account"], summary="Change the current password")
async def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(browser_principal)],
) -> dict[str, bool]:
    settings = get_settings()
    store = get_account_store(settings)
    try:
        store.change_password(principal.user_id or "", payload.current_password, payload.new_password)
        _, token = store.create_session(principal.user_id or "", settings.session_ttl_hours)
    except AccountStoreError as error:
        raise account_error(error) from error
    set_session_cookie(response, token)
    return {"ok": True}


@app.get("/api/account/summary", tags=["account", "usage"], summary="Get daily quota and usage")
async def account_summary(principal: Annotated[Principal, Depends(browser_principal)]) -> dict[str, object]:
    settings = get_settings()
    summary = get_account_store(settings).account_summary(principal.user_id or "", settings.quota_timezone)
    return {
        **summary,
        "timezone": settings.quota_timezone,
    }


@app.get("/api/account/api-keys", tags=["account"], summary="List current user's API keys")
async def list_api_keys(principal: Annotated[Principal, Depends(browser_principal)]) -> dict[str, object]:
    return {"items": get_account_store().list_api_keys(principal.user_id or "")}


@app.post("/api/account/api-keys", tags=["account"], summary="Create an API key")
async def create_api_key(
    payload: ApiKeyCreateRequest,
    principal: Annotated[Principal, Depends(browser_principal)],
) -> dict[str, object]:
    store = get_account_store()
    record, plain_key = store.create_api_key(principal.user_id or "", payload.name)
    return {"item": record, "api_key": plain_key, "message": "API Key 只显示这一次，请立即妥善保存。"}


@app.delete("/api/account/api-keys/{api_key_id}", tags=["account"], summary="Revoke an API key")
async def revoke_api_key(
    api_key_id: str,
    principal: Annotated[Principal, Depends(browser_principal)],
) -> dict[str, bool]:
    try:
        get_account_store().revoke_api_key(principal.user_id or "", api_key_id)
    except AccountStoreError as error:
        raise account_error(error) from error
    return {"ok": True}


@app.get("/api/conversations", tags=["account"], summary="List browser conversation history")
async def conversations(principal: Annotated[Principal, Depends(browser_principal)]) -> dict[str, object]:
    return {"items": get_account_store().list_conversations(principal.user_id or "")}


@app.get("/api/conversations/{conversation_id}", tags=["account"], summary="Get one conversation")
async def conversation_detail(
    conversation_id: str,
    principal: Annotated[Principal, Depends(browser_principal)],
) -> dict[str, object]:
    try:
        return get_account_store().get_conversation(principal.user_id or "", conversation_id)
    except AccountStoreError as error:
        raise account_error(error) from error


@app.delete("/api/conversations/{conversation_id}", tags=["account"], summary="Delete one conversation")
async def delete_conversation(
    conversation_id: str,
    principal: Annotated[Principal, Depends(browser_principal)],
) -> dict[str, bool]:
    try:
        get_account_store().delete_conversation(principal.user_id or "", conversation_id)
    except AccountStoreError as error:
        raise account_error(error) from error
    return {"ok": True}


@app.post(
    "/api/ask",
    response_model=AskResponse,
    tags=["qa"],
    summary="Ask a domain-scoped question",
    description=(
        "Answers mineral-resource standards, technical specification, and related policy questions. "
        "Answered and in-scope evidence-gap requests consume daily quota; clarification, out-of-scope, "
        "and system-error results do not."
    ),
)
async def ask(
    payload: AskRequest,
    http_request: Request,
    principal: Annotated[Principal, Depends(authenticated_principal)],
) -> AskResponse:
    settings = get_settings()
    store = get_account_store(settings)
    started = perf_counter()
    request_id = "req_" + uuid4().hex
    channel = "web" if principal.auth_type == "session" else "api"
    conversation_id: str | None = None
    quota_reserved = False
    unreserved_quota: dict[str, object] | None = None
    resolution: QuestionResolution | None = None

    await enforce_rate_limit(principal.rate_limit_key)
    retrieval_question = payload.question
    if principal.user_id:
        try:
            conversation_id = store.ensure_conversation(principal.user_id, payload.session_id, payload.question)
            payload.session_id = conversation_id
            previous_question = store.latest_user_question(principal.user_id, conversation_id)
            retrieval_question = contextualize_follow_up(payload.question, previous_question)
        except AccountStoreError as error:
            raise account_error(error) from error
    payload._retrieval_question = retrieval_question

    domain_decision = domain_gate.check(payload.retrieval_question)
    if domain_decision.in_scope:
        resolution = await resolve_question(
            settings,
            payload.retrieval_question,
            mode="basic",
        )
        if resolution.requires_clarification:
            quota_snapshot = None
            if principal.user_id and principal.quota_managed:
                quota_snapshot = store.quota_snapshot(
                    principal.user_id,
                    settings.quota_timezone,
                )
            result = clarification_response(
                resolution,
                session_id=conversation_id or payload.session_id or str(uuid4()),
                request_id=request_id,
                mode="basic",
                quota=quota_snapshot,
            )
            if principal.user_id and conversation_id:
                try:
                    store.save_exchange(
                        principal.user_id,
                        conversation_id,
                        request_id,
                        payload.question,
                        result.answer,
                        {
                            "status": result.status,
                            "mode": "basic",
                            "clarification": result.clarification.model_dump(mode="json")
                            if result.clarification
                            else None,
                            "retrieval_question": payload.retrieval_question,
                            "question_resolution": {
                                "model_used": resolution.model_used,
                                "canonical_question": resolution.canonical_question,
                                "error": resolution.error,
                            },
                            "quota": result.quota.model_dump(mode="json") if result.quota else None,
                        },
                    )
                except AccountStoreError:
                    logger.exception("Unable to persist clarification for %s", conversation_id)
            usage_logger.write(
                {
                    "user_id": principal.user_id,
                    "credential_id": principal.credential_id,
                    "auth_type": principal.auth_type,
                    "endpoint": "/api/ask",
                    "method": "POST",
                    "client_host": http_request.client.host if http_request.client else None,
                    "request_id": request_id,
                    "question_chars": len(payload.question),
                    "status": result.status,
                    "quota_consumed": False,
                    "quota_consumed_units": 0,
                    "quota_remaining": result.quota.remaining if result.quota else None,
                    "question_resolution_used": resolution.model_used,
                    "question_resolution_error": resolution.error,
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                }
            )
            return result
        payload._retrieval_question = resolution.canonical_question

    if principal.user_id and principal.quota_managed:
        try:
            if domain_decision.in_scope:
                store.reserve_qa_quota(
                    principal.user_id,
                    request_id,
                    channel,
                    principal.credential_id if principal.auth_type == "api_key" else None,
                    conversation_id,
                    len(payload.question),
                    settings.quota_timezone,
                    quota_units=1,
                    request_mode="basic",
                )
                quota_reserved = True
            else:
                unreserved_quota = store.quota_snapshot(
                    principal.user_id,
                    settings.quota_timezone,
                )
                unreserved_quota["consumed"] = False
                unreserved_quota["consumed_units"] = 0
        except AccountStoreError as error:
            raise account_error(error) from error

    agent = MiningQAAgent(settings)
    try:
        result = await agent.ask(payload)
        result.request_id = request_id
        result.mode = "basic"
        result.quota_cost = 1
        if quota_reserved:
            settlement = store.settle_qa_quota(
                request_id,
                result.status,
                len(result.answer),
                settings.quota_timezone,
            )
            result.quota = QuotaInfo(**settlement)
        elif unreserved_quota is not None:
            result.quota = QuotaInfo(**unreserved_quota)
    except Exception:
        if quota_reserved:
            store.fail_qa_quota(request_id, settings.quota_timezone)
        raise
    finally:
        close_agent = getattr(agent, "aclose", None)
        if close_agent is not None:
            await close_agent()

    if principal.user_id and conversation_id:
        try:
            store.save_exchange(
                principal.user_id,
                conversation_id,
                request_id,
                payload.question,
                result.answer,
                {
                    "status": result.status,
                    "confidence": result.confidence,
                    "sources": [source.model_dump(mode="json") for source in result.sources],
                    "retrieval": result.retrieval.model_dump(mode="json"),
                    "limitations": result.limitations.model_dump(mode="json"),
                    "quota": result.quota.model_dump(mode="json") if result.quota else None,
                    "retrieval_question": payload.retrieval_question
                    if payload.retrieval_question != payload.question
                    else None,
                    "question_resolution": {
                        "model_used": resolution.model_used,
                        "canonical_question": resolution.canonical_question,
                        "error": resolution.error,
                    }
                    if resolution
                    else None,
                    "mode": "basic",
                    "mode_recommendation": result.mode_recommendation,
                },
            )
        except AccountStoreError:
            logger.exception("Unable to persist conversation %s", conversation_id)

    usage_logger.write(
        {
            "user_id": principal.user_id,
            "credential_id": principal.credential_id,
            "auth_type": principal.auth_type,
            "endpoint": "/api/ask",
            "method": "POST",
            "client_host": http_request.client.host if http_request.client else None,
            "request_id": request_id,
            "question_chars": len(payload.question),
            "status": result.status,
            "confidence": result.confidence,
            "has_clause_level_evidence": result.limitations.has_clause_level_evidence,
            "source_count": len(result.sources),
            "web_hits": result.retrieval.web_hits,
            "knowledge_gap_task_id": result.knowledge_gap_task.task_id if result.knowledge_gap_task else None,
            "quota_consumed": result.quota.consumed if result.quota else False,
            "quota_consumed_units": result.quota.consumed_units if result.quota else 0,
            "quota_remaining": result.quota.remaining if result.quota else None,
            "question_resolution_used": resolution.model_used if resolution else False,
            "question_resolution_error": resolution.error if resolution else None,
            "duration_ms": round((perf_counter() - started) * 1000, 2),
        }
    )
    return result


@app.post(
    "/api/research/tasks",
    response_model=ResearchTaskResponse | AskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["research"],
    summary="Create a persistent deep-research task",
    description=(
        "Deep mode enumerates a governed candidate corpus and reviews documents asynchronously. "
        "A new task costs three quota units; upgrading the same basic answer reserves only two additional units. "
        "Ambiguous questions return clarification before task creation and do not reserve quota."
    ),
)
async def create_research_task(
    payload: ResearchTaskCreateRequest,
    http_request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(authenticated_principal)],
) -> ResearchTaskResponse | AskResponse:
    if not principal.user_id or not principal.quota_managed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "ACCOUNT_REQUIRED",
                "message": "深度模式需要注册账号及账号级配额，旧版服务密钥不能创建研究任务。",
            },
        )
    settings = get_settings()
    store = get_account_store(settings)
    await enforce_rate_limit(principal.rate_limit_key)
    channel = "web" if principal.auth_type == "session" else "api"
    request_id = "req_" + uuid4().hex
    task_id = "research_" + uuid4().hex
    quota_reserved = False
    try:
        conversation_id = store.ensure_conversation(
            principal.user_id,
            payload.session_id,
            payload.question,
        )
        previous_question = store.latest_user_question(principal.user_id, conversation_id)
        retrieval_question = contextualize_follow_up(payload.question, previous_question)
        if not domain_gate.check(retrieval_question).in_scope:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "OUT_OF_SCOPE",
                    "message": "深度模式仅处理矿产资源、地质标准规范和相关政策技术问题。",
                },
            )
        resolution = await resolve_question(
            settings,
            retrieval_question,
            mode="deep",
        )
        if resolution.requires_clarification:
            response.status_code = status.HTTP_200_OK
            quota_snapshot = store.quota_snapshot(
                principal.user_id,
                settings.quota_timezone,
            )
            result = clarification_response(
                resolution,
                session_id=conversation_id,
                request_id=request_id,
                mode="deep",
                quota=quota_snapshot,
            )
            store.save_exchange(
                principal.user_id,
                conversation_id,
                request_id,
                payload.question,
                result.answer,
                {
                    "status": result.status,
                    "mode": "deep",
                    "clarification": result.clarification.model_dump(mode="json")
                    if result.clarification
                    else None,
                    "retrieval_question": retrieval_question,
                    "question_resolution": {
                        "model_used": resolution.model_used,
                        "canonical_question": resolution.canonical_question,
                        "error": resolution.error,
                    },
                    "quota": result.quota.model_dump(mode="json") if result.quota else None,
                },
            )
            usage_logger.write(
                {
                    "user_id": principal.user_id,
                    "credential_id": principal.credential_id,
                    "auth_type": principal.auth_type,
                    "endpoint": "/api/research/tasks",
                    "method": "POST",
                    "client_host": http_request.client.host if http_request.client else None,
                    "request_id": request_id,
                    "question_chars": len(payload.question),
                    "status": result.status,
                    "quota_reserved_units": 0,
                    "quota_remaining": result.quota.remaining if result.quota else None,
                    "question_resolution_used": resolution.model_used,
                    "question_resolution_error": resolution.error,
                }
            )
            return result
        retrieval_question = resolution.canonical_question
        reserved_units = store.research_upgrade_quota_cost(
            principal.user_id,
            payload.source_request_id,
            conversation_id,
            payload.question,
        )
        quota = store.reserve_qa_quota(
            principal.user_id,
            request_id,
            channel,
            principal.credential_id if principal.auth_type == "api_key" else None,
            conversation_id,
            len(payload.question),
            settings.quota_timezone,
            quota_units=reserved_units,
            request_mode="deep",
            parent_request_id=payload.source_request_id,
        )
        quota_reserved = True
        task = store.create_research_task(
            task_id=task_id,
            request_id=request_id,
            user_id=principal.user_id,
            api_key_id=principal.credential_id if principal.auth_type == "api_key" else None,
            conversation_id=conversation_id,
            channel=channel,
            question=payload.question,
            retrieval_question=retrieval_question,
            filters=payload.filters.model_dump(exclude_none=True),
            reserved_quota_units=reserved_units,
        )
    except HTTPException:
        raise
    except AccountStoreError as error:
        if quota_reserved:
            store.fail_qa_quota(request_id, settings.quota_timezone)
        raise account_error(error) from error

    research_runner.schedule(task_id)
    usage_logger.write(
        {
            "user_id": principal.user_id,
            "credential_id": principal.credential_id,
            "auth_type": principal.auth_type,
            "endpoint": "/api/research/tasks",
            "method": "POST",
            "client_host": http_request.client.host if http_request.client else None,
            "request_id": request_id,
            "task_id": task_id,
            "question_chars": len(payload.question),
            "status": "queued",
            "quota_reserved_units": int(task["reserved_quota_units"]),
            "quota_remaining": quota["remaining"],
            "question_resolution_used": resolution.model_used,
            "question_resolution_error": resolution.error,
        }
    )
    return research_task_response(task, quota)


@app.get(
    "/api/research/tasks/{task_id}",
    response_model=ResearchTaskResponse,
    tags=["research"],
    summary="Get deep-research progress",
)
async def research_task_status(
    task_id: str,
    principal: Annotated[Principal, Depends(authenticated_principal)],
) -> ResearchTaskResponse:
    if not principal.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account required")
    settings = get_settings()
    try:
        task = get_account_store(settings).get_research_task(principal.user_id, task_id)
        quota = get_account_store(settings).quota_snapshot(principal.user_id, settings.quota_timezone)
    except AccountStoreError as error:
        raise account_error(error) from error
    return research_task_response(task, quota)


@app.get(
    "/api/research/tasks/{task_id}/result",
    response_model=ResearchResult,
    tags=["research"],
    summary="Get a completed deep-research result",
)
async def research_task_result(
    task_id: str,
    principal: Annotated[Principal, Depends(authenticated_principal)],
) -> ResearchResult:
    if not principal.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account required")
    try:
        task = get_account_store().get_research_task(principal.user_id, task_id)
    except AccountStoreError as error:
        raise account_error(error) from error
    if not task.get("result"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "RESEARCH_RESULT_NOT_READY",
                "message": "深度研究结果尚未生成。",
                "status": task["status"],
            },
        )
    return ResearchResult.model_validate(task["result"])


@app.post(
    "/api/research/tasks/{task_id}/cancel",
    response_model=ResearchTaskResponse,
    tags=["research"],
    summary="Cancel a queued deep-research task",
)
async def cancel_research_task(
    task_id: str,
    principal: Annotated[Principal, Depends(authenticated_principal)],
) -> ResearchTaskResponse:
    if not principal.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account required")
    settings = get_settings()
    store = get_account_store(settings)
    try:
        current = store.get_research_task(principal.user_id, task_id)
        if current["status"] != "queued":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "RESEARCH_TASK_ALREADY_STARTED",
                    "message": "只有仍在排队的深度任务可以取消。",
                    "status": current["status"],
                },
            )
        task = store.cancel_queued_research_task(principal.user_id, task_id)
        store.fail_qa_quota(task["request_id"], settings.quota_timezone)
        quota = store.quota_snapshot(principal.user_id, settings.quota_timezone)
        quota["consumed"] = False
        quota["consumed_units"] = 0
    except HTTPException:
        raise
    except AccountStoreError as error:
        raise account_error(error) from error
    return research_task_response(task, quota)


@app.post(
    "/api/feedback",
    response_model=FeedbackResponse,
    tags=["feedback"],
    summary="Submit answer feedback",
)
async def feedback(
    payload: FeedbackRequest,
    http_request: Request,
    principal: Annotated[Principal, Depends(authenticated_principal)],
) -> FeedbackResponse:
    await enforce_rate_limit(principal.rate_limit_key)
    record = get_account_store().create_feedback(
        user_id=principal.user_id,
        api_key_id=principal.credential_id if principal.auth_type == "api_key" else None,
        conversation_id=payload.session_id,
        request_id=payload.request_id,
        rating=payload.rating,
        reason=payload.reason,
        comment=payload.comment,
        question=payload.question,
    )
    try:
        feedback_logger.write(
            {
                "feedback_id": record["feedback_id"],
                "user_id": principal.user_id,
                "credential_id": principal.credential_id,
                "auth_type": principal.auth_type,
                "endpoint": "/api/feedback",
                "method": "POST",
                "client_host": http_request.client.host if http_request.client else None,
                "session_id": payload.session_id,
                "request_id": payload.request_id,
                "rating": payload.rating,
                "reason": payload.reason,
                "comment": payload.comment,
                "question": payload.question,
                "review_lane": record["review_lane"],
                "status": record["status"],
            }
        )
    except OSError:
        logger.exception("Unable to append feedback audit log for %s", record["feedback_id"])
    return FeedbackResponse(
        feedback_id=record["feedback_id"],
        review_lane=record["review_lane"],
        status=record["status"],
    )


@app.get(
    "/api/standards",
    response_model=StandardsResponse,
    tags=["catalog"],
    summary="Search the standard catalog",
)
async def standards(
    http_request: Request,
    principal: Annotated[Principal, Depends(authenticated_principal)],
    q: Annotated[str | None, Query()] = None,
    standard_no: Annotated[str | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    text_access: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> StandardsResponse:
    started = perf_counter()
    await enforce_rate_limit(principal.rate_limit_key)
    client = KnowledgeClient(get_settings())
    result = await client.standards(
        {
            "q": q,
            "standard_no": standard_no,
            "status": status_filter,
            "text_access": text_access,
            "page": page,
            "page_size": page_size,
        }
    )
    usage_logger.write(
        {
            "user_id": principal.user_id,
            "credential_id": principal.credential_id,
            "auth_type": principal.auth_type,
            "endpoint": "/api/standards",
            "method": "GET",
            "client_host": http_request.client.host if http_request.client else None,
            "query": q,
            "standard_no": standard_no,
            "result_count": len(result.items),
            "duration_ms": round((perf_counter() - started) * 1000, 2),
        }
    )
    return result


@app.get(
    "/api/usage",
    tags=["usage"],
    summary="Get current account or legacy API-key usage",
)
async def usage(principal: Annotated[Principal, Depends(authenticated_principal)]) -> dict[str, object]:
    await enforce_rate_limit(principal.rate_limit_key)
    settings = get_settings()
    if principal.user_id:
        account_usage = get_account_store(settings).account_summary(
            principal.user_id,
            settings.quota_timezone,
        )
        scope = "account"
    else:
        account_usage = usage_stats.summarize(None)
        scope = "legacy_service"
    return {
        "scope": scope,
        "rate_limit": {
            "enabled": settings.rate_limit_enabled,
            "limit_per_minute": settings.rate_limit_per_minute,
            "backend": rate_limiter.last_backend,
        },
        "quota_policy": {
            "mode": "daily_account_quota_units",
            "timezone": settings.quota_timezone,
            "web_and_api_keys_shared": True,
            "system_errors_refunded": True,
            "out_of_scope_not_consumed": True,
            "basic_cost": 1,
            "deep_cost": 3,
            "basic_to_deep_additional_cost": 2,
        },
        "usage": account_usage,
    }


@app.get("/api/admin/invitations", tags=["admin"], summary="List invitation metadata")
async def admin_invitations(principal: Annotated[Principal, Depends(admin_principal)]) -> dict[str, object]:
    return {"items": get_account_store().list_invitations()}


@app.post("/api/admin/invitations", tags=["admin"], summary="Create an invitation code")
async def admin_create_invitation(
    payload: InvitationCreateRequest,
    principal: Annotated[Principal, Depends(admin_principal)],
) -> dict[str, object]:
    record, code = get_account_store().create_invitation(
        principal.user_id,
        payload.label,
        payload.max_uses,
        payload.expires_in_days,
    )
    return {"item": record, "invite_code": code, "message": "邀请码只显示这一次。"}


@app.get("/api/admin/users", tags=["admin"], summary="List users and daily quotas")
async def admin_users(principal: Annotated[Principal, Depends(admin_principal)]) -> dict[str, object]:
    settings = get_settings()
    return {"items": get_account_store(settings).list_users(settings.quota_timezone)}


@app.get("/api/admin/feedback", tags=["admin"], summary="List answer feedback queue")
async def admin_feedback(
    principal: Annotated[Principal, Depends(admin_principal)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    review_lane: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, object]:
    return {
        "items": get_account_store().list_feedback(
            status_filter=status_filter,
            review_lane=review_lane,
            limit=limit,
        )
    }


@app.post("/api/admin/feedback/{feedback_id}/status", tags=["admin"], summary="Update feedback status")
async def admin_feedback_status(
    feedback_id: str,
    payload: FeedbackStatusUpdateRequest,
    principal: Annotated[Principal, Depends(admin_principal)],
) -> dict[str, object]:
    try:
        item = get_account_store().update_feedback_status(
            feedback_id,
            payload.status,
            payload.resolution_note,
            principal.user_id or "",
        )
    except AccountStoreError as error:
        raise account_error(error) from error
    return {"ok": True, "item": item}


@app.get("/api/admin/lexicon", tags=["admin"], summary="List governed domain lexicon records")
async def admin_lexicon(
    principal: Annotated[Principal, Depends(admin_principal)],
    entry_status: Annotated[str | None, Query()] = None,
    candidate_status: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
) -> dict[str, object]:
    store = get_lexicon_governance_store()
    return {
        "summary": store.summary(),
        "entries": store.list_entries(status=entry_status, query=q),
        "candidates": store.list_candidates(status=candidate_status),
        "audit": store.list_audit(limit=80),
    }


@app.post("/api/admin/lexicon/candidates", tags=["admin"], summary="Create a lexicon candidate")
async def admin_create_lexicon_candidate(
    payload: LexiconCandidateRequest,
    principal: Annotated[Principal, Depends(admin_principal)],
) -> dict[str, object]:
    try:
        item = get_lexicon_governance_store().create_candidate(
            payload.model_dump(mode="json"),
            principal.user_id or "",
        )
    except LexiconGovernanceError as error:
        raise lexicon_error(error) from error
    return {"ok": True, "item": item}


@app.put(
    "/api/admin/lexicon/candidates/{candidate_id}",
    tags=["admin"],
    summary="Update a lexicon candidate",
)
async def admin_update_lexicon_candidate(
    candidate_id: str,
    payload: LexiconCandidateRequest,
    principal: Annotated[Principal, Depends(admin_principal)],
) -> dict[str, object]:
    try:
        item = get_lexicon_governance_store().update_candidate(
            candidate_id,
            payload.model_dump(mode="json"),
            principal.user_id or "",
        )
    except LexiconGovernanceError as error:
        raise lexicon_error(error) from error
    return {"ok": True, "item": item}


@app.post("/api/admin/lexicon/preview", tags=["admin"], summary="Preview a lexicon candidate")
async def admin_preview_lexicon_candidate(
    payload: LexiconPreviewRequest,
    principal: Annotated[Principal, Depends(admin_principal)],
) -> dict[str, object]:
    try:
        preview = get_lexicon_governance_store().preview_candidate(
            payload.query,
            payload.candidate.model_dump(mode="json"),
            candidate_id=payload.candidate_id,
            actor_user_id=principal.user_id or "",
        )
    except LexiconGovernanceError as error:
        raise lexicon_error(error) from error
    return {"ok": True, **preview}


@app.post(
    "/api/admin/lexicon/candidates/{candidate_id}/review",
    tags=["admin"],
    summary="Approve or reject a lexicon candidate",
)
async def admin_review_lexicon_candidate(
    candidate_id: str,
    payload: LexiconReviewRequest,
    principal: Annotated[Principal, Depends(admin_principal)],
) -> dict[str, object]:
    try:
        item = get_lexicon_governance_store().review_candidate(
            candidate_id,
            payload.action,
            payload.note,
            principal.user_id or "",
        )
    except LexiconGovernanceError as error:
        raise lexicon_error(error) from error
    return {"ok": True, "item": item}


@app.post(
    "/api/admin/lexicon/entries/{lexicon_id}/status",
    tags=["admin"],
    summary="Activate or disable a governed lexicon entry",
)
async def admin_set_lexicon_entry_status(
    lexicon_id: str,
    payload: LexiconEntryStatusRequest,
    principal: Annotated[Principal, Depends(admin_principal)],
) -> dict[str, object]:
    try:
        item = get_lexicon_governance_store().set_entry_status(
            lexicon_id,
            payload.status,
            payload.note,
            principal.user_id or "",
        )
    except LexiconGovernanceError as error:
        raise lexicon_error(error) from error
    return {"ok": True, "item": item}


@app.post("/api/admin/users/{user_id}/daily-limit", tags=["admin"], summary="Set a user's daily limit")
async def admin_set_daily_limit(
    user_id: str,
    payload: DailyLimitUpdateRequest,
    principal: Annotated[Principal, Depends(admin_principal)],
) -> dict[str, object]:
    settings = get_settings()
    try:
        user = get_account_store(settings).set_daily_limit(
            user_id,
            payload.daily_limit,
            payload.reason,
            principal.user_id or "",
            settings.quota_timezone,
        )
    except AccountStoreError as error:
        raise account_error(error) from error
    return {"ok": True, "user": user}


@app.post("/api/admin/users/{user_id}/quota", tags=["admin"], summary="Add daily requests")
async def admin_add_daily_quota(
    user_id: str,
    payload: DailyQuotaAdjustmentRequest,
    principal: Annotated[Principal, Depends(admin_principal)],
) -> dict[str, object]:
    settings = get_settings()
    try:
        quota = get_account_store(settings).adjust_daily_quota(
            user_id,
            payload.extra_requests,
            payload.reason,
            principal.user_id or "",
            settings.quota_timezone,
            payload.date,
        )
    except AccountStoreError as error:
        raise account_error(error) from error
    return {"ok": True, "quota": quota}


@app.post("/api/admin/users/{user_id}/status", tags=["admin"], summary="Suspend or restore a user")
async def admin_set_user_status(
    user_id: str,
    payload: UserStatusRequest,
    principal: Annotated[Principal, Depends(admin_principal)],
) -> dict[str, object]:
    if user_id == principal.user_id and payload.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "SELF_SUSPEND_FORBIDDEN", "message": "管理员不能停用自己的当前账号。"},
        )
    try:
        user = get_account_store().set_user_status(user_id, payload.status)
    except AccountStoreError as error:
        raise account_error(error) from error
    return {"ok": True, "user": user}
