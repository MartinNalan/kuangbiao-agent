from __future__ import annotations

import logging
import os
from html import escape
from urllib.parse import quote

import httpx

from .config import Settings


logger = logging.getLogger(__name__)


class EmailDeliveryError(RuntimeError):
    pass


def https_proxy() -> str | None:
    return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None


def agentmail_proxy(settings: Settings) -> str | None:
    return settings.agentmail_proxy_url.strip() or https_proxy()


async def send_verification_email(settings: Settings, recipient: str, code: str) -> None:
    if settings.email_debug:
        logger.info("EMAIL_DEBUG enabled: skipped external delivery for %s", recipient)
        return
    if settings.email_provider != "agentmail":
        raise EmailDeliveryError(f"unsupported email provider: {settings.email_provider}")
    if not settings.agentmail_api_key or not settings.agentmail_inbox_id:
        raise EmailDeliveryError("AgentMail is not configured")

    ttl = settings.email_code_ttl_minutes
    subject = "geowiki 注册验证码"
    text = (
        f"你的 geowiki 注册验证码是：{code}\n\n"
        f"验证码在 {ttl} 分钟内有效，请勿转发给他人。"
        "如果这不是你的操作，请忽略本邮件。"
    )
    html = f"""
    <div style="font-family:Arial,'Microsoft YaHei',sans-serif;color:#172c28;line-height:1.7;max-width:560px">
      <h1 style="font-size:22px;margin:0 0 8px">geowiki</h1>
      <p style="margin:0 0 24px;color:#5f6f6b">一款专注地质领域的百科全搜</p>
      <p>你的注册验证码是：</p>
      <p style="font-size:30px;font-weight:700;letter-spacing:6px;margin:18px 0">{escape(code)}</p>
      <p>验证码在 {ttl} 分钟内有效，请勿转发给他人。</p>
      <p style="color:#7b8784">如果这不是你的操作，请忽略本邮件。</p>
    </div>
    """.strip()
    inbox_id = quote(settings.agentmail_inbox_id, safe="")
    url = f"{settings.agentmail_base_url.rstrip('/')}/inboxes/{inbox_id}/messages/send"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=5.0),
            proxy=agentmail_proxy(settings),
            trust_env=False,
        ) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.agentmail_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "to": [recipient],
                    "subject": subject,
                    "text": text,
                    "html": html,
                },
            )
            response.raise_for_status()
    except (httpx.HTTPError, ValueError) as error:
        logger.exception("AgentMail failed to send a verification email")
        raise EmailDeliveryError("verification email delivery failed") from error
