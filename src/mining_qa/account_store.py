from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PASSWORD_SCRYPT_N = 2**14
PASSWORD_SCRYPT_R = 8
PASSWORD_SCRYPT_P = 1


class AccountStoreError(RuntimeError):
    pass


class DuplicateAccountError(AccountStoreError):
    pass


class InvalidInviteError(AccountStoreError):
    pass


class InvalidCredentialsError(AccountStoreError):
    pass


class InvalidEmailCodeError(AccountStoreError):
    pass


class EmailCodeCooldownError(AccountStoreError):
    def __init__(self, retry_after_seconds: int):
        super().__init__("email verification cooldown")
        self.retry_after_seconds = retry_after_seconds


class EmailCodeDailyLimitError(AccountStoreError):
    pass


class DailyQuotaExceededError(AccountStoreError):
    def __init__(self, quota: dict[str, Any]):
        super().__init__("daily quota exceeded")
        self.quota = quota


class ResourceNotFoundError(AccountStoreError):
    pass


class PermissionDeniedError(AccountStoreError):
    pass


class ActiveResearchTaskError(AccountStoreError):
    def __init__(self, task_id: str | None = None):
        super().__init__("an active research task already exists")
        self.task_id = task_id


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_account(account: str) -> str:
    return account.strip().casefold()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_verification_code(secret: str, email: str, code: str) -> str:
    payload = f"{normalize_account(email)}:{code.strip()}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=PASSWORD_SCRYPT_N,
        r=PASSWORD_SCRYPT_R,
        p=PASSWORD_SCRYPT_P,
        dklen=32,
    )
    return "$".join(
        [
            "scrypt",
            str(PASSWORD_SCRYPT_N),
            str(PASSWORD_SCRYPT_R),
            str(PASSWORD_SCRYPT_P),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n_value, r_value, p_value, salt_value, digest_value = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_value.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_value.encode("ascii"))
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_value),
            r=int(r_value),
            p=int(p_value),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_expired(value: str | None) -> bool:
    parsed = _parse_datetime(value)
    return bool(value) and (parsed is None or parsed <= datetime.now(timezone.utc))


def usage_date(timezone_name: str, current: datetime | None = None) -> str:
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    now = current or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(zone).date().isoformat()


class AccountStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    account TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    status TEXT NOT NULL DEFAULT 'active',
                    daily_limit INTEGER NOT NULL DEFAULT 10,
                    email_verified_at TEXT,
                    created_at TEXT NOT NULL,
                    last_login_at TEXT
                );

                CREATE TABLE IF NOT EXISTS invitations (
                    invitation_id TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL UNIQUE,
                    code_prefix TEXT NOT NULL,
                    label TEXT NOT NULL,
                    max_uses INTEGER NOT NULL DEFAULT 1,
                    used_count INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    expires_at TEXT,
                    created_by TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (created_by) REFERENCES users(user_id)
                );

                CREATE TABLE IF NOT EXISTS email_verifications (
                    verification_id TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 5,
                    request_ip TEXT,
                    created_at TEXT NOT NULL,
                    consumed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    revoked_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS user_api_keys (
                    api_key_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    key_prefix TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    revoked_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    request_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS daily_usage (
                    user_id TEXT NOT NULL,
                    usage_date TEXT NOT NULL,
                    used_count INTEGER NOT NULL DEFAULT 0,
                    reserved_count INTEGER NOT NULL DEFAULT 0,
                    bonus_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, usage_date),
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS quota_adjustments (
                    adjustment_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    adjustment_type TEXT NOT NULL,
                    usage_date TEXT NOT NULL,
                    delta_count INTEGER NOT NULL,
                    previous_limit INTEGER,
                    new_limit INTEGER,
                    reason TEXT NOT NULL,
                    admin_user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY (admin_user_id) REFERENCES users(user_id)
                );

                CREATE TABLE IF NOT EXISTS qa_requests (
                    request_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    api_key_id TEXT,
                    conversation_id TEXT,
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL,
                    quota_date TEXT,
                    quota_consumed INTEGER NOT NULL DEFAULT 0,
                    quota_units INTEGER NOT NULL DEFAULT 1,
                    quota_consumed_units INTEGER NOT NULL DEFAULT 0,
                    request_mode TEXT NOT NULL DEFAULT 'basic',
                    parent_request_id TEXT,
                    question_chars INTEGER NOT NULL DEFAULT 0,
                    answer_chars INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY (api_key_id) REFERENCES user_api_keys(api_key_id),
                    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
                );

                CREATE TABLE IF NOT EXISTS research_tasks (
                    task_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL,
                    api_key_id TEXT,
                    conversation_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    question TEXT NOT NULL,
                    retrieval_question TEXT NOT NULL,
                    filters_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress_percent INTEGER NOT NULL DEFAULT 0,
                    status_message TEXT NOT NULL DEFAULT '',
                    total_documents INTEGER NOT NULL DEFAULT 0,
                    examined_documents INTEGER NOT NULL DEFAULT 0,
                    evidence_documents INTEGER NOT NULL DEFAULT 0,
                    quota_cost INTEGER NOT NULL DEFAULT 3,
                    reserved_quota_units INTEGER NOT NULL DEFAULT 3,
                    plan_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    FOREIGN KEY (request_id) REFERENCES qa_requests(request_id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY (api_key_id) REFERENCES user_api_keys(api_key_id),
                    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
                );

                CREATE TABLE IF NOT EXISTS answer_feedback (
                    feedback_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    api_key_id TEXT,
                    conversation_id TEXT,
                    request_id TEXT,
                    rating TEXT NOT NULL,
                    reason TEXT,
                    comment TEXT,
                    question TEXT,
                    review_lane TEXT NOT NULL,
                    status TEXT NOT NULL,
                    resolution_note TEXT,
                    resolved_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE SET NULL,
                    FOREIGN KEY (api_key_id) REFERENCES user_api_keys(api_key_id) ON DELETE SET NULL,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id) ON DELETE SET NULL,
                    FOREIGN KEY (resolved_by) REFERENCES users(user_id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_email_verifications_email_created
                    ON email_verifications(email, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash);
                CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash ON user_api_keys(key_hash);
                CREATE INDEX IF NOT EXISTS idx_conversations_user_updated ON conversations(user_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_conversation_created ON messages(conversation_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_requests_user_created ON qa_requests(user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_research_user_created
                    ON research_tasks(user_id, created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_research_one_active_user
                    ON research_tasks(user_id)
                    WHERE status IN ('queued', 'planning', 'retrieving', 'analyzing');
                CREATE INDEX IF NOT EXISTS idx_feedback_status_created
                    ON answer_feedback(status, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_feedback_request ON answer_feedback(request_id);
                CREATE INDEX IF NOT EXISTS idx_quota_adjustments_user_created
                    ON quota_adjustments(user_id, created_at DESC);
                """
            )
            self._ensure_column(connection, "users", "daily_limit", "INTEGER NOT NULL DEFAULT 10")
            self._ensure_column(connection, "users", "email_verified_at", "TEXT")
            self._ensure_column(connection, "qa_requests", "quota_date", "TEXT")
            self._ensure_column(connection, "qa_requests", "quota_consumed", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "qa_requests", "quota_units", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(
                connection,
                "qa_requests",
                "quota_consumed_units",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(connection, "qa_requests", "request_mode", "TEXT NOT NULL DEFAULT 'basic'")
            self._ensure_column(connection, "qa_requests", "parent_request_id", "TEXT")
            self._ensure_column(
                connection,
                "research_tasks",
                "reserved_quota_units",
                "INTEGER NOT NULL DEFAULT 3",
            )
            connection.execute(
                """
                UPDATE qa_requests
                SET quota_consumed_units = quota_consumed
                WHERE quota_consumed != 0 AND quota_consumed_units = 0
                """
            )
            connection.commit()

    def _ensure_column(self, connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def user_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM users").fetchone()
        return int(row["total"] if row else 0)

    def account_exists(self, account: str) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT 1 FROM users WHERE account = ?", (normalize_account(account),)).fetchone()
        return row is not None

    def validate_invitation(self, invite_code: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM invitations WHERE code_hash = ?",
                (hash_token(invite_code.strip()),),
            ).fetchone()
        if not self._invite_is_valid(row):
            raise InvalidInviteError("invalid or exhausted invitation")
        return self._invitation_payload(row)

    def create_user(
        self,
        account: str,
        password: str,
        display_name: str,
        daily_limit: int,
        role: str = "user",
    ) -> dict[str, Any]:
        return self._create_user(account, password, display_name, daily_limit, role, None, None, None)

    def register_user(
        self,
        account: str,
        password: str,
        display_name: str,
        invite_code: str,
        email_code: str,
        daily_limit: int,
        verification_secret: str,
    ) -> dict[str, Any]:
        return self._create_user(
            account,
            password,
            display_name,
            daily_limit,
            "user",
            invite_code,
            email_code,
            verification_secret,
        )

    def _create_user(
        self,
        account: str,
        password: str,
        display_name: str,
        daily_limit: int,
        role: str,
        invite_code: str | None,
        email_code: str | None,
        verification_secret: str | None,
    ) -> dict[str, Any]:
        normalized = normalize_account(account)
        now = utc_now()
        user_id = "usr_" + uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            invite = None
            if invite_code is not None:
                invite = connection.execute(
                    "SELECT * FROM invitations WHERE code_hash = ?",
                    (hash_token(invite_code.strip()),),
                ).fetchone()
                if not self._invite_is_valid(invite):
                    raise InvalidInviteError("invalid or exhausted invitation")

            verification = None
            if email_code is not None:
                if not verification_secret:
                    raise InvalidEmailCodeError("verification secret is missing")
                verification = connection.execute(
                    """
                    SELECT * FROM email_verifications
                    WHERE email = ? AND consumed_at IS NULL
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (normalized,),
                ).fetchone()
                if (
                    not verification
                    or _is_expired(verification["expires_at"])
                    or verification["attempts"] >= verification["max_attempts"]
                ):
                    raise InvalidEmailCodeError("verification code is invalid or expired")
                expected_hash = hash_verification_code(verification_secret, normalized, email_code)
                if not hmac.compare_digest(expected_hash, verification["code_hash"]):
                    connection.execute(
                        "UPDATE email_verifications SET attempts = attempts + 1 WHERE verification_id = ?",
                        (verification["verification_id"],),
                    )
                    connection.commit()
                    raise InvalidEmailCodeError("verification code is invalid or expired")

            verified_at = now if verification is not None or role == "admin" else None
            try:
                connection.execute(
                    """
                    INSERT INTO users(
                        user_id, account, display_name, password_hash, role, status,
                        daily_limit, email_verified_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        user_id,
                        normalized,
                        display_name.strip(),
                        hash_password(password),
                        role,
                        max(1, daily_limit),
                        verified_at,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise DuplicateAccountError(normalized) from error

            if invite is not None:
                connection.execute(
                    "UPDATE invitations SET used_count = used_count + 1 WHERE invitation_id = ?",
                    (invite["invitation_id"],),
                )
            if verification is not None:
                connection.execute(
                    "UPDATE email_verifications SET consumed_at = ? WHERE verification_id = ?",
                    (now, verification["verification_id"]),
                )
            connection.commit()
        return self.get_user(user_id)

    def create_email_verification(
        self,
        email: str,
        code: str,
        verification_secret: str,
        ttl_minutes: int,
        cooldown_seconds: int,
        daily_send_limit: int,
        request_ip: str | None,
    ) -> str:
        normalized = normalize_account(email)
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        day_cutoff = now_dt - timedelta(hours=24)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM users WHERE account = ?", (normalized,)).fetchone():
                raise DuplicateAccountError(normalized)
            latest = connection.execute(
                "SELECT created_at FROM email_verifications WHERE email = ? ORDER BY created_at DESC LIMIT 1",
                (normalized,),
            ).fetchone()
            if latest:
                last_sent = _parse_datetime(latest["created_at"])
                if last_sent:
                    elapsed = int((now_dt - last_sent).total_seconds())
                    if elapsed < cooldown_seconds:
                        connection.commit()
                        raise EmailCodeCooldownError(cooldown_seconds - elapsed)
            sent_count = connection.execute(
                "SELECT COUNT(*) AS total FROM email_verifications WHERE email = ? AND created_at >= ?",
                (normalized, day_cutoff.isoformat()),
            ).fetchone()
            if int(sent_count["total"] or 0) >= daily_send_limit:
                connection.commit()
                raise EmailCodeDailyLimitError("daily email verification limit reached")

            verification_id = "emv_" + uuid4().hex
            connection.execute(
                """
                INSERT INTO email_verifications(
                    verification_id, email, code_hash, expires_at, attempts,
                    max_attempts, request_ip, created_at
                ) VALUES (?, ?, ?, ?, 0, 5, ?, ?)
                """,
                (
                    verification_id,
                    normalized,
                    hash_verification_code(verification_secret, normalized, code),
                    (now_dt + timedelta(minutes=ttl_minutes)).isoformat(),
                    request_ip,
                    now_dt.isoformat(),
                ),
            )
            connection.commit()
        return verification_id

    def cancel_email_verification(self, verification_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM email_verifications WHERE verification_id = ? AND consumed_at IS NULL",
                (verification_id,),
            )
            connection.commit()

    def authenticate_user(self, account: str, password: str) -> dict[str, Any]:
        normalized = normalize_account(account)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE account = ?", (normalized,)).fetchone()
            if not row or row["status"] != "active" or not verify_password(password, row["password_hash"]):
                raise InvalidCredentialsError("invalid account or password")
            now = utc_now()
            connection.execute("UPDATE users SET last_login_at = ? WHERE user_id = ?", (now, row["user_id"]))
            connection.commit()
        return self.get_user(row["user_id"])

    def get_user(self, user_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT user_id, account, display_name, role, status, daily_limit,
                       email_verified_at, created_at, last_login_at
                FROM users WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if not row:
            raise ResourceNotFoundError(user_id)
        return self._user_payload(row)

    def get_user_by_account(self, account: str) -> dict[str, Any]:
        normalized = normalize_account(account)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT user_id, account, display_name, role, status, daily_limit,
                       email_verified_at, created_at, last_login_at
                FROM users WHERE account = ?
                """,
                (normalized,),
            ).fetchone()
        if not row:
            raise ResourceNotFoundError(normalized)
        return self._user_payload(row)

    def create_session(self, user_id: str, ttl_hours: int) -> tuple[str, str]:
        token = "kb_session_" + secrets.token_urlsafe(32)
        session_id = "ses_" + uuid4().hex
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        expires_at = (now_dt + timedelta(hours=ttl_hours)).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions(session_id, user_id, token_hash, created_at, expires_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, user_id, hash_token(token), now_dt.isoformat(), expires_at, now_dt.isoformat()),
            )
            connection.commit()
        return session_id, token

    def authenticate_session(self, token: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.session_id, s.expires_at, u.user_id, u.account, u.display_name,
                       u.role, u.status, u.daily_limit, u.email_verified_at
                FROM sessions s
                JOIN users u ON u.user_id = s.user_id
                WHERE s.token_hash = ? AND s.revoked_at IS NULL
                """,
                (hash_token(token),),
            ).fetchone()
            if not row or row["status"] != "active" or _is_expired(row["expires_at"]):
                return None
            connection.execute(
                "UPDATE sessions SET last_used_at = ? WHERE session_id = ?",
                (utc_now(), row["session_id"]),
            )
            connection.commit()
        payload = self._user_payload(row)
        payload["session_id"] = row["session_id"]
        return payload

    def revoke_session(self, token: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (utc_now(), hash_token(token)),
            )
            connection.commit()

    def change_password(self, user_id: str, current_password: str, new_password: str) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT password_hash FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if not row or not verify_password(current_password, row["password_hash"]):
                raise InvalidCredentialsError("current password is invalid")
            connection.execute(
                "UPDATE users SET password_hash = ? WHERE user_id = ?",
                (hash_password(new_password), user_id),
            )
            connection.execute(
                "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (utc_now(), user_id),
            )
            connection.commit()

    def create_invitation(
        self,
        created_by: str | None,
        label: str,
        max_uses: int = 1,
        expires_in_days: int | None = 30,
        code: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        plain_code = code or self._generate_invite_code()
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        expires_at = (now_dt + timedelta(days=expires_in_days)).isoformat() if expires_in_days else None
        invitation_id = "inv_" + uuid4().hex
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO invitations(
                    invitation_id, code_hash, code_prefix, label, max_uses, used_count,
                    enabled, expires_at, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, 0, 1, ?, ?, ?)
                """,
                (
                    invitation_id,
                    hash_token(plain_code),
                    plain_code[:7],
                    label.strip(),
                    max_uses,
                    expires_at,
                    created_by,
                    now_dt.isoformat(),
                ),
            )
            connection.commit()
        return self.get_invitation(invitation_id), plain_code

    def get_invitation(self, invitation_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM invitations WHERE invitation_id = ?", (invitation_id,)).fetchone()
        if not row:
            raise ResourceNotFoundError(invitation_id)
        return self._invitation_payload(row)

    def list_invitations(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM invitations ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._invitation_payload(row) for row in rows]

    def create_api_key(self, user_id: str, name: str) -> tuple[dict[str, Any], str]:
        plain_key = "kb_live_" + secrets.token_urlsafe(32)
        api_key_id = "key_" + uuid4().hex
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_api_keys(api_key_id, user_id, name, key_prefix, key_hash, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (api_key_id, user_id, name.strip(), plain_key[:16], hash_token(plain_key), now),
            )
            connection.commit()
        return self.get_api_key(user_id, api_key_id), plain_key

    def list_api_keys(self, user_id: str, *, include_revoked: bool = False) -> list[dict[str, Any]]:
        status_filter = "" if include_revoked else "AND enabled = 1 AND revoked_at IS NULL"
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT api_key_id, name, key_prefix, enabled, created_at, last_used_at, revoked_at
                FROM user_api_keys
                WHERE user_id = ? {status_filter}
                ORDER BY created_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_api_key(self, user_id: str, api_key_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT api_key_id, name, key_prefix, enabled, created_at, last_used_at, revoked_at
                FROM user_api_keys WHERE user_id = ? AND api_key_id = ?
                """,
                (user_id, api_key_id),
            ).fetchone()
        if not row:
            raise ResourceNotFoundError(api_key_id)
        return dict(row)

    def authenticate_api_key(self, api_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT k.api_key_id, u.user_id, u.account, u.display_name, u.role,
                       u.status, u.daily_limit, u.email_verified_at
                FROM user_api_keys k
                JOIN users u ON u.user_id = k.user_id
                WHERE k.key_hash = ? AND k.enabled = 1 AND k.revoked_at IS NULL
                """,
                (hash_token(api_key),),
            ).fetchone()
            if not row or row["status"] != "active":
                return None
            connection.execute(
                "UPDATE user_api_keys SET last_used_at = ? WHERE api_key_id = ?",
                (utc_now(), row["api_key_id"]),
            )
            connection.commit()
        return dict(row)

    def revoke_api_key(self, user_id: str, api_key_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE user_api_keys SET enabled = 0, revoked_at = ?
                WHERE user_id = ? AND api_key_id = ? AND revoked_at IS NULL
                """,
                (utc_now(), user_id, api_key_id),
            )
            exists = cursor.rowcount > 0
            if not exists:
                exists = connection.execute(
                    "SELECT 1 FROM user_api_keys WHERE user_id = ? AND api_key_id = ?",
                    (user_id, api_key_id),
                ).fetchone() is not None
            connection.commit()
        if not exists:
            raise ResourceNotFoundError(api_key_id)

    def ensure_conversation(self, user_id: str, conversation_id: str | None, question: str) -> str:
        now = utc_now()
        title = " ".join(question.strip().split())[:42] or "新对话"
        with self._connect() as connection:
            if conversation_id:
                row = connection.execute(
                    "SELECT user_id, deleted_at FROM conversations WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                if row:
                    if row["deleted_at"]:
                        raise ResourceNotFoundError(conversation_id)
                    if row["user_id"] != user_id:
                        raise PermissionDeniedError(conversation_id)
                    return conversation_id
                if len(conversation_id) > 128:
                    raise ResourceNotFoundError(conversation_id)
            else:
                conversation_id = "conv_" + uuid4().hex
            connection.execute(
                """
                INSERT INTO conversations(conversation_id, user_id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (conversation_id, user_id, title, now, now),
            )
            connection.commit()
        return conversation_id

    def save_exchange(
        self,
        user_id: str,
        conversation_id: str,
        request_id: str,
        question: str,
        answer: str,
        response_metadata: dict[str, Any],
    ) -> None:
        now = utc_now()
        with self._connect() as connection:
            owner = connection.execute(
                "SELECT user_id FROM conversations WHERE conversation_id = ? AND deleted_at IS NULL",
                (conversation_id,),
            ).fetchone()
            if not owner or owner["user_id"] != user_id:
                raise PermissionDeniedError(conversation_id)
            connection.execute(
                """
                INSERT INTO messages(message_id, conversation_id, role, content, request_id, metadata_json, created_at)
                VALUES (?, ?, 'user', ?, ?, '{}', ?)
                """,
                ("msg_" + uuid4().hex, conversation_id, question, request_id, now),
            )
            connection.execute(
                """
                INSERT INTO messages(message_id, conversation_id, role, content, request_id, metadata_json, created_at)
                VALUES (?, ?, 'assistant', ?, ?, ?, ?)
                """,
                (
                    "msg_" + uuid4().hex,
                    conversation_id,
                    answer,
                    request_id,
                    json.dumps(response_metadata, ensure_ascii=False),
                    now,
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                (now, conversation_id),
            )
            connection.commit()

    def latest_user_question(self, user_id: str, conversation_id: str) -> str | None:
        with self._connect() as connection:
            owner = connection.execute(
                "SELECT user_id FROM conversations WHERE conversation_id = ? AND deleted_at IS NULL",
                (conversation_id,),
            ).fetchone()
            if not owner:
                raise ResourceNotFoundError(conversation_id)
            if owner["user_id"] != user_id:
                raise PermissionDeniedError(conversation_id)
            row = connection.execute(
                """
                SELECT content FROM messages
                WHERE conversation_id = ? AND role = 'user'
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return str(row["content"]) if row else None

    def recent_user_questions(
        self,
        user_id: str,
        conversation_id: str,
        limit: int = 4,
    ) -> list[str]:
        with self._connect() as connection:
            owner = connection.execute(
                "SELECT user_id FROM conversations WHERE conversation_id = ? AND deleted_at IS NULL",
                (conversation_id,),
            ).fetchone()
            if not owner:
                raise ResourceNotFoundError(conversation_id)
            if owner["user_id"] != user_id:
                raise PermissionDeniedError(conversation_id)
            rows = connection.execute(
                """
                SELECT content FROM messages
                WHERE conversation_id = ? AND role = 'user'
                ORDER BY created_at DESC, rowid DESC LIMIT ?
                """,
                (conversation_id, max(1, min(int(limit), 8))),
            ).fetchall()
        return [str(row["content"]) for row in reversed(rows)]

    def create_feedback(
        self,
        user_id: str | None,
        api_key_id: str | None,
        conversation_id: str | None,
        request_id: str | None,
        rating: str,
        reason: str | None,
        comment: str | None,
        question: str | None,
    ) -> dict[str, Any]:
        now = utc_now()
        if rating == "satisfied":
            review_lane = "no_action"
            feedback_status = "closed"
        elif reason in {"wrong_standard", "wrong_clause", "missing_evidence"}:
            review_lane = "kb_review"
            feedback_status = "open"
        elif reason in {"quote_too_long", "answer_too_vague", "format_issue"}:
            review_lane = "product"
            feedback_status = "open"
        else:
            review_lane = "manual_review"
            feedback_status = "open"

        with self._connect() as connection:
            if user_id and not connection.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone():
                user_id = None
            if api_key_id and not connection.execute(
                "SELECT 1 FROM user_api_keys WHERE api_key_id = ?", (api_key_id,)
            ).fetchone():
                api_key_id = None
            if conversation_id and not connection.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = ?", (conversation_id,)
            ).fetchone():
                conversation_id = None
            resolved_question = (question or "").strip() or None
            if not resolved_question and request_id:
                row = connection.execute(
                    """
                    SELECT content FROM messages
                    WHERE request_id = ? AND role = 'user'
                    ORDER BY created_at DESC, rowid DESC LIMIT 1
                    """,
                    (request_id,),
                ).fetchone()
                resolved_question = str(row["content"]) if row else None
            if not resolved_question and user_id and conversation_id:
                row = connection.execute(
                    """
                    SELECT m.content FROM messages m
                    JOIN conversations c ON c.conversation_id = m.conversation_id
                    WHERE m.conversation_id = ? AND c.user_id = ? AND m.role = 'user'
                    ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1
                    """,
                    (conversation_id, user_id),
                ).fetchone()
                resolved_question = str(row["content"]) if row else None

            feedback_id = "fb_" + uuid4().hex
            connection.execute(
                """
                INSERT INTO answer_feedback(
                    feedback_id, user_id, api_key_id, conversation_id, request_id,
                    rating, reason, comment, question, review_lane, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback_id,
                    user_id,
                    api_key_id,
                    conversation_id,
                    request_id,
                    rating,
                    reason,
                    comment,
                    resolved_question,
                    review_lane,
                    feedback_status,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM answer_feedback WHERE feedback_id = ?",
                (feedback_id,),
            ).fetchone()
            connection.commit()
        return dict(row)

    def list_feedback(
        self,
        status_filter: str | None = None,
        review_lane: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if status_filter:
            where.append("f.status = ?")
            params.append(status_filter)
        if review_lane:
            where.append("f.review_lane = ?")
            params.append(review_lane)
        clause = "WHERE " + " AND ".join(where) if where else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT f.*, u.account, u.display_name
                FROM answer_feedback f
                LEFT JOIN users u ON u.user_id = f.user_id
                {clause}
                ORDER BY CASE WHEN f.status IN ('open', 'in_progress', 'kb_review') THEN 0 ELSE 1 END,
                         f.created_at DESC
                LIMIT ?
                """,
                [*params, max(1, min(limit, 500))],
            ).fetchall()
        return [dict(row) for row in rows]

    def update_feedback_status(
        self,
        feedback_id: str,
        feedback_status: str,
        resolution_note: str | None,
        admin_user_id: str,
    ) -> dict[str, Any]:
        allowed = {"open", "in_progress", "kb_review", "resolved", "dismissed", "closed"}
        if feedback_status not in allowed:
            raise ValueError(feedback_status)
        now = utc_now()
        resolved_by = admin_user_id if feedback_status in {"resolved", "dismissed", "closed"} else None
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE answer_feedback
                SET status = ?, resolution_note = ?, resolved_by = ?, updated_at = ?
                WHERE feedback_id = ?
                """,
                (feedback_status, resolution_note, resolved_by, now, feedback_id),
            )
            if cursor.rowcount == 0:
                connection.rollback()
                raise ResourceNotFoundError(feedback_id)
            row = connection.execute(
                "SELECT * FROM answer_feedback WHERE feedback_id = ?",
                (feedback_id,),
            ).fetchone()
            connection.commit()
        return dict(row)

    def list_conversations(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT conversation_id, title, created_at, updated_at
                FROM conversations
                WHERE user_id = ? AND deleted_at IS NULL
                ORDER BY updated_at DESC LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_conversation(self, user_id: str, conversation_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            conversation = connection.execute(
                """
                SELECT conversation_id, title, created_at, updated_at
                FROM conversations
                WHERE conversation_id = ? AND user_id = ? AND deleted_at IS NULL
                """,
                (conversation_id, user_id),
            ).fetchone()
            if not conversation:
                raise ResourceNotFoundError(conversation_id)
            messages = connection.execute(
                """
                SELECT message_id, role, content, request_id, metadata_json, created_at
                FROM messages WHERE conversation_id = ? ORDER BY created_at, rowid
                """,
                (conversation_id,),
            ).fetchall()
        payload = dict(conversation)
        payload["messages"] = [
            {**dict(row), "metadata": json.loads(row["metadata_json"] or "{}")} for row in messages
        ]
        for message in payload["messages"]:
            message.pop("metadata_json", None)
        return payload

    def delete_conversation(self, user_id: str, conversation_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE conversations SET deleted_at = ?, updated_at = ?
                WHERE conversation_id = ? AND user_id = ? AND deleted_at IS NULL
                """,
                (utc_now(), utc_now(), conversation_id, user_id),
            )
            connection.commit()
        if cursor.rowcount == 0:
            raise ResourceNotFoundError(conversation_id)

    def research_upgrade_quota_cost(
        self,
        user_id: str,
        source_request_id: str | None,
        conversation_id: str,
        question: str,
    ) -> int:
        if not source_request_id:
            return 3
        normalized_question = " ".join(question.split())
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT q.request_id, q.request_mode, q.quota_consumed_units,
                       q.conversation_id, m.content AS question
                FROM qa_requests q
                LEFT JOIN messages m
                  ON m.request_id = q.request_id AND m.role = 'user'
                WHERE q.request_id = ? AND q.user_id = ?
                ORDER BY m.created_at DESC, m.rowid DESC
                LIMIT 1
                """,
                (source_request_id, user_id),
            ).fetchone()
        if not row:
            return 3
        stored_question = " ".join(str(row["question"] or "").split())
        if (
            row["request_mode"] == "basic"
            and int(row["quota_consumed_units"] or 0) == 1
            and row["conversation_id"] == conversation_id
            and stored_question == normalized_question
        ):
            return 2
        return 3

    def create_research_task(
        self,
        *,
        task_id: str,
        request_id: str,
        user_id: str,
        api_key_id: str | None,
        conversation_id: str,
        channel: str,
        question: str,
        retrieval_question: str,
        filters: dict[str, Any],
        reserved_quota_units: int,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT task_id FROM research_tasks
                WHERE user_id = ? AND status IN ('queued', 'planning', 'retrieving', 'analyzing')
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if active:
                connection.commit()
                raise ActiveResearchTaskError(active["task_id"])
            try:
                connection.execute(
                    """
                    INSERT INTO research_tasks(
                        task_id, request_id, user_id, api_key_id, conversation_id, channel,
                        question, retrieval_question, filters_json, status, stage,
                        progress_percent, status_message, quota_cost, reserved_quota_units, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', 0, ?, 3, ?, ?)
                    """,
                    (
                        task_id,
                        request_id,
                        user_id,
                        api_key_id,
                        conversation_id,
                        channel,
                        question,
                        retrieval_question,
                        json.dumps(filters, ensure_ascii=False),
                        "任务已进入队列。",
                        reserved_quota_units,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                active = connection.execute(
                    """
                    SELECT task_id FROM research_tasks
                    WHERE user_id = ? AND status IN ('queued', 'planning', 'retrieving', 'analyzing')
                    LIMIT 1
                    """,
                    (user_id,),
                ).fetchone()
                connection.rollback()
                raise ActiveResearchTaskError(active["task_id"] if active else None) from error
            connection.commit()
        return self.get_research_task(user_id, task_id)

    def get_research_task(self, user_id: str, task_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM research_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if not row:
            raise ResourceNotFoundError(task_id)
        if row["user_id"] != user_id:
            raise PermissionDeniedError(task_id)
        return self._research_task_payload(row)

    def get_research_task_internal(self, task_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM research_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if not row:
            raise ResourceNotFoundError(task_id)
        return self._research_task_payload(row)

    def update_research_task(
        self,
        task_id: str,
        *,
        status: str,
        percent: int,
        message: str,
        total_documents: int | None = None,
        examined_documents: int | None = None,
        evidence_documents: int | None = None,
        plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        fields = [
            "status = ?",
            "stage = ?",
            "progress_percent = ?",
            "status_message = ?",
            "started_at = COALESCE(started_at, ?)",
        ]
        values: list[Any] = [status, status, max(0, min(100, percent)), message, now]
        for column, value in (
            ("total_documents", total_documents),
            ("examined_documents", examined_documents),
            ("evidence_documents", evidence_documents),
        ):
            if value is not None:
                fields.append(f"{column} = ?")
                values.append(max(0, int(value)))
        if plan is not None:
            fields.append("plan_json = ?")
            values.append(json.dumps(plan, ensure_ascii=False))
        values.append(task_id)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE research_tasks SET {', '.join(fields)} WHERE task_id = ?",
                values,
            )
            connection.commit()
        if cursor.rowcount == 0:
            raise ResourceNotFoundError(task_id)
        return self.get_research_task_internal(task_id)

    def complete_research_task(
        self,
        task_id: str,
        status: str,
        result: dict[str, Any] | None,
        *,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        percent = 100 if status in {"completed", "partial", "insufficient_evidence"} else 0
        message = {
            "completed": "深度研究已完成。",
            "partial": "深度研究已完成，但部分候选文件缺少直接证据或未完成覆盖。",
            "insufficient_evidence": "深度研究未获得足够的直接条款证据。",
            "failed": "深度研究因系统错误失败，预留次数已退回。",
            "cancelled": "深度研究已取消。",
        }.get(status, "深度研究任务已结束。")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE research_tasks
                SET status = ?, stage = ?, progress_percent = ?, result_json = ?,
                    status_message = ?, error_code = ?, finished_at = ?
                WHERE task_id = ?
                """,
                (
                    status,
                    status,
                    percent,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    message,
                    error_code,
                    now,
                    task_id,
                ),
            )
            connection.commit()
        if cursor.rowcount == 0:
            raise ResourceNotFoundError(task_id)
        return self.get_research_task_internal(task_id)

    def cancel_queued_research_task(self, user_id: str, task_id: str) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM research_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if not row:
                connection.commit()
                raise ResourceNotFoundError(task_id)
            if row["user_id"] != user_id:
                connection.commit()
                raise PermissionDeniedError(task_id)
            if row["status"] != "queued":
                connection.commit()
                raise PermissionDeniedError("only queued research tasks can be cancelled")
            connection.execute(
                """
                UPDATE research_tasks
                SET status = 'cancelled', stage = 'cancelled', status_message = ?,
                    finished_at = ?
                WHERE task_id = ?
                """,
                ("任务已在排队阶段取消。", now, task_id),
            )
            connection.commit()
        return self.get_research_task(user_id, task_id)

    def recover_research_tasks(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT task_id FROM research_tasks
                WHERE status IN ('queued', 'planning', 'retrieving', 'analyzing')
                ORDER BY created_at
                """
            ).fetchall()
            task_ids = [str(row["task_id"]) for row in rows]
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                connection.execute(
                    f"""
                    UPDATE research_tasks
                    SET status = 'queued', stage = 'queued', progress_percent = 0,
                        status_message = '服务重启后已恢复到任务队列。', started_at = NULL
                    WHERE task_id IN ({placeholders})
                    """,
                    task_ids,
                )
            connection.commit()
        return task_ids

    @staticmethod
    def _research_task_payload(row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        for field in ("filters_json", "plan_json", "result_json"):
            raw = payload.pop(field, None)
            key = field.removesuffix("_json")
            try:
                payload[key] = json.loads(raw) if raw else (None if field == "result_json" else {})
            except json.JSONDecodeError:
                payload[key] = None if field == "result_json" else {}
        return payload

    def reserve_qa_quota(
        self,
        user_id: str,
        request_id: str,
        channel: str,
        api_key_id: str | None,
        conversation_id: str | None,
        question_chars: int,
        timezone_name: str,
        quota_units: int = 1,
        request_mode: str = "basic",
        parent_request_id: str | None = None,
    ) -> dict[str, Any]:
        quota_units = max(1, int(quota_units))
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        current_date = usage_date(timezone_name, now_dt)
        stale_cutoff = (now_dt - timedelta(minutes=10)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._release_stale_reservations(connection, user_id, current_date, stale_cutoff, now_dt.isoformat())
            self._ensure_daily_usage(connection, user_id, current_date, now_dt.isoformat())
            snapshot = self._quota_snapshot(connection, user_id, current_date)
            if snapshot["remaining"] < quota_units:
                connection.commit()
                raise DailyQuotaExceededError(snapshot)
            connection.execute(
                """
                UPDATE daily_usage SET reserved_count = reserved_count + ?, updated_at = ?
                WHERE user_id = ? AND usage_date = ?
                """,
                (quota_units, now_dt.isoformat(), user_id, current_date),
            )
            connection.execute(
                """
                INSERT INTO qa_requests(
                    request_id, user_id, api_key_id, conversation_id, channel, status,
                    quota_date, quota_consumed, quota_units, quota_consumed_units,
                    request_mode, parent_request_id, question_chars, created_at
                ) VALUES (?, ?, ?, ?, ?, 'processing', ?, 0, ?, 0, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    user_id,
                    api_key_id,
                    conversation_id,
                    channel,
                    current_date,
                    quota_units,
                    request_mode,
                    parent_request_id,
                    question_chars,
                    now_dt.isoformat(),
                ),
            )
            connection.commit()
        snapshot = self.quota_snapshot(user_id, timezone_name)
        snapshot["consumed"] = False
        snapshot["consumed_units"] = 0
        return snapshot

    def settle_qa_quota(
        self,
        request_id: str,
        status: str,
        answer_chars: int,
        timezone_name: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            request = connection.execute("SELECT * FROM qa_requests WHERE request_id = ?", (request_id,)).fetchone()
            if not request:
                raise ResourceNotFoundError(request_id)
            if request["status"] != "processing":
                snapshot = self._quota_snapshot(connection, request["user_id"], request["quota_date"])
                snapshot["consumed"] = bool(request["quota_consumed"])
                snapshot["consumed_units"] = int(request["quota_consumed_units"] or 0)
                connection.commit()
                return snapshot

            consume = status not in {"system_error", "out_of_scope"}
            reserved_units = max(1, int(request["quota_units"] or 1))
            consumed_units = reserved_units if consume else 0
            self._ensure_daily_usage(connection, request["user_id"], request["quota_date"], now)
            connection.execute(
                """
                UPDATE daily_usage
                SET reserved_count = MAX(0, reserved_count - ?),
                    used_count = used_count + ?, updated_at = ?
                WHERE user_id = ? AND usage_date = ?
                """,
                (
                    reserved_units,
                    consumed_units,
                    now,
                    request["user_id"],
                    request["quota_date"],
                ),
            )
            connection.execute(
                """
                UPDATE qa_requests
                SET status = ?, quota_consumed = ?, quota_consumed_units = ?,
                    answer_chars = ?, finished_at = ?
                WHERE request_id = ?
                """,
                (status, 1 if consume else 0, consumed_units, answer_chars, now, request_id),
            )
            snapshot = self._quota_snapshot(connection, request["user_id"], request["quota_date"])
            snapshot["consumed"] = consume
            snapshot["consumed_units"] = consumed_units
            connection.commit()
        return snapshot

    def fail_qa_quota(self, request_id: str, timezone_name: str) -> None:
        try:
            self.settle_qa_quota(request_id, "system_error", 0, timezone_name)
        except ResourceNotFoundError:
            return

    def quota_snapshot(self, user_id: str, timezone_name: str, target_date: str | None = None) -> dict[str, Any]:
        current_date = target_date or usage_date(timezone_name)
        with self._connect() as connection:
            self._ensure_daily_usage(connection, user_id, current_date, utc_now())
            snapshot = self._quota_snapshot(connection, user_id, current_date)
            connection.commit()
        return snapshot

    def account_summary(self, user_id: str, timezone_name: str, adjustment_limit: int = 20) -> dict[str, Any]:
        current_date = usage_date(timezone_name)
        with self._connect() as connection:
            self._ensure_daily_usage(connection, user_id, current_date, utc_now())
            quota = self._quota_snapshot(connection, user_id, current_date)
            totals = connection.execute(
                """
                SELECT COUNT(*) AS total_calls,
                       COALESCE(SUM(quota_consumed), 0) AS consumed_calls,
                       COALESCE(SUM(quota_consumed_units), 0) AS consumed_units
                FROM qa_requests WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            adjustments = connection.execute(
                """
                SELECT adjustment_id, adjustment_type, usage_date, delta_count,
                       previous_limit, new_limit, reason, admin_user_id, created_at
                FROM quota_adjustments WHERE user_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT ?
                """,
                (user_id, adjustment_limit),
            ).fetchall()
            connection.commit()
        return {
            "quota": quota,
            "total_calls": int(totals["total_calls"] or 0),
            "consumed_calls": int(totals["consumed_calls"] or 0),
            "consumed_units": int(totals["consumed_units"] or 0),
            "adjustments": [dict(row) for row in adjustments],
        }

    def list_users(self, timezone_name: str, limit: int = 200) -> list[dict[str, Any]]:
        current_date = usage_date(timezone_name)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT u.user_id, u.account, u.display_name, u.role, u.status,
                       u.daily_limit, u.email_verified_at, u.created_at, u.last_login_at,
                       COUNT(q.request_id) AS total_calls
                FROM users u
                LEFT JOIN qa_requests q ON q.user_id = u.user_id
                GROUP BY u.user_id
                ORDER BY u.created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            payload = []
            for row in rows:
                self._ensure_daily_usage(connection, row["user_id"], current_date, utc_now())
                item = self._user_payload(row)
                item["total_calls"] = int(row["total_calls"] or 0)
                item["quota"] = self._quota_snapshot(connection, row["user_id"], current_date)
                payload.append(item)
            connection.commit()
        return payload

    def set_daily_limit(
        self,
        user_id: str,
        daily_limit: int,
        reason: str,
        admin_user_id: str,
        timezone_name: str,
    ) -> dict[str, Any]:
        if daily_limit < 1:
            raise ValueError("daily limit must be positive")
        current_date = usage_date(timezone_name)
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = connection.execute("SELECT daily_limit FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if not user:
                raise ResourceNotFoundError(user_id)
            connection.execute("UPDATE users SET daily_limit = ? WHERE user_id = ?", (daily_limit, user_id))
            connection.execute(
                """
                INSERT INTO quota_adjustments(
                    adjustment_id, user_id, adjustment_type, usage_date, delta_count,
                    previous_limit, new_limit, reason, admin_user_id, created_at
                ) VALUES (?, ?, 'daily_limit_change', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "qadj_" + uuid4().hex,
                    user_id,
                    current_date,
                    daily_limit - int(user["daily_limit"]),
                    int(user["daily_limit"]),
                    daily_limit,
                    reason.strip(),
                    admin_user_id,
                    now,
                ),
            )
            connection.commit()
        return self.get_user(user_id)

    def adjust_daily_quota(
        self,
        user_id: str,
        delta_count: int,
        reason: str,
        admin_user_id: str,
        timezone_name: str,
        target_date: str | None = None,
    ) -> dict[str, Any]:
        current_date = target_date or usage_date(timezone_name)
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = connection.execute("SELECT daily_limit FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if not user:
                raise ResourceNotFoundError(user_id)
            self._ensure_daily_usage(connection, user_id, current_date, now)
            current = connection.execute(
                "SELECT used_count, reserved_count, bonus_count FROM daily_usage WHERE user_id = ? AND usage_date = ?",
                (user_id, current_date),
            ).fetchone()
            new_bonus = int(current["bonus_count"]) + delta_count
            effective = int(user["daily_limit"]) + new_bonus
            if effective < int(current["used_count"]) + int(current["reserved_count"]):
                raise DailyQuotaExceededError(self._quota_snapshot(connection, user_id, current_date))
            connection.execute(
                "UPDATE daily_usage SET bonus_count = ?, updated_at = ? WHERE user_id = ? AND usage_date = ?",
                (new_bonus, now, user_id, current_date),
            )
            connection.execute(
                """
                INSERT INTO quota_adjustments(
                    adjustment_id, user_id, adjustment_type, usage_date, delta_count,
                    previous_limit, new_limit, reason, admin_user_id, created_at
                ) VALUES (?, ?, 'daily_bonus', ?, ?, NULL, NULL, ?, ?, ?)
                """,
                (
                    "qadj_" + uuid4().hex,
                    user_id,
                    current_date,
                    delta_count,
                    reason.strip(),
                    admin_user_id,
                    now,
                ),
            )
            snapshot = self._quota_snapshot(connection, user_id, current_date)
            connection.commit()
        return snapshot

    def set_user_status(self, user_id: str, status: str) -> dict[str, Any]:
        with self._connect() as connection:
            cursor = connection.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))
            if status != "active":
                connection.execute(
                    "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                    (utc_now(), user_id),
                )
            connection.commit()
        if cursor.rowcount == 0:
            raise ResourceNotFoundError(user_id)
        return self.get_user(user_id)

    def _ensure_daily_usage(
        self,
        connection: sqlite3.Connection,
        user_id: str,
        current_date: str,
        updated_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO daily_usage(user_id, usage_date, used_count, reserved_count, bonus_count, updated_at)
            VALUES (?, ?, 0, 0, 0, ?)
            """,
            (user_id, current_date, updated_at),
        )

    def _quota_snapshot(
        self,
        connection: sqlite3.Connection,
        user_id: str,
        current_date: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT u.daily_limit, d.used_count, d.reserved_count, d.bonus_count
            FROM users u
            LEFT JOIN daily_usage d ON d.user_id = u.user_id AND d.usage_date = ?
            WHERE u.user_id = ?
            """,
            (current_date, user_id),
        ).fetchone()
        if not row:
            raise ResourceNotFoundError(user_id)
        daily_limit = int(row["daily_limit"] or 0)
        used = int(row["used_count"] or 0)
        reserved = int(row["reserved_count"] or 0)
        bonus = int(row["bonus_count"] or 0)
        effective_limit = max(0, daily_limit + bonus)
        return {
            "date": current_date,
            "daily_limit": daily_limit,
            "bonus": bonus,
            "effective_limit": effective_limit,
            "used": used,
            "reserved": reserved,
            "remaining": max(0, effective_limit - used - reserved),
        }

    def _release_stale_reservations(
        self,
        connection: sqlite3.Connection,
        user_id: str,
        current_date: str,
        stale_cutoff: str,
        now: str,
    ) -> None:
        stale = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(quota_units), 0) AS reserved_units
            FROM qa_requests
            WHERE user_id = ? AND quota_date = ? AND status = 'processing'
              AND request_mode != 'deep' AND created_at < ?
            """,
            (user_id, current_date, stale_cutoff),
        ).fetchone()
        stale_count = int(stale["total"] or 0)
        if not stale_count:
            return
        stale_units = int(stale["reserved_units"] or stale_count)
        connection.execute(
            """
            UPDATE qa_requests
            SET status = 'system_error', quota_consumed = 0,
                quota_consumed_units = 0, finished_at = ?
            WHERE user_id = ? AND quota_date = ? AND status = 'processing'
              AND request_mode != 'deep' AND created_at < ?
            """,
            (now, user_id, current_date, stale_cutoff),
        )
        self._ensure_daily_usage(connection, user_id, current_date, now)
        connection.execute(
            """
            UPDATE daily_usage SET reserved_count = MAX(0, reserved_count - ?), updated_at = ?
            WHERE user_id = ? AND usage_date = ?
            """,
            (stale_units, now, user_id, current_date),
        )

    def _user_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "user_id": row["user_id"],
            "account": row["account"],
            "display_name": row["display_name"],
            "role": row["role"],
            "status": row["status"],
            "daily_limit": int(row["daily_limit"] if "daily_limit" in row.keys() else 10),
            "email_verified": bool(row["email_verified_at"] if "email_verified_at" in row.keys() else False),
            "email_verified_at": row["email_verified_at"] if "email_verified_at" in row.keys() else None,
            "created_at": row["created_at"] if "created_at" in row.keys() else None,
            "last_login_at": row["last_login_at"] if "last_login_at" in row.keys() else None,
        }

    def _invite_is_valid(self, row: sqlite3.Row | None) -> bool:
        return bool(
            row
            and row["enabled"]
            and row["used_count"] < row["max_uses"]
            and not _is_expired(row["expires_at"])
        )

    def _invitation_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload.pop("code_hash", None)
        return payload

    def _generate_invite_code(self) -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
        return "KB-" + "-".join(groups)
