from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.v4_governance import (  # noqa: E402
    OfficialRecord,
    amendment_parent_title,
    choose_exact_record,
    effective_status_from_official,
    evidence_file_name,
    json_dumps,
    load_json_object,
    normalize_standard_no,
    normalize_title,
    official_query_url,
    parse_official_search,
    sha256_bytes,
    title_similarity,
)


DEFAULT_ROOT = PROJECT_ROOT / "data" / "knowledge_base_v4"
DEFAULT_DB = DEFAULT_ROOT / "db" / "corpus.sqlite"
V3_DB = PROJECT_ROOT / "data" / "knowledge_base" / "db" / "knowledge_base.sqlite"
USER_AGENT = "Mozilla/5.0 (compatible; OreExpert-KB-Governance/1.0)"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fetch(url: str, retries: int = 3, timeout: float = 30.0) -> bytes:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"official source request failed: {url}: {last_error}")


def fetch_cached(url: str, path: Path, *, refresh: bool) -> tuple[bytes, bool]:
    if path.exists() and path.stat().st_size > 0 and not refresh:
        return path.read_bytes(), True
    payload = fetch(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload, False


def artifact_id(path: Path) -> str:
    import hashlib

    return "artifact-status-" + hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]


def attach_status_evidence(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    path: Path,
    source_url: str,
    recorded_at: str,
) -> None:
    payload = path.read_bytes()
    identifier = artifact_id(path)
    conn.execute(
        """
        insert into source_artifacts(artifact_id,path,artifact_type,sha256,bytes,source_url,exists_on_disk,recorded_at)
        values (?, ?, 'official_status_html', ?, ?, ?, 1, ?)
        on conflict(path) do update set
          sha256=excluded.sha256,
          bytes=excluded.bytes,
          source_url=excluded.source_url,
          exists_on_disk=1,
          recorded_at=excluded.recorded_at
        """,
        (identifier, str(path.resolve()), sha256_bytes(payload), len(payload), source_url, recorded_at),
    )
    actual_id = conn.execute("select artifact_id from source_artifacts where path = ?", (str(path.resolve()),)).fetchone()[0]
    conn.execute(
        "insert or ignore into document_artifacts(document_id,artifact_id,artifact_role) values (?, ?, 'status_evidence')",
        (document_id, actual_id),
    )


