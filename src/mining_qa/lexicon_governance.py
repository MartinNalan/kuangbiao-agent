from __future__ import annotations

import hashlib
import json
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import PROJECT_ROOT, Settings, get_settings
from .domain_lexicon import (
    base_domain_lexicon,
    lexicon_candidate_warnings,
    lexicon_match_summary,
    normalize_lexicon_entry,
    publish_runtime_lexicon,
)


LIST_FIELDS = (
    "aliases",
    "positive_expansions",
    "negative_terms",
    "evidence_required_patterns",
    "required_context_terms",
    "forbidden_context_terms",
)
CANDIDATE_LIST_FIELDS = (*LIST_FIELDS, "positive_examples", "negative_examples")
CANDIDATE_FINGERPRINT_FIELDS = (
    "user_expression",
    "canonical_term",
    "intent_label",
    "domain",
    *LIST_FIELDS,
    "match_type",
    "domain_gate_enabled",
    "intent_trigger_enabled",
    "priority",
    "risk_level",
)


class LexiconGovernanceError(RuntimeError):
    pass


class LexiconRecordNotFoundError(LexiconGovernanceError):
    pass


class LexiconReviewError(LexiconGovernanceError):
    pass


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def resolved_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _review_examples(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            " ".join(str(item or "").split())[:1000]
            for item in value
            if " ".join(str(item or "").split())
        )
    )[:20]


