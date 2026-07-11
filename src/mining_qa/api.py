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
from .feedback_log import FeedbackLogger
from .knowledge_client import KnowledgeClient
from .query_understanding import contextualize_follow_up
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
    LoginRequest,
    PasswordChangeRequest,
    QuotaInfo,
    RegisterRequest,
    StandardsResponse,
    UserStatusRequest,
)
from .usage_log import UsageLogger
from .usage_stats import UsageStats
from . import __version__


logger = logging.getLogger(__name__)

OPENAPI_TAGS = [
    {"name": "system", "description": "Service health and runtime metadata."},
    {"name": "auth", "description": "Invite-only registration and browser session authentication."},
    {"name": "account", "description": "Current account, API keys, daily quota, and conversation history."},
    {"name": "qa", "description": "Controlled public QA API for mineral-resource standards and policies."},
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
        "quota_mode": "daily_account_quota",
        "daily_quota_default": settings.daily_quota_default,
        "quota_timezone": settings.quota_timezone,
        "email_verification_ready": settings.email_verification_ready,
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
        "Answered and in-scope evidence-gap requests consume daily quota; out-of-scope and system-error results do not."
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

    await enforce_rate_limit(principal.rate_limit_key)
    if principal.user_id:
        try:
            conversation_id = store.ensure_conversation(principal.user_id, payload.session_id, payload.question)
            payload.session_id = conversation_id
            previous_question = store.latest_user_question(principal.user_id, conversation_id)
            payload._retrieval_question = contextualize_follow_up(payload.question, previous_question)
            if principal.quota_managed:
                store.reserve_qa_quota(
                    principal.user_id,
                    request_id,
                    channel,
                    principal.credential_id if principal.auth_type == "api_key" else None,
                    conversation_id,
                    len(payload.question),
                    settings.quota_timezone,
                )
                quota_reserved = True
        except AccountStoreError as error:
            raise account_error(error) from error

    agent = MiningQAAgent(settings)
    try:
        result = await agent.ask(payload)
        result.request_id = request_id
        if quota_reserved:
            settlement = store.settle_qa_quota(
                request_id,
                result.status,
                len(result.answer),
                settings.quota_timezone,
            )
            result.quota = QuotaInfo(**settlement)
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
            "quota_remaining": result.quota.remaining if result.quota else None,
            "duration_ms": round((perf_counter() - started) * 1000, 2),
        }
    )
    return result


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
            "mode": "daily_account_quota",
            "timezone": settings.quota_timezone,
            "web_and_api_keys_shared": True,
            "system_errors_refunded": True,
            "out_of_scope_not_consumed": True,
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
