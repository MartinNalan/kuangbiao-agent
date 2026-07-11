from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"


def update_env(path: Path, updates: dict[str, str], remove: set[str] | None = None) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    removed = remove or set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in removed:
                continue
            if key in pending:
                output.append(f"{key}={pending.pop(key)}")
                continue
        output.append(line)
    if pending:
        if output and output[-1]:
            output.append("")
        output.append("# geowiki registration email verification")
        output.extend(f"{key}={value}" for key, value in pending.items())
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def find_inbox(payload: Any, preferred_id: str) -> dict[str, Any] | None:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("inboxes") or payload.get("items") or payload.get("data") or []
    else:
        items = []
    if isinstance(items, dict):
        items = items.get("inboxes") or items.get("items") or []
    for item in items:
        if not isinstance(item, dict):
            continue
        inbox_id = item.get("inbox_id") or item.get("email")
        if inbox_id == preferred_id or item.get("client_id") == "geowiki-registration-sender":
            return item
    return None


def create_or_reuse_inbox(api_key: str, base_url: str, proxy_url: str | None = None) -> str:
    preferred_id = "geowiki@agentmail.to"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "username": "geowiki",
        "domain": "agentmail.to",
        "display_name": "geowiki",
        "client_id": "geowiki-registration-sender",
        "metadata": {"application": "geowiki", "purpose": "registration-verification"},
    }
    proxy = proxy_url or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None
    try:
        with httpx.Client(
            timeout=httpx.Timeout(20.0, connect=5.0),
            proxy=proxy,
            trust_env=False,
        ) as client:
            response = client.post(f"{base_url.rstrip('/')}/inboxes", headers=headers, json=body)
            if response.is_success:
                payload = response.json()
                return str(payload.get("inbox_id") or payload.get("email") or preferred_id)

            listed = client.get(f"{base_url.rstrip('/')}/inboxes", headers=headers, params={"limit": 100})
            if listed.is_success:
                existing = find_inbox(listed.json(), preferred_id)
                if existing:
                    return str(existing.get("inbox_id") or existing.get("email"))
            raise SystemExit(
                f"AgentMail inbox setup failed: HTTP {response.status_code} {response.text[:300]}"
            )
    except httpx.HTTPError as error:
        raise SystemExit(f"AgentMail network request failed: {error}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure AgentMail for geowiki registration emails.")
    parser.add_argument("--env", type=Path, default=ENV_PATH)
    parser.add_argument(
        "--defer-inbox",
        action="store_true",
        help="Write quota and verification settings without contacting AgentMail",
    )
    args = parser.parse_args()

    values = dotenv_values(args.env)
    api_key = str(values.get("AGENTMAIL_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit(f"AGENTMAIL_API_KEY is missing from {args.env}")
    base_url = str(values.get("AGENTMAIL_BASE_URL") or "https://api.agentmail.to/v0").strip()
    proxy_url = str(values.get("AGENTMAIL_PROXY_URL") or "").strip() or None
    inbox_id = str(values.get("AGENTMAIL_INBOX_ID") or "").strip()
    if not inbox_id and not args.defer_inbox:
        inbox_id = create_or_reuse_inbox(api_key, base_url, proxy_url=proxy_url)

    verification_secret = str(values.get("EMAIL_VERIFICATION_SECRET") or "").strip()
    if not verification_secret:
        verification_secret = secrets.token_urlsafe(48)

    updates = {
        "EMAIL_VERIFICATION_ENABLED": "true",
        "EMAIL_VERIFICATION_SECRET": verification_secret,
        "EMAIL_CODE_TTL_MINUTES": str(values.get("EMAIL_CODE_TTL_MINUTES") or "10"),
        "EMAIL_CODE_COOLDOWN_SECONDS": str(values.get("EMAIL_CODE_COOLDOWN_SECONDS") or "60"),
        "EMAIL_CODE_DAILY_LIMIT": str(values.get("EMAIL_CODE_DAILY_LIMIT") or "5"),
        "EMAIL_DEBUG": "false",
        "EMAIL_PROVIDER": "agentmail",
        "AGENTMAIL_BASE_URL": base_url,
        "DAILY_QUOTA_DEFAULT": str(values.get("DAILY_QUOTA_DEFAULT") or "10"),
        "QUOTA_TIMEZONE": str(values.get("QUOTA_TIMEZONE") or "Asia/Shanghai"),
    }
    if inbox_id:
        updates["AGENTMAIL_INBOX_ID"] = inbox_id
    update_env(
        args.env,
        updates,
        remove={"TRIAL_CREDIT_MICROS", "ANSWERED_FEE_MICROS"},
    )
    if inbox_id:
        print(f"AgentMail configured for geowiki with inbox {inbox_id}.")
    else:
        print("Local email verification settings prepared; inbox creation is deferred.")
    print(f"Updated {args.env}; API key and verification secret were not displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