def candidate_fingerprint(payload: dict[str, Any]) -> str:
    normalized = normalize_lexicon_entry(
        {
            **payload,
            "lexicon_id": payload.get("target_lexicon_id") or payload.get("candidate_id") or "candidate",
            "status": "active",
        }
    )
    snapshot = {
        "target_lexicon_id": str(payload.get("target_lexicon_id") or ""),
        "status": str(payload.get("status") or "draft"),
        **{field: normalized[field] for field in CANDIDATE_FINGERPRINT_FIELDS},
        "positive_examples": _review_examples(payload.get("positive_examples")),
        "negative_examples": _review_examples(payload.get("negative_examples")),
    }
    serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class LexiconGovernanceStore:
    def __init__(self, db_path: Path, runtime_path: Path):
        self.db_path = db_path
        self.runtime_path = runtime_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS domain_lexicon_entries (
                    lexicon_id TEXT PRIMARY KEY,
                    user_expression TEXT NOT NULL,
                    canonical_term TEXT NOT NULL,
                    intent_label TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    positive_expansions_json TEXT NOT NULL DEFAULT '[]',
                    negative_terms_json TEXT NOT NULL DEFAULT '[]',
                    evidence_required_patterns_json TEXT NOT NULL DEFAULT '[]',
                    required_context_terms_json TEXT NOT NULL DEFAULT '[]',
                    forbidden_context_terms_json TEXT NOT NULL DEFAULT '[]',
                    match_type TEXT NOT NULL DEFAULT 'phrase',
                    domain_gate_enabled INTEGER NOT NULL DEFAULT 1,
                    intent_trigger_enabled INTEGER NOT NULL DEFAULT 1,
                    priority INTEGER NOT NULL DEFAULT 50,
                    risk_level TEXT NOT NULL DEFAULT 'medium',
                    status TEXT NOT NULL DEFAULT 'active',
                    origin TEXT NOT NULL DEFAULT 'builtin',
                    version INTEGER NOT NULL DEFAULT 1,
                    review_note TEXT,
                    reviewed_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS domain_lexicon_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    target_lexicon_id TEXT,
                    user_expression TEXT NOT NULL,
                    canonical_term TEXT NOT NULL,
                    intent_label TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    positive_expansions_json TEXT NOT NULL DEFAULT '[]',
                    negative_terms_json TEXT NOT NULL DEFAULT '[]',
                    evidence_required_patterns_json TEXT NOT NULL DEFAULT '[]',
                    required_context_terms_json TEXT NOT NULL DEFAULT '[]',
                    forbidden_context_terms_json TEXT NOT NULL DEFAULT '[]',
                    positive_examples_json TEXT NOT NULL DEFAULT '[]',
                    negative_examples_json TEXT NOT NULL DEFAULT '[]',
                    match_type TEXT NOT NULL DEFAULT 'phrase',
                    domain_gate_enabled INTEGER NOT NULL DEFAULT 0,
                    intent_trigger_enabled INTEGER NOT NULL DEFAULT 1,
                    priority INTEGER NOT NULL DEFAULT 50,
                    risk_level TEXT NOT NULL DEFAULT 'medium',
                    status TEXT NOT NULL DEFAULT 'draft',
                    source_type TEXT NOT NULL DEFAULT 'manual',
                    source_reference TEXT,
                    review_note TEXT,
                    created_by TEXT NOT NULL,
                    reviewed_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    last_preview_fingerprint TEXT,
                    last_preview_passed INTEGER NOT NULL DEFAULT 0,
                    last_previewed_at TEXT,
                    last_previewed_by TEXT
                );

                CREATE TABLE IF NOT EXISTS domain_lexicon_audit (
                    audit_id TEXT PRIMARY KEY,
                    lexicon_id TEXT,
                    candidate_id TEXT,
                    action TEXT NOT NULL,
                    actor_user_id TEXT NOT NULL,
                    note TEXT,
                    snapshot_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_lexicon_entries_status_priority
                    ON domain_lexicon_entries(status, priority DESC);
                CREATE INDEX IF NOT EXISTS idx_lexicon_candidates_status_created
                    ON domain_lexicon_candidates(status, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_lexicon_audit_created
                    ON domain_lexicon_audit(created_at DESC);
                """
            )
            self._ensure_column(connection, "domain_lexicon_candidates", "last_preview_fingerprint", "TEXT")
            self._ensure_column(
                connection,
                "domain_lexicon_candidates",
                "last_preview_passed",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(connection, "domain_lexicon_candidates", "last_previewed_at", "TEXT")
            self._ensure_column(connection, "domain_lexicon_candidates", "last_previewed_by", "TEXT")
            connection.execute(
                """
                UPDATE domain_lexicon_entries
                SET origin = 'admin_override'
                WHERE origin = 'builtin' AND reviewed_by IS NOT NULL
                """
            )
            self._seed_builtin_entries(connection)
            connection.commit()

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        existing = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _seed_builtin_entries(self, connection: sqlite3.Connection) -> None:
        now = utc_now()
        builtin_ids: list[str] = []
        for raw in base_domain_lexicon():
            entry = normalize_lexicon_entry(raw)
            builtin_ids.append(entry["lexicon_id"])
            values = self._entry_sql_values(entry, created_at=entry.get("created_at") or now)
            connection.execute(
                """
                INSERT INTO domain_lexicon_entries(
                    lexicon_id, user_expression, canonical_term, intent_label, domain,
                    aliases_json, positive_expansions_json, negative_terms_json,
                    evidence_required_patterns_json, required_context_terms_json,
                    forbidden_context_terms_json, match_type, domain_gate_enabled,
                    intent_trigger_enabled, priority, risk_level, status, origin,
                    version, review_note, reviewed_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lexicon_id) DO UPDATE SET
                    user_expression = excluded.user_expression,
                    canonical_term = excluded.canonical_term,
                    intent_label = excluded.intent_label,
                    domain = excluded.domain,
                    aliases_json = excluded.aliases_json,
                    positive_expansions_json = excluded.positive_expansions_json,
                    negative_terms_json = excluded.negative_terms_json,
                    evidence_required_patterns_json = excluded.evidence_required_patterns_json,
                    required_context_terms_json = excluded.required_context_terms_json,
                    forbidden_context_terms_json = excluded.forbidden_context_terms_json,
                    match_type = excluded.match_type,
                    domain_gate_enabled = excluded.domain_gate_enabled,
                    intent_trigger_enabled = excluded.intent_trigger_enabled,
                    priority = excluded.priority,
                    risk_level = excluded.risk_level,
                    status = excluded.status,
                    version = excluded.version,
                    review_note = NULL,
                    reviewed_by = NULL,
                    updated_at = excluded.updated_at
                WHERE domain_lexicon_entries.origin = 'builtin'
                """,
                values,
            )
        if builtin_ids:
            placeholders = ", ".join("?" for _ in builtin_ids)
            connection.execute(
                f"DELETE FROM domain_lexicon_entries WHERE origin = 'builtin' AND lexicon_id NOT IN ({placeholders})",
                builtin_ids,
            )
        else:
            connection.execute("DELETE FROM domain_lexicon_entries WHERE origin = 'builtin'")

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(value if isinstance(value, list) else [], ensure_ascii=False)

    def _entry_sql_values(
        self,
        entry: dict[str, Any],
        *,
        created_at: str,
        review_note: str | None = None,
        reviewed_by: str | None = None,
    ) -> tuple[Any, ...]:
        now = utc_now()
        return (
            entry["lexicon_id"],
            entry["user_expression"],
            entry["canonical_term"],
            entry["intent_label"],
            entry["domain"],
            self._json(entry["aliases"]),
            self._json(entry["positive_expansions"]),
            self._json(entry["negative_terms"]),
            self._json(entry["evidence_required_patterns"]),
            self._json(entry["required_context_terms"]),
            self._json(entry["forbidden_context_terms"]),
            entry["match_type"],
            int(entry["domain_gate_enabled"]),
            int(entry["intent_trigger_enabled"]),
            entry["priority"],
            entry["risk_level"],
            entry["status"],
            entry.get("origin") or "builtin",
            entry["version"],
            review_note,
            reviewed_by,
            created_at,
            now,
        )

    @staticmethod
    def _payload(row: sqlite3.Row, *, candidate: bool = False) -> dict[str, Any]:
        payload = dict(row)
        fields = CANDIDATE_LIST_FIELDS if candidate else LIST_FIELDS
        for field in fields:
            raw = payload.pop(f"{field}_json", "[]")
            try:
                payload[field] = json.loads(raw) if raw else []
            except json.JSONDecodeError:
                payload[field] = []
        for field in ("domain_gate_enabled", "intent_trigger_enabled"):
            payload[field] = bool(payload.get(field))
        if candidate:
            preview_fingerprint = payload.pop("last_preview_fingerprint", None)
            preview_passed = bool(payload.pop("last_preview_passed", 0))
            payload["preview_ready"] = bool(
                preview_passed
                and preview_fingerprint
                and preview_fingerprint == candidate_fingerprint(payload)
            )
        return payload

    def list_entries(
        self,
        *,
        status: str | None = None,
        query: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("status = ?")
            values.append(status)
        if query:
            clauses.append("(user_expression LIKE ? OR canonical_term LIKE ? OR intent_label LIKE ?)")
            pattern = f"%{query.strip()}%"
            values.extend((pattern, pattern, pattern))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(2000, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM domain_lexicon_entries
                {where}
                ORDER BY status = 'active' DESC, priority DESC, updated_at DESC, rowid DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
        return [self._payload(row) for row in rows]

    def list_candidates(
        self,
        *,
        status: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        values: list[Any] = []
        where = ""
        if status:
            where = "WHERE status = ?"
            values.append(status)
        values.append(max(1, min(2000, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM domain_lexicon_candidates
                {where}
                ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END,
                         updated_at DESC, rowid DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
        return [self._payload(row, candidate=True) for row in rows]

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM domain_lexicon_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        if not row:
            raise LexiconRecordNotFoundError(candidate_id)
        return self._payload(row, candidate=True)

    @staticmethod
    def _candidate_values(payload: dict[str, Any], *, candidate_id: str, actor_user_id: str) -> tuple[Any, ...]:
        entry = normalize_lexicon_entry(
            {
                **payload,
                "lexicon_id": payload.get("target_lexicon_id") or candidate_id,
                "status": "active",
            }
        )
        now = utc_now()
        return (
            candidate_id,
            payload.get("target_lexicon_id"),
            entry["user_expression"],
            entry["canonical_term"],
            entry["intent_label"],
            entry["domain"],
            json.dumps(entry["aliases"], ensure_ascii=False),
            json.dumps(entry["positive_expansions"], ensure_ascii=False),
            json.dumps(entry["negative_terms"], ensure_ascii=False),
            json.dumps(entry["evidence_required_patterns"], ensure_ascii=False),
            json.dumps(entry["required_context_terms"], ensure_ascii=False),
            json.dumps(entry["forbidden_context_terms"], ensure_ascii=False),
            json.dumps(payload.get("positive_examples") or [], ensure_ascii=False),
            json.dumps(payload.get("negative_examples") or [], ensure_ascii=False),
            entry["match_type"],
            int(entry["domain_gate_enabled"]),
            int(entry["intent_trigger_enabled"]),
            entry["priority"],
            entry["risk_level"],
            payload.get("status") or "draft",
            payload.get("source_type") or "manual",
            payload.get("source_reference"),
            payload.get("review_note"),
            actor_user_id,
            now,
            now,
        )

    def create_candidate(self, payload: dict[str, Any], actor_user_id: str) -> dict[str, Any]:
        candidate_id = "lexcand_" + uuid4().hex
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO domain_lexicon_candidates(
                    candidate_id, target_lexicon_id, user_expression, canonical_term,
                    intent_label, domain, aliases_json, positive_expansions_json,
                    negative_terms_json, evidence_required_patterns_json,
                    required_context_terms_json, forbidden_context_terms_json,
                    positive_examples_json, negative_examples_json, match_type,
                    domain_gate_enabled, intent_trigger_enabled, priority, risk_level,
                    status, source_type, source_reference, review_note, created_by,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._candidate_values(payload, candidate_id=candidate_id, actor_user_id=actor_user_id),
            )
            self._write_audit(
                connection,
                action="candidate_created",
                actor_user_id=actor_user_id,
                candidate_id=candidate_id,
                note=payload.get("review_note"),
                snapshot=payload,
            )
            connection.commit()
        return self.get_candidate(candidate_id)

    def update_candidate(
        self,
        candidate_id: str,
        payload: dict[str, Any],
        actor_user_id: str,
    ) -> dict[str, Any]:
        current = self.get_candidate(candidate_id)
        if current["status"] == "approved":
            raise LexiconReviewError("approved candidates cannot be edited")
        preview_changed = candidate_fingerprint(current) != candidate_fingerprint(payload)
        values = list(self._candidate_values(payload, candidate_id=candidate_id, actor_user_id=actor_user_id))
        values = values[1:23] + [utc_now(), candidate_id]
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE domain_lexicon_candidates SET
                    target_lexicon_id = ?, user_expression = ?, canonical_term = ?,
                    intent_label = ?, domain = ?, aliases_json = ?, positive_expansions_json = ?,
                    negative_terms_json = ?, evidence_required_patterns_json = ?,
                    required_context_terms_json = ?, forbidden_context_terms_json = ?,
                    positive_examples_json = ?, negative_examples_json = ?, match_type = ?,
                    domain_gate_enabled = ?, intent_trigger_enabled = ?, priority = ?,
                    risk_level = ?, status = ?, source_type = ?, source_reference = ?,
                    review_note = ?, updated_at = ?
                WHERE candidate_id = ?
                """,
                values,
            )
            if preview_changed:
                connection.execute(
                    """
                    UPDATE domain_lexicon_candidates
                    SET last_preview_fingerprint = NULL, last_preview_passed = 0,
                        last_previewed_at = NULL, last_previewed_by = NULL
                    WHERE candidate_id = ?
                    """,
                    (candidate_id,),
                )
            self._write_audit(
                connection,
                action="candidate_updated",
                actor_user_id=actor_user_id,
                candidate_id=candidate_id,
                note=payload.get("review_note"),
                snapshot=payload,
            )
            connection.commit()
        return self.get_candidate(candidate_id)

    @staticmethod
    def _example_matches_candidate(
        summary: dict[str, Any],
        candidate: dict[str, Any],
        target_id: str,
    ) -> bool:
        checks: list[bool] = []
        if candidate.get("domain_gate_enabled"):
            checks.append(any(item["lexicon_id"] == target_id for item in summary["domain_matches"]))
        if candidate.get("intent_trigger_enabled"):
            checks.append(any(item["lexicon_id"] == target_id for item in summary["intent_matches"]))
        if not checks:
            checks.append(any(item["lexicon_id"] == target_id for item in summary["retrieval_matches"]))
        return all(checks)

    def preview_candidate(
        self,
        query: str,
        payload: dict[str, Any],
        *,
        candidate_id: str | None = None,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        current_entries = self.list_entries(status="active", limit=2000)
        target_id = payload.get("target_lexicon_id") or "lex-preview"
        candidate = normalize_lexicon_entry(
            {
                **payload,
                "lexicon_id": target_id,
                "status": "active",
                "origin": "preview",
            }
        )
        proposed = [entry for entry in current_entries if entry["lexicon_id"] != target_id]
        proposed.append(candidate)
        warnings = lexicon_candidate_warnings(payload, current_entries)
        example_checks: list[dict[str, Any]] = []
        for kind, examples in (
            ("positive", _review_examples(payload.get("positive_examples"))),
            ("negative", _review_examples(payload.get("negative_examples"))),
        ):
            for example in examples:
                summary = lexicon_match_summary(example, proposed)
                matched = self._example_matches_candidate(summary, candidate, target_id)
                passed = matched if kind == "positive" else not matched
                example_checks.append(
                    {"kind": kind, "query": example, "passed": passed, "matched": matched}
                )
                if not passed:
                    warnings.append(
                        f"{'正向示例未命中' if kind == 'positive' else '反例仍会命中'}：{example}"
                    )
        verification_passed = bool(example_checks) and all(item["passed"] for item in example_checks)
        result = {
            "current": lexicon_match_summary(query, current_entries),
            "proposed": lexicon_match_summary(query, proposed),
            "warnings": list(dict.fromkeys(warnings)),
            "example_checks": example_checks,
            "verification_passed": verification_passed,
            "recorded": False,
        }
        if not candidate_id:
            return result

        stored = self.get_candidate(candidate_id)
        fingerprint = candidate_fingerprint(payload)
        if fingerprint != candidate_fingerprint(stored):
            raise LexiconReviewError("请先保存候选的最新修改，再执行上线前预览。")
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE domain_lexicon_candidates
                SET last_preview_fingerprint = ?, last_preview_passed = ?,
                    last_previewed_at = ?, last_previewed_by = ?
                WHERE candidate_id = ?
                """,
                (
                    fingerprint if verification_passed else None,
                    int(verification_passed),
                    now,
                    actor_user_id,
                    candidate_id,
                ),
            )
            self._write_audit(
                connection,
                action="candidate_previewed",
                actor_user_id=actor_user_id or "",
                candidate_id=candidate_id,
                note=query,
                snapshot={
                    "query": query,
                    "verification_passed": verification_passed,
                    "warnings": result["warnings"],
                    "example_checks": example_checks,
                },
            )
            connection.commit()
        result["recorded"] = True
        result["previewed_at"] = now
        return result

    def review_candidate(
        self,
        candidate_id: str,
        action: str,
        note: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        candidate = self.get_candidate(candidate_id)
        if candidate["status"] == "approved":
            raise LexiconReviewError("candidate is already approved")
        if action == "reject":
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE domain_lexicon_candidates
                    SET status = 'rejected', review_note = ?, reviewed_by = ?,
                        reviewed_at = ?, updated_at = ?
                    WHERE candidate_id = ?
                    """,
                    (note, actor_user_id, utc_now(), utc_now(), candidate_id),
                )
                self._write_audit(
                    connection,
                    action="candidate_rejected",
                    actor_user_id=actor_user_id,
                    candidate_id=candidate_id,
                    note=note,
                    snapshot=candidate,
                )
                connection.commit()
            return self.get_candidate(candidate_id)
        if action != "approve":
            raise LexiconReviewError("unsupported review action")
        if candidate["status"] != "pending":
            raise LexiconReviewError("候选必须先提交为待审核状态。")
        if not candidate["positive_examples"] or not candidate["negative_examples"]:
            raise LexiconReviewError("approval requires positive and negative examples")
        if not candidate.get("preview_ready"):
            raise LexiconReviewError("候选必须先通过最新正向示例和反例的上线前预览。")
        warnings = lexicon_candidate_warnings(candidate, self.list_entries(status="active", limit=2000))
        blocking = [
            warning
            for warning in warnings
            if "领域门控" in warning or "无运行作用" in warning
        ]
        if blocking:
            raise LexiconReviewError(blocking[0])

        target_id = candidate.get("target_lexicon_id") or ("lex-admin-" + uuid4().hex[:16])
        now = utc_now()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT created_at, version, origin FROM domain_lexicon_entries WHERE lexicon_id = ?",
                (target_id,),
            ).fetchone()
            entry = normalize_lexicon_entry(
                {
                    **candidate,
                    "lexicon_id": target_id,
                    "status": "active",
                    "origin": (
                        "admin_override"
                        if existing and existing["origin"] == "builtin"
                        else existing["origin"] if existing else "admin"
                    ),
                    "version": int(existing["version"] or 0) + 1 if existing else 1,
                }
            )
            values = self._entry_sql_values(
                entry,
                created_at=existing["created_at"] if existing else now,
                review_note=note,
                reviewed_by=actor_user_id,
            )
            connection.execute(
                """
                INSERT INTO domain_lexicon_entries(
                    lexicon_id, user_expression, canonical_term, intent_label, domain,
                    aliases_json, positive_expansions_json, negative_terms_json,
                    evidence_required_patterns_json, required_context_terms_json,
                    forbidden_context_terms_json, match_type, domain_gate_enabled,
                    intent_trigger_enabled, priority, risk_level, status, origin,
                    version, review_note, reviewed_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lexicon_id) DO UPDATE SET
                    user_expression = excluded.user_expression,
                    canonical_term = excluded.canonical_term,
                    intent_label = excluded.intent_label,
                    domain = excluded.domain,
                    aliases_json = excluded.aliases_json,
                    positive_expansions_json = excluded.positive_expansions_json,
                    negative_terms_json = excluded.negative_terms_json,
                    evidence_required_patterns_json = excluded.evidence_required_patterns_json,
                    required_context_terms_json = excluded.required_context_terms_json,
                    forbidden_context_terms_json = excluded.forbidden_context_terms_json,
                    match_type = excluded.match_type,
                    domain_gate_enabled = excluded.domain_gate_enabled,
                    intent_trigger_enabled = excluded.intent_trigger_enabled,
                    priority = excluded.priority,
                    risk_level = excluded.risk_level,
                    status = excluded.status,
                    origin = excluded.origin,
                    version = excluded.version,
                    review_note = excluded.review_note,
                    reviewed_by = excluded.reviewed_by,
                    updated_at = excluded.updated_at
                """,
                values,
            )
            connection.execute(
                """
                UPDATE domain_lexicon_candidates
                SET status = 'approved', target_lexicon_id = ?, review_note = ?,
                    reviewed_by = ?, reviewed_at = ?, updated_at = ?
                WHERE candidate_id = ?
                """,
                (target_id, note, actor_user_id, now, now, candidate_id),
            )
            self._write_audit(
                connection,
                action="candidate_approved",
                actor_user_id=actor_user_id,
                lexicon_id=target_id,
                candidate_id=candidate_id,
                note=note,
                snapshot=entry,
            )
            connection.commit()
        self.publish_runtime()
        return self.get_candidate(candidate_id)

    def set_entry_status(
        self,
        lexicon_id: str,
        status: str,
        note: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        if status not in {"active", "disabled"}:
            raise LexiconReviewError("unsupported entry status")
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM domain_lexicon_entries WHERE lexicon_id = ?",
                (lexicon_id,),
            ).fetchone()
            if not row:
                raise LexiconRecordNotFoundError(lexicon_id)
            connection.execute(
                """
                UPDATE domain_lexicon_entries
                SET status = ?, version = version + 1, review_note = ?, reviewed_by = ?,
                    origin = CASE WHEN origin = 'builtin' THEN 'admin_override' ELSE origin END,
                    updated_at = ?
                WHERE lexicon_id = ?
                """,
                (status, note, actor_user_id, now, lexicon_id),
            )
            snapshot = self._payload(row)
            snapshot["status"] = status
            self._write_audit(
                connection,
                action=f"entry_{status}",
                actor_user_id=actor_user_id,
                lexicon_id=lexicon_id,
                note=note,
                snapshot=snapshot,
            )
            connection.commit()
        self.publish_runtime()
        return next(entry for entry in self.list_entries(limit=2000) if entry["lexicon_id"] == lexicon_id)

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM domain_lexicon_audit
                ORDER BY created_at DESC, rowid DESC LIMIT ?
                """,
                (max(1, min(500, int(limit))),),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["snapshot"] = json.loads(item.pop("snapshot_json") or "{}")
            except json.JSONDecodeError:
                item["snapshot"] = {}
            items.append(item)
        return items

    def summary(self) -> dict[str, int]:
        with self._connect() as connection:
            entry_rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM domain_lexicon_entries GROUP BY status"
            ).fetchall()
            candidate_rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM domain_lexicon_candidates GROUP BY status"
            ).fetchall()
            high_risk = connection.execute(
                "SELECT COUNT(*) AS total FROM domain_lexicon_candidates WHERE risk_level = 'high' AND status IN ('draft', 'pending')"
            ).fetchone()
        entries = {row["status"]: int(row["total"]) for row in entry_rows}
        candidates = {row["status"]: int(row["total"]) for row in candidate_rows}
        return {
            "active_entries": entries.get("active", 0),
            "disabled_entries": entries.get("disabled", 0),
            "pending_candidates": candidates.get("pending", 0),
            "draft_candidates": candidates.get("draft", 0),
            "high_risk_candidates": int(high_risk["total"] if high_risk else 0),
        }

    def publish_runtime(self) -> Path:
        return publish_runtime_lexicon(self.list_entries(limit=2000), self.runtime_path)

    @staticmethod
    def _write_audit(
        connection: sqlite3.Connection,
        *,
        action: str,
        actor_user_id: str,
        snapshot: dict[str, Any],
        lexicon_id: str | None = None,
        candidate_id: str | None = None,
        note: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO domain_lexicon_audit(
                audit_id, lexicon_id, candidate_id, action, actor_user_id,
                note, snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "lexaudit_" + uuid4().hex,
                lexicon_id,
                candidate_id,
                action,
                actor_user_id,
                note,
                json.dumps(snapshot, ensure_ascii=False),
                utc_now(),
            ),
        )


def runtime_path_for_settings(settings: Settings) -> Path:
    configured = settings.domain_lexicon_runtime_path.strip()
    return resolved_path(configured) if configured else resolved_path(settings.app_db_path).parent / "domain_lexicon_runtime.json"


@lru_cache(maxsize=8)
def _store_for_paths(db_path: str, runtime_path: str) -> LexiconGovernanceStore:
    return LexiconGovernanceStore(Path(db_path), Path(runtime_path))


def get_lexicon_governance_store(settings: Settings | None = None) -> LexiconGovernanceStore:
    current = settings or get_settings()
    db_path = resolved_path(current.app_db_path)
    runtime_path = runtime_path_for_settings(current)
    return _store_for_paths(str(db_path), str(runtime_path))


def clear_lexicon_governance_cache() -> None:
    _store_for_paths.cache_clear()
