from __future__ import annotations

import argparse
import getpass
import json
import secrets
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.account_store import AccountStore, AccountStoreError  # noqa: E402
from mining_qa.config import get_settings  # noqa: E402


def resolved_db_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def password_from_prompt(generate: bool = False) -> tuple[str, bool]:
    if generate:
        return secrets.token_urlsafe(18), True
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")
    if len(password) < 8:
        raise SystemExit("Password must contain at least 8 characters.")
    return password, False


def build_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Manage geowiki users, invitations, and daily quotas.")
    parser.add_argument("--db", default=settings.app_db_path, help="Application SQLite path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_admin = subparsers.add_parser("create-admin", help="Create an administrator account")
    create_admin.add_argument("--account", required=True)
    create_admin.add_argument("--display-name", default="管理员")
    create_admin.add_argument("--daily-limit", type=int, default=settings.daily_quota_default)
    create_admin.add_argument(
        "--generate-password",
        action="store_true",
        help="Generate and display a one-time temporary password",
    )

    create_invite = subparsers.add_parser("create-invite", help="Create an invitation code")
    create_invite.add_argument("--label", required=True)
    create_invite.add_argument("--max-uses", type=int, default=1)
    create_invite.add_argument("--days", type=int, default=30)
    create_invite.add_argument("--admin-account", default=None)

    set_limit = subparsers.add_parser("set-daily-limit", help="Set one account's persistent daily limit")
    set_limit.add_argument("--account", required=True)
    set_limit.add_argument("--limit", type=int, required=True)
    set_limit.add_argument("--reason", default="CLI daily-limit adjustment")
    set_limit.add_argument("--admin-account", required=True)

    add_quota = subparsers.add_parser("add-quota", help="Add requests for one account on a date")
    add_quota.add_argument("--account", required=True)
    add_quota.add_argument("--count", type=int, required=True)
    add_quota.add_argument("--date", default=None, help="YYYY-MM-DD; defaults to today")
    add_quota.add_argument("--reason", default="CLI daily-quota adjustment")
    add_quota.add_argument("--admin-account", required=True)

    status_parser = subparsers.add_parser("set-status", help="Suspend or restore one account")
    status_parser.add_argument("--account", required=True)
    status_parser.add_argument("--status", choices=["active", "suspended"], required=True)

    subparsers.add_parser("list-users", help="List user metadata and today's quotas")
    subparsers.add_parser("list-invites", help="List invitation metadata")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    settings = get_settings()
    store = AccountStore(resolved_db_path(args.db))
    try:
        if args.command == "create-admin":
            password, generated = password_from_prompt(args.generate_password)
            user = store.create_user(
                args.account,
                password,
                args.display_name,
                args.daily_limit,
                role="admin",
            )
            payload = {"user": user}
            if generated:
                payload["temporary_password"] = password
                payload["message"] = "Change this password after the first login."
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "create-invite":
            created_by = None
            if args.admin_account:
                created_by = store.get_user_by_account(args.admin_account)["user_id"]
            record, code = store.create_invitation(
                created_by,
                args.label,
                max_uses=args.max_uses,
                expires_in_days=args.days,
            )
            print(json.dumps({"item": record, "invite_code": code}, ensure_ascii=False, indent=2))
            print("The invitation code is displayed only once.", file=sys.stderr)
            return 0

        if args.command == "set-daily-limit":
            user = store.get_user_by_account(args.account)
            admin = store.get_user_by_account(args.admin_account)
            updated = store.set_daily_limit(
                user["user_id"],
                args.limit,
                args.reason,
                admin["user_id"],
                settings.quota_timezone,
            )
            print(json.dumps(updated, ensure_ascii=False, indent=2))
            return 0

        if args.command == "add-quota":
            if args.count <= 0:
                raise SystemExit("--count must be positive.")
            user = store.get_user_by_account(args.account)
            admin = store.get_user_by_account(args.admin_account)
            updated = store.adjust_daily_quota(
                user["user_id"],
                args.count,
                args.reason,
                admin["user_id"],
                settings.quota_timezone,
                args.date,
            )
            print(json.dumps(updated, ensure_ascii=False, indent=2))
            return 0

        if args.command == "set-status":
            user = store.get_user_by_account(args.account)
            updated = store.set_user_status(user["user_id"], args.status)
            print(json.dumps(updated, ensure_ascii=False, indent=2))
            return 0

        if args.command == "list-users":
            print(
                json.dumps(
                    {"items": store.list_users(settings.quota_timezone)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if args.command == "list-invites":
            print(json.dumps({"items": store.list_invitations()}, ensure_ascii=False, indent=2))
            return 0
    except AccountStoreError as error:
        print(f"Account operation failed: {error}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