def collect_coded_documents(
    conn: sqlite3.Connection,
    evidence_dir: Path,
    *,
    refresh: bool,
    sleep_seconds: float,
    checked_at: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows = conn.execute(
        """
        select * from documents
        where corpus='technical_standards' and standard_no is not null and trim(standard_no) != ''
        order by standard_no,title
        """
    ).fetchall()
    report_rows: list[dict[str, Any]] = []
    results: dict[str, dict[str, Any]] = {}
    for row in rows:
        document = dict(row)
        platform_key, query_url = official_query_url(document["standard_no"])
        evidence_path = evidence_dir / evidence_file_name(document["document_id"], platform_key)
        error: str | None = None
        records: list[OfficialRecord] = []
        cache_hit = False
        try:
            payload, cache_hit = fetch_cached(query_url, evidence_path, refresh=refresh)
            records = parse_official_search(platform_key, payload.decode("utf-8", errors="replace"), query_url)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        record = choose_exact_record(records, document["standard_no"], document["title"])
        if record is None:
            effective_status = "governance_conflict"
            official_status = "official_exact_record_not_found"
            review_status = "identity_or_status_review_required"
            evidence = f"官方检索未找到与 {document['standard_no']} 完全一致的记录。"
            official_url = query_url
            official_title = None
            official_standard_no = None
            similarity = 0.0
            platform = "自然资源标准化信息服务平台" if platform_key == "nrsis" else "全国标准信息公共服务平台"
        else:
            effective_status = record.effective_status
            official_status = record.official_status
            official_title = record.title
            official_standard_no = record.standard_no
            similarity = title_similarity(document["title"], record.title)
            platform = record.platform
            official_url = record.detail_url
            if similarity < 0.72:
                effective_status = "governance_conflict"
                review_status = "identity_or_status_review_required"
                evidence = (
                    f"官方记录标准号完全匹配，但标题相似度仅 {similarity:.3f}："
                    f"库内《{document['title']}》；官方《{record.title}》。"
                )
            elif effective_status == "current":
                review_status = (
                    document["review_status"]
                    if document["review_status"] == "approved_for_service"
                    else "status_verified_pending_quality_review"
                )
                evidence = f"官方平台记录 {record.standard_no}《{record.title}》状态为“{record.official_status}”。"
            elif effective_status == "repealed":
                review_status = "status_verified_repealed"
                evidence = f"官方平台记录 {record.standard_no}《{record.title}》状态为“{record.official_status}”。"
            else:
                review_status = "identity_or_status_review_required"
                evidence = f"官方平台状态“{record.official_status}”无法映射为现行或废止。"

        metadata = load_json_object(document["source_metadata_json"])
        metadata.setdefault("pre_t020_identity", {})
        metadata["pre_t020_identity"].update(
            {
                "title": document["title"],
                "standard_no": document["standard_no"],
                "status": document["status"],
                "effective_status": document["effective_status"],
                "source_url": document["source_url"],
                "source_platform": document["source_platform"],
            }
        )
        metadata["t020_governance"] = {
            "checked_at": checked_at,
            "identity_method": "exact_normalized_standard_number_then_title_cross_check",
            "status_source": platform,
            "status_evidence": evidence,
            "official_query_url": query_url,
            "official_url": official_url,
            "official_standard_no": official_standard_no,
            "official_title": official_title,
            "official_status": official_status,
            "title_similarity": similarity,
            "evidence_path": str(evidence_path.resolve()) if evidence_path.exists() else None,
            "evidence_sha256": sha256_bytes(evidence_path.read_bytes()) if evidence_path.exists() else None,
            "request_error": error,
            "cache_hit": cache_hit,
        }
        corrected_title = official_title if official_title and similarity >= 0.72 else document["title"]
        corrected_number = official_standard_no if official_standard_no else document["standard_no"]
        can_answer = int(effective_status == "current" and review_status == "approved_for_service")
        conn.execute(
            """
            update documents
            set title=?,standard_no=?,status=?,effective_status=?,review_status=?,can_answer=?,
                source_url=?,source_platform=?,source_metadata_json=?,updated_at=?
            where document_id=?
            """,
            (
                corrected_title,
                corrected_number,
                official_status,
                effective_status,
                review_status,
                can_answer,
                official_url,
                platform,
                json_dumps(metadata),
                checked_at,
                document["document_id"],
            ),
        )
        if evidence_path.exists():
            attach_status_evidence(
                conn,
                document_id=document["document_id"],
                path=evidence_path,
                source_url=query_url,
                recorded_at=checked_at,
            )
        result = {
            "document_id": document["document_id"],
            "standard_no_before": document["standard_no"],
            "standard_no": corrected_number,
            "title_before": document["title"],
            "title": corrected_title,
            "document_kind": "standard",
            "verification_method": "official_exact_standard_number",
            "identity_relation": None,
            "parent_document_id": None,
            "parent_standard_no": None,
            "effective_status_before": document["effective_status"],
            "effective_status": effective_status,
            "official_status": official_status,
            "status_source": platform,
            "status_evidence": evidence,
            "status_checked_at": checked_at,
            "official_url": official_url,
            "official_query_url": query_url,
            "evidence_path": str(evidence_path.resolve()) if evidence_path.exists() else None,
            "evidence_sha256": sha256_bytes(evidence_path.read_bytes()) if evidence_path.exists() else None,
            "title_similarity": similarity,
            "request_error": error,
            "can_answer": can_answer,
        }
        results[document["document_id"]] = result
        report_rows.append(result)
        if sleep_seconds and not cache_hit:
            time.sleep(sleep_seconds)
    return report_rows, results


def collect_uncoded_documents(
    conn: sqlite3.Connection,
    evidence_dir: Path,
    coded_results: dict[str, dict[str, Any]],
    *,
    checked_at: str,
) -> list[dict[str, Any]]:
    all_documents = [dict(row) for row in conn.execute("select * from documents where corpus='technical_standards'")]
    parents_by_title = {normalize_title(row["title"]): row for row in all_documents if row["standard_no"]}
    report_rows: list[dict[str, Any]] = []
    for document in sorted((row for row in all_documents if not row["standard_no"]), key=lambda row: row["title"]):
        parent_title = amendment_parent_title(document["title"])
        parent = parents_by_title.get(normalize_title(parent_title)) if parent_title else None
        metadata = load_json_object(document["source_metadata_json"])
        metadata.setdefault("pre_t020_identity", {})
        metadata["pre_t020_identity"].update(
            {
                "title": document["title"],
                "standard_no": document["standard_no"],
                "status": document["status"],
                "effective_status": document["effective_status"],
                "source_url": document["source_url"],
                "source_platform": document["source_platform"],
            }
        )
        if parent and parent["document_id"] in coded_results:
            parent_result = coded_results[parent["document_id"]]
            effective_status = parent_result["effective_status"]
            if effective_status == "current":
                review_status = "status_verified_pending_quality_review"
                raw_status = "随母标准现行"
            elif effective_status == "repealed":
                review_status = "status_verified_repealed"
                raw_status = "随母标准废止"
            else:
                review_status = "identity_or_status_review_required"
                raw_status = "母标准状态冲突"
            evidence = (
                f"该文档为 {parent_result['standard_no']}《{parent_result['title']}》修改单；"
                f"母标准官方状态为“{parent_result['official_status']}”，修改单随母标准治理。"
            )
            relation = {
                "relation_type": "AMENDS",
                "parent_document_id": parent["document_id"],
                "parent_standard_no": parent_result["standard_no"],
                "parent_title": parent_result["title"],
            }
            metadata["t020_governance"] = {
                "checked_at": checked_at,
                "identity_method": "explicit_amendment_title_to_parent_relation",
                "status_source": parent_result["status_source"],
                "status_evidence": evidence,
                "official_query_url": parent_result["official_query_url"],
                "official_url": parent_result["official_url"],
                "official_standard_no": None,
                "official_title": document["title"],
                "official_status": raw_status,
                "identity_relation": relation,
                "evidence_path": parent_result["evidence_path"],
                "evidence_sha256": parent_result["evidence_sha256"],
            }
            conn.execute(
                """
                update documents
                set status=?,effective_status=?,review_status=?,can_answer=0,source_url=?,source_platform=?,
                    source_metadata_json=?,updated_at=?
                where document_id=?
                """,
                (
                    raw_status,
                    effective_status,
                    review_status,
                    parent_result["official_url"],
                    parent_result["status_source"],
                    json_dumps(metadata),
                    checked_at,
                    document["document_id"],
                ),
            )
            if parent_result["evidence_path"]:
                evidence_path = Path(parent_result["evidence_path"])
                attach_status_evidence(
                    conn,
                    document_id=document["document_id"],
                    path=evidence_path,
                    source_url=parent_result["official_query_url"],
                    recorded_at=checked_at,
                )
            result = {
                "document_id": document["document_id"],
                "standard_no_before": None,
                "standard_no": None,
                "title_before": document["title"],
                "title": document["title"],
                "document_kind": "amendment",
                "verification_method": "explicit_amendment_parent_relation",
                "identity_relation": relation,
                "parent_document_id": parent["document_id"],
                "parent_standard_no": parent_result["standard_no"],
                "effective_status_before": document["effective_status"],
                "effective_status": effective_status,
                "official_status": raw_status,
                "status_source": parent_result["status_source"],
                "status_evidence": evidence,
                "status_checked_at": checked_at,
                "official_url": parent_result["official_url"],
                "official_query_url": parent_result["official_query_url"],
                "evidence_path": parent_result["evidence_path"],
                "evidence_sha256": parent_result["evidence_sha256"],
                "title_similarity": None,
                "request_error": None,
                "can_answer": 0,
            }
        else:
            search_path = evidence_dir.parent / "t020_300_questions_search_all.json"
            evidence = "未找到可证明该无编号材料具有正式标准效力的官方标准记录；保留为非规范性解读材料且禁止正向回答。"
            metadata["t020_governance"] = {
                "checked_at": checked_at,
                "identity_method": "title_search_no_official_standard_record",
                "status_source": "官方标准平台无匹配记录；公开网页仅作身份线索",
                "status_evidence": evidence,
                "official_query_url": None,
                "official_url": None,
                "official_standard_no": None,
                "official_title": None,
                "official_status": "无正式标准效力记录",
                "evidence_path": str(search_path.resolve()) if search_path.exists() else None,
                "evidence_sha256": sha256_bytes(search_path.read_bytes()) if search_path.exists() else None,
            }
            conn.execute(
                """
                update documents
                set status='non_normative_guidance_unverified',effective_status='governance_conflict',
                    review_status='identity_or_status_review_required',can_answer=0,source_metadata_json=?,updated_at=?
                where document_id=?
                """,
                (json_dumps(metadata), checked_at, document["document_id"]),
            )
            result = {
                "document_id": document["document_id"],
                "standard_no_before": None,
                "standard_no": None,
                "title_before": document["title"],
                "title": document["title"],
                "document_kind": "non_normative_guidance",
                "verification_method": "title_search_no_official_standard_record",
                "identity_relation": None,
                "parent_document_id": None,
                "parent_standard_no": None,
                "effective_status_before": document["effective_status"],
                "effective_status": "governance_conflict",
                "official_status": "无正式标准效力记录",
                "status_source": "官方标准平台无匹配记录；公开网页仅作身份线索",
                "status_evidence": evidence,
                "status_checked_at": checked_at,
                "official_url": None,
                "official_query_url": None,
                "evidence_path": str(search_path.resolve()) if search_path.exists() else None,
                "evidence_sha256": sha256_bytes(search_path.read_bytes()) if search_path.exists() else None,
                "title_similarity": None,
                "request_error": None,
                "can_answer": 0,
            }
        report_rows.append(result)
    return report_rows


def apply_governance_overrides(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    override_path: Path,
    *,
    checked_at: str,
) -> list[dict[str, Any]]:
    if not override_path.exists():
        return rows
    payload = json.loads(override_path.read_text(encoding="utf-8"))
    overrides = payload.get("documents") if isinstance(payload, dict) else None
    if not isinstance(overrides, list):
        raise ValueError(f"invalid governance override manifest: {override_path}")
    by_id = {row["document_id"]: row for row in rows}
    for override in overrides:
        document_id = str(override.get("document_id") or "")
        if document_id not in by_id:
            raise ValueError(f"override document is not in the T020 corpus: {document_id}")
        effective_status = str(override["effective_status"])
        if effective_status not in {"current", "repealed", "governance_conflict"}:
            raise ValueError(f"invalid effective status override for {document_id}: {effective_status}")
        evidence_paths = [Path(value) for value in override.get("evidence_paths") or []]
        missing = [str(path) for path in evidence_paths if not path.exists()]
        if missing:
            raise FileNotFoundError(f"missing override evidence for {document_id}: {missing}")
        document = conn.execute("select * from documents where document_id=?", (document_id,)).fetchone()
        metadata = load_json_object(document["source_metadata_json"])
        evidence_records = [
            {"path": str(path.resolve()), "sha256": sha256_bytes(path.read_bytes()), "bytes": path.stat().st_size}
            for path in evidence_paths
        ]
        governance = {
            "checked_at": checked_at,
            "identity_method": override["verification_method"],
            "status_source": override["status_source"],
            "status_evidence": override["status_evidence"],
            "official_query_url": override.get("official_query_url"),
            "official_url": override.get("official_url"),
            "official_standard_no": override.get("official_standard_no"),
            "official_title": override.get("official_title") or document["title"],
            "official_status": override["official_status"],
            "identity_relation": override.get("identity_relation"),
            "evidence_files": evidence_records,
            "governance_override_reason": override.get("governance_override_reason"),
        }
        metadata["t020_governance"] = governance
        review_status = str(override["review_status"])
        can_answer = int(bool(override.get("can_answer")))
        if can_answer and not (effective_status == "current" and review_status == "approved_for_service"):
            raise ValueError(f"unsafe can_answer override for {document_id}")
        conn.execute(
            """
            update documents
            set title=?,standard_no=?,status=?,effective_status=?,review_status=?,can_answer=?,
                source_url=?,source_platform=?,source_metadata_json=?,updated_at=?
            where document_id=?
            """,
            (
                override.get("official_title") or document["title"],
                override.get("official_standard_no", document["standard_no"]),
                override["official_status"],
                effective_status,
                review_status,
                can_answer,
                override.get("official_url"),
                override["status_source"],
                json_dumps(metadata),
                checked_at,
                document_id,
            ),
        )
        for path in evidence_paths:
            attach_status_evidence(
                conn,
                document_id=document_id,
                path=path,
                source_url=override.get("official_query_url") or override.get("official_url") or "local-governance-evidence",
                recorded_at=checked_at,
            )
        row = by_id[document_id]
        row.update(
            {
                "standard_no": override.get("official_standard_no", row["standard_no"]),
                "title": override.get("official_title") or row["title"],
                "verification_method": override["verification_method"],
                "effective_status": effective_status,
                "official_status": override["official_status"],
                "status_source": override["status_source"],
                "status_evidence": override["status_evidence"],
                "status_checked_at": checked_at,
                "official_url": override.get("official_url"),
                "official_query_url": override.get("official_query_url"),
                "evidence_path": " | ".join(str(path.resolve()) for path in evidence_paths),
                "evidence_sha256": " | ".join(record["sha256"] for record in evidence_records),
                "request_error": None,
                "can_answer": can_answer,
                "identity_relation": override.get("identity_relation"),
            }
        )
    return rows


def refresh_status_findings(conn: sqlite3.Connection, rows: list[dict[str, Any]], checked_at: str) -> None:
    technical_ids = [row["document_id"] for row in rows]
    placeholders = ",".join("?" for _ in technical_ids)
    conn.execute(
        f"delete from quality_findings where document_id in ({placeholders}) and finding_type='effective_status_unverified'",
        technical_ids,
    )
    conn.execute(
        f"delete from quality_findings where document_id in ({placeholders}) and finding_type='identity_status_governance_conflict'",
        technical_ids,
    )
    for row in rows:
        missing = conn.execute(
            "select finding_id from quality_findings where document_id=? and finding_type='missing_standard_number'",
            (row["document_id"],),
        ).fetchone()
        if missing and (row["document_kind"] == "amendment" or row.get("identity_relation")):
            conn.execute(
                """
                update quality_findings
                set severity='info',message=?,evidence_json=?,created_at=?
                where finding_id=?
                """,
                (
                    "无编号技术材料已记录明确的母标准或标准体系关系及治理证据，可保留无独立标准号。",
                    json_dumps(
                        {
                            "title": row["title"],
                            "parent_document_id": row["parent_document_id"],
                            "parent_standard_no": row["parent_standard_no"],
                            "identity_relation": row.get("identity_relation")
                            or {
                                "relation_type": "AMENDS",
                                "parent_document_id": row["parent_document_id"],
                                "parent_standard_no": row["parent_standard_no"],
                            },
                            "status_checked_at": checked_at,
                        }
                    ),
                    checked_at,
                    missing[0],
                ),
            )
        if row["effective_status"] == "governance_conflict":
            finding_id = "finding-t020-" + row["document_id"]
            conn.execute(
                """
                insert or replace into quality_findings(
                  finding_id,document_id,unit_id,severity,finding_type,message,evidence_json,created_at
                ) values (?, ?, null, 'error', 'identity_status_governance_conflict', ?, ?, ?)
                """,
                (
                    finding_id,
                    row["document_id"],
                    "文献身份或正式效力无法由官方标准记录确认，禁止进入正向回答。",
                    json_dumps(
                        {
                            "standard_no": row["standard_no"],
                            "title": row["title"],
                            "status_source": row["status_source"],
                            "status_evidence": row["status_evidence"],
                            "status_checked_at": checked_at,
                        }
                    ),
                    checked_at,
                ),
            )


def write_reports(report_dir: Path, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "t020_standard_identity_status_reconciliation.json"
    csv_path = report_dir / "t020_standard_identity_status_reconciliation.csv"
    md_path = report_dir / "t020_standard_identity_status_reconciliation.md"
    json_path.write_text(json.dumps({"summary": summary, "documents": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = list(rows[0])
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    status_counts = Counter(row["effective_status"] for row in rows)
    kind_counts = Counter(row["document_kind"] for row in rows)
    conflicts = [row for row in rows if row["effective_status"] == "governance_conflict"]
    changed_titles = [row for row in rows if row["title_before"] != row["title"]]
    lines = [
        "# T020 标准身份与有效状态核验报告",
        "",
        f"- 核验时间：`{summary['checked_at']}`",
        f"- 技术文档：{len(rows)}",
        f"- 分类：{dict(kind_counts)}",
        f"- 有效状态：{dict(status_counts)}",
        f"- 官方精确编号记录：{summary['official_exact_matches']}",
        f"- 修改单母标准关系：{summary['amendment_relations']}",
        f"- 标题按官方记录修正：{len(changed_titles)}",
        f"- 状态或身份冲突：{len(conflicts)}",
        f"- SQLite 完整性：`{summary['integrity_check']}`",
        f"- v3 生产库未修改：`{summary['v3_unchanged']}`",
        f"- 检索表新增：{summary['retrieval_tables_created']}",
        "",
        "## 冲突文档",
        "",
    ]
    if conflicts:
        lines.extend(f"- `{row['standard_no'] or 'NO-CODE'}` {row['title']}：{row['status_evidence']}" for row in conflicts)
    else:
        lines.append("- 无。")
    lines.extend(["", "## 标题修正", ""])
    if changed_titles:
        lines.extend(f"- `{row['standard_no']}`：{row['title_before']} -> {row['title']}" for row in changed_titles)
    else:
        lines.append("- 无。")
    lines.extend(
        [
            "",
            "## 审计规则",
            "",
            "- 带编号标准以官方平台标准号完全匹配为身份主键，并交叉检查标题。",
            "- 修改单保留无独立标准号，但必须记录 `AMENDS` 母标准关系，并随母标准官方状态治理。",
            "- 无标准号的权威解读材料，仅在来源、版本和解释对象关系明确且经治理批准后可引用；必须标注为解释性材料，不替代正式标准条款。",
            "- 本任务未修改任何页面正文、条款单元，也未建立 FTS、向量或知识图谱。",
        ]
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def file_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile v4 technical-standard identity and status from official sources.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.05)
    parser.add_argument("--overrides", type=Path, default=None)
    args = parser.parse_args()

    evidence_dir = args.root / "source_evidence" / "t020"
    report_dir = args.root / "reports"
    backup_dir = args.root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    checked_at = utc_now()
    stamp = checked_at.replace(":", "").replace("-", "").replace("+", "_")
    backup_path = backup_dir / f"corpus.pre_t020_{stamp}.sqlite"
    shutil.copy2(args.db, backup_path)
    v3_signature_before = file_signature(V3_DB)

    with sqlite3.connect(args.db) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys=on")
        before_counts = dict(
            conn.execute(
                "select effective_status,count(*) from documents where corpus='technical_standards' group by effective_status"
            ).fetchall()
        )
        coded_rows, coded_results = collect_coded_documents(
            conn,
            evidence_dir,
            refresh=args.refresh,
            sleep_seconds=args.sleep,
            checked_at=checked_at,
        )
        uncoded_rows = collect_uncoded_documents(
            conn,
            evidence_dir,
            coded_results,
            checked_at=checked_at,
        )
        rows = sorted(coded_rows + uncoded_rows, key=lambda row: (row["standard_no"] or "ZZZ", row["title"]))
        override_path = args.overrides or (args.root / "governance" / "t020_official_overrides.json")
        rows = apply_governance_overrides(conn, rows, override_path, checked_at=checked_at)
        refresh_status_findings(conn, rows, checked_at)
        conn.commit()
        integrity = conn.execute("pragma integrity_check").fetchone()[0]
        remaining_unverified = conn.execute(
            "select count(*) from documents where corpus='technical_standards' and effective_status='unverified'"
        ).fetchone()[0]
        retrieval_tables = [
            row[0]
            for row in conn.execute("select name from sqlite_master where type='table'")
            if row[0] in {"chunks_fts", "chunk_vectors", "chunk_embeddings", "kg_entities", "kg_relations"}
        ]
        after_counts = dict(
            conn.execute(
                "select effective_status,count(*) from documents where corpus='technical_standards' group by effective_status"
            ).fetchall()
        )
    v3_signature_after = file_signature(V3_DB)
    summary = {
        "task": "T020",
        "checked_at": checked_at,
        "database": str(args.db.resolve()),
        "rollback_backup": str(backup_path.resolve()),
        "technical_document_count": len(rows),
        "coded_documents": len(coded_rows),
        "uncoded_documents": len(uncoded_rows),
        "official_exact_matches": sum(
            row["verification_method"] in {"official_exact_standard_number", "official_replacement_announcement", "official_publication_and_current_use"}
            and row["effective_status"] != "governance_conflict"
            for row in rows
        ),
        "amendment_relations": sum(row["document_kind"] == "amendment" for row in rows),
        "before_effective_status": before_counts,
        "after_effective_status": after_counts,
        "remaining_unverified": remaining_unverified,
        "governance_conflicts": sum(row["effective_status"] == "governance_conflict" for row in rows),
        "integrity_check": integrity,
        "v3_signature_before": v3_signature_before,
        "v3_signature_after": v3_signature_after,
        "v3_unchanged": v3_signature_before == v3_signature_after,
        "retrieval_tables_created": retrieval_tables,
        "body_text_mutations": 0,
    }
    write_reports(report_dir, rows, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if integrity != "ok" or remaining_unverified or not summary["v3_unchanged"] or retrieval_tables:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
