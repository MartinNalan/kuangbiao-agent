from __future__ import annotations

import json
import hashlib
import math
import re
import sqlite3
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import quote

from .ann_index import AnnManifest, get_ann_index
from .config import get_settings
from .embedding_provider import EmbeddingProvider, cosine_dense, embedding_config
from .query_understanding import (
    QueryPlan,
    canonical_exploration_type,
    query_plan_from_payload,
    understand_query,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KB_ROOT = PROJECT_ROOT / "data" / "knowledge_base"
DEFAULT_DB_PATH = DEFAULT_KB_ROOT / "db" / "knowledge_base.sqlite"
DOMAIN_LEXICON_PATH = Path(__file__).with_name("domain_lexicon.json")
QUOTE_LIMIT = 260
VECTOR_DIM = 512
RRF_K = 60
ROUTE_WEIGHTS = {"full_text": 1.15, "graph": 0.9, "vector": 1.0, "reference": 1.2}


@dataclass(frozen=True)
class VectorCandidateResult:
    candidates: tuple[tuple[sqlite3.Row, float], ...] = ()
    route: str = "none"
    embedding_ms: float = 0.0
    search_ms: float = 0.0
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.route in {"ann", "exact_dense"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    return conn


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.executescript(
            """
            create table if not exists documents (
              document_id text primary key,
              title text not null,
              standard_no text,
              document_type text not null default 'standard',
              status text not null default 'unknown',
              source_type text not null default 'local_kb',
              text_access text not null default 'ocr_text',
              validation_status text not null default 'parsed',
              visibility text not null default 'internal',
              owner_user_id text,
              organization_id text,
              review_status text not null default 'approved_for_service',
              publish_date text,
              implementation_date text,
              ingestion_time text not null,
              updated_at text not null,
              source_priority integer,
              source_trace_json text,
              bibliographic_json text,
              quality_json text,
              page_count integer not null default 0,
              chunk_count integer not null default 0,
              table_count integer not null default 0,
              can_answer integer not null default 0
            );

            create table if not exists chunks (
              chunk_id text primary key,
              document_id text not null references documents(document_id) on delete cascade,
              chunk_type text not null default 'text',
              title text not null,
              standard_no text,
              section_path text,
              clause_no text,
              page_start integer,
              page_end integer,
              char_start integer,
              char_end integer,
              text text not null,
              table_json text,
              source_type text not null default 'local_kb',
              text_access text not null default 'ocr_text',
              parse_method text not null default 'ocr_page_chunk',
              confidence real,
              validation_status text not null default 'parsed',
              visibility text not null default 'internal',
              source_ref text,
              created_at text not null
            );

            create table if not exists candidates (
              candidate_id text primary key,
              triggering_question text,
              standard_no text,
              title text,
              source_url text,
              source_type text,
              text_access text,
              page_range text,
              extracted_text text,
              ocr_confidence real,
              ocr_engine text,
              ocr_engine_version text,
              review_status text not null default 'candidate_found',
              copyright_note text,
              created_at text not null,
              updated_at text not null
            );

            create table if not exists ingest_runs (
              run_id text primary key,
              source_root text not null,
              started_at text not null,
              finished_at text,
              document_count integer not null default 0,
              chunk_count integer not null default 0,
              table_count integer not null default 0,
              status text not null default 'running',
              summary_json text
            );

            create index if not exists idx_documents_standard_no on documents(standard_no);
            create index if not exists idx_documents_title on documents(title);
            create index if not exists idx_documents_status on documents(status);
            create index if not exists idx_documents_visibility on documents(visibility);
            create index if not exists idx_chunks_document_id on chunks(document_id);
            create index if not exists idx_chunks_page on chunks(page_start, page_end);
            """
        )
        try:
            conn.execute(
                """
                create virtual table if not exists chunks_fts using fts5(
                  chunk_id unindexed,
                  document_id unindexed,
                  title,
                  standard_no,
                  section_path,
                  text,
                  tokenize='trigram'
                )
                """
            )
        except sqlite3.OperationalError:
            conn.execute(
                """
                create virtual table if not exists chunks_fts using fts5(
                  chunk_id unindexed,
                  document_id unindexed,
                  title,
                  standard_no,
                  section_path,
                  text
                )
                """
            )
        ensure_document_source_columns(conn)
        hydrate_official_urls(conn)


def reset_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    if db_path.exists():
        db_path.unlink()
    init_db(db_path)


def ensure_document_source_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("pragma table_info(documents)").fetchall()}
    if "official_url" not in columns:
        conn.execute("alter table documents add column official_url text")
    if "source_platform" not in columns:
        conn.execute("alter table documents add column source_platform text")


def official_source(standard_no: str | None) -> tuple[str | None, str | None]:
    if not standard_no:
        return None, None
    code = standard_no.strip()
    upper = code.upper().replace(" ", "")
    if upper.startswith("GB"):
        return "国家标准公开系统", f"https://std.samr.gov.cn/search/stdPage?q={quote(code)}&tid="
    if upper.startswith(("DZ/T", "DZ")):
        return "自然资源标准化信息服务平台", f"http://www.nrsis.org.cn/portal/xxcx/std?key={quote(code)}"
    return None, None


def hydrate_official_urls(conn: sqlite3.Connection) -> None:
    policy_rows = conn.execute(
        """
        select document_id, source_trace_json
        from documents
        where source_type = 'official_fulltext'
          and (official_url is null or official_url = '')
        """
    ).fetchall()
    for row in policy_rows:
        try:
            trace = json.loads(row["source_trace_json"] or "{}")
        except json.JSONDecodeError:
            trace = {}
        source_url = trace.get("source_url")
        if source_url:
            conn.execute(
                "update documents set official_url = ?, source_platform = ? where document_id = ?",
                (source_url, "自然资源部政策法规库", row["document_id"]),
            )

    rows = conn.execute(
        """
        select document_id, standard_no
        from documents
        where standard_no is not null
          and source_type != 'official_fulltext'
          and (official_url is null or official_url = '')
        """
    ).fetchall()
    for row in rows:
        platform, url = official_source(row["standard_no"])
        if url:
            conn.execute(
                "update documents set official_url = ?, source_platform = ? where document_id = ?",
                (url, platform, row["document_id"]),
            )


@lru_cache(maxsize=1)
def domain_lexicon() -> list[dict[str, Any]]:
    if not DOMAIN_LEXICON_PATH.exists():
        return []
    try:
        data = json.loads(DOMAIN_LEXICON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    entries = [entry for entry in data if isinstance(entry, dict) and entry.get("status") == "active"]
    return sorted(entries, key=lambda entry: int(entry.get("priority") or 0), reverse=True)


def matched_lexicon_entries(query: str, intent_label: str | None = None) -> list[dict[str, Any]]:
    matched = []
    for entry in domain_lexicon():
        if intent_label and entry.get("intent_label") != intent_label:
            continue
        probes = [entry.get("user_expression"), entry.get("canonical_term")]
        if any(probe and str(probe) in query for probe in probes):
            matched.append(entry)
    return matched


def lexicon_query_expansions(query: str) -> list[str]:
    expansions: list[str] = []
    for entry in matched_lexicon_entries(query):
        expansions.append(str(entry.get("canonical_term") or ""))
        expansions.extend(str(term) for term in (entry.get("positive_expansions") or []) if term)
    return [term for term in expansions if term]


def query_has_intent(query: str, intent_label: str) -> bool:
    return bool(matched_lexicon_entries(query, intent_label=intent_label))


def lexicon_negative_terms(query: str, intent_label: str | None = None) -> list[str]:
    terms: list[str] = []
    for entry in matched_lexicon_entries(query, intent_label=intent_label):
        terms.extend(str(term) for term in (entry.get("negative_terms") or []) if term)
    deduped: list[str] = []
    seen = set()
    for term in terms:
        if term not in seen:
            deduped.append(term)
            seen.add(term)
    return deduped


def split_evidence_sentences(text: str) -> list[str]:
    clean = re.sub(r"\s+", " ", text).strip()
    if not clean:
        return []
    parts = re.split(r"(?<=[。！？；;.!?])\s*|\n+", clean)
    sentences = [part.strip() for part in parts if part.strip()]
    if len(sentences) <= 1 and len(clean) > 120:
        clauses = re.split(r"(?<=[，,、])\s*", clean)
        sentences = [part.strip() for part in clauses if part.strip()]
    return sentences or [clean]


def quote_text(text: str, query: str = "", limit: int = QUOTE_LIMIT, max_sentences: int = 3) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    targeted_patterns: list[str] = []
    if "无限外推" in query:
        infinite_match = re.search(r"(b\)\s*无限外推：.*?经验工程间距\s*1/2\s*尖推。)", clean)
        if infinite_match:
            finite_match = re.search(r"(普查阶段.*?实际工程间距\s*的\s*1/4\s*平推处理。)", clean)
            quote = "".join(
                part.group(1).strip()
                for part in (finite_match, infinite_match)
                if part is not None
            )
            return quote if len(quote) <= limit else quote[:limit].rstrip() + "..."
    if any(term in query for term in ("真实性", "弄虚作假")):
        targeted_patterns.append(r"(矿业权人应当对其报送的储量报告的真实性负责，不得弄虚作假。)")
    if "采矿权" in query and any(term in query for term in ("申请材料", "申请资料", "资料清单", "附件4")):
        targeted_patterns.append(
            r"(自然资源部负责的矿业权.*?按照本通知附件2探矿权申请资料清单及要求、附件4采矿权申请资料清单及要求执行。)"
        )
    for pattern in targeted_patterns:
        match = re.search(pattern, clean)
        if match:
            quote = match.group(1).strip()
            return quote if len(quote) <= limit else quote[:limit].rstrip() + "..."
    if len(clean) <= limit:
        return clean
    priority_terms = [term for term in ["起草单位", "起草人", "发布", "实施", "代替", "替代"] if term in query]
    terms = priority_terms + query_terms(query)
    sentences = split_evidence_sentences(clean)
    scored: list[tuple[int, int, str]] = []
    for index, sentence in enumerate(sentences):
        score = sum(1 for term in terms if term and term in sentence)
        if score:
            scored.append((score, index, sentence))
    if scored:
        best_index = sorted(scored, key=lambda item: (-item[0], item[1]))[0][1]
        start = best_index
        selected = [sentences[best_index]]
        if len(selected[0]) < limit * 0.45 and best_index > 0:
            start = best_index - 1
            selected.insert(0, sentences[start])
        end = best_index + 1
        while len(selected) < max_sentences and end < len(sentences) and len("".join(selected + [sentences[end]])) <= limit:
            selected.append(sentences[end])
            end += 1
        quote = "".join(selected).strip()
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(sentences) else ""
        if len(quote) > limit:
            quote = quote[:limit].rstrip() + "..."
            suffix = ""
        return prefix + quote + suffix
    return clean[:limit].rstrip() + "..."


def normalize_table_cell(value: Any) -> str:
    cell = re.sub(r"\s+", "", str(value)).strip()
    if cell == "工":
        return "Ⅰ"
    return cell


def markdown_table_cell(value: Any) -> str:
    cell = re.sub(r"\s+", " ", str(value or "")).strip()
    if cell == "工":
        cell = "Ⅰ"
    return cell.replace("\\", "\\\\").replace("|", "\\|")


def _markdown_table_quote(matrix: list[Any], caption: str, limit: int) -> str | None:
    rows = [
        [markdown_table_cell(cell) for cell in row]
        for row in matrix
        if isinstance(row, list) and any(str(cell).strip() for cell in row)
    ]
    if not rows:
        return None
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = normalized[0]
    lines = []
    if caption:
        lines.extend([f"**{str(caption).strip()}**", ""])
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in normalized[1:]:
        candidate = "| " + " | ".join(row) + " |"
        if len("\n".join([*lines, candidate])) > limit:
            break
        lines.append(candidate)
    return "\n".join(lines)


def table_references(text: str) -> tuple[str, ...]:
    compact = re.sub(r"\s+", "", text or "").upper()
    references: list[str] = []
    for start_letter, start_no, end_letter, end_no in re.findall(
        r"表?([A-Z])\.(\d+)\s*(?:至|到|~|～|-)\s*表?([A-Z])\.(\d+)",
        compact,
    ):
        if start_letter != end_letter:
            continue
        lower, upper = sorted((int(start_no), int(end_no)))
        if upper - lower > 20:
            continue
        references.extend(f"{start_letter}.{number}" for number in range(lower, upper + 1))
    references.extend(re.findall(r"表([A-Z]\.[0-9]+)", compact))
    return tuple(dict.fromkeys(references))


def transfer_evidence_quote(text: str) -> tuple[str | None, str | None]:
    clean = re.sub(r"\s+", " ", text or "").strip()
    policy = re.search(
        r"(探矿权转采矿权，应当依据经评审备案的矿产资源储量报告。"
        r"资源储量规模为大型的非煤矿山、大中型煤矿应当达到勘探程度，"
        r"其他矿山应当达到详查（含）以上程度。)",
        clean,
    )
    if policy:
        return policy.group(1), "二、#1"
    report_limit = re.search(
        r"(矿产资源储量核实报告不能替代探矿权转采矿权时应提交的地质勘查报告。)",
        clean,
    )
    if report_limit:
        return report_limit.group(1), "A.9.5"
    return None, None


def companion_resource_type_quote(text: str) -> tuple[str | None, str | None]:
    clean = re.sub(r"\s+", " ", text or "").strip()
    intro = re.search(
        r"(9\.2\s*当伴生矿产进行了基本分析，且研究工作达到以下程度时，"
        r"其资源储量类型可与主要矿产相同：)",
        clean,
    )
    if intro:
        parts = [intro.group(1)]
        patterns = (
            r"(a[）)]\s*地质研究程度：伴生矿产的质量、赋存状态、分布规律等达到与主要矿产相同的查明程度；)",
            r"(b[）)]\s*矿石加工选冶试验研究程度：伴生矿产的物质组成与回收利用的加工选冶试验研究等达到与\s*主要矿产相应的查明程度；)",
            r"(c[）)]\s*可行性评价：对伴生矿产综合回收的经济意义作出了相应评价。)",
        )
        for pattern in patterns:
            match = re.search(pattern, clean)
            if match:
                parts.append(match.group(1))
        if len(parts) >= 2:
            return " ".join(parts), "9.2"
    for clause, pattern in (
        ("9.3", r"(9\.3\s*当伴生矿产进行了基本分析但未能满足9\.2中其他条件时，应降低资源储量类型。)"),
        ("9.4", r"(9\.4\s*伴生矿产只进行了组合分析而未做基本分析时，划为推断资源量。)"),
    ):
        match = re.search(pattern, clean)
        if match:
            return match.group(1), clause
    return None, None


def basic_analysis_quote(text: str, plan: QueryPlan) -> tuple[str | None, str | None]:
    clean = re.sub(r"\s+", " ", text or "").strip()
    mineral_patterns = []
    if "铁矿" in plan.normalized_query:
        mineral_patterns.append(
            r"(d[）)]?\s*铁矿石基本分析项目.*?赤铁矿石、褐铁矿石、菱铁矿石，分析项目为\s*TFe。)"
        )
    if "锰矿" in plan.normalized_query:
        mineral_patterns.append(r"(e[）)]?\s*锰矿石基本分析项目.*?(?=\s*f[）)]|$))")
    if "铬矿" in plan.normalized_query:
        mineral_patterns.append(r"(f[）)]?\s*铬矿石基本分析项目.*?(?=\s*[g-z][）)]|$))")
    for pattern in mineral_patterns:
        match = re.search(pattern, clean, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip(), "6.7.2.3"
    return None, None


def structured_intent_quote(row: sqlite3.Row, plan: QueryPlan) -> tuple[str | None, str | None]:
    text = row["text"] or ""
    if plan.intent == "exploration_to_mining_eligibility":
        return transfer_evidence_quote(text)
    if plan.intent == "companion_resource_type":
        return companion_resource_type_quote(text)
    if plan.intent == "basic_analysis_items":
        return basic_analysis_quote(text, plan)
    return None, None


def _target_table_quote(matrix: list[Any], caption: str, plan: QueryPlan, limit: int) -> str | None:
    target_type = plan.target_exploration_type
    if not target_type:
        return None

    normalized_rows = [
        [normalize_table_cell(cell) for cell in row]
        for row in matrix
        if isinstance(row, list) and any(str(cell).strip() for cell in row)
    ]
    target_index = -1
    first_data_index = -1
    for index, row in enumerate(normalized_rows):
        row_type = canonical_exploration_type(row[0]) if row else None
        if row_type and first_data_index < 0:
            first_data_index = index
        if row_type == target_type:
            target_index = index
            break
    if target_index < 0:
        return None

    target_row = normalized_rows[target_index]
    header_rows = normalized_rows[:first_data_index]
    labels = ["勘查类型"]
    for column in range(1, len(target_row)):
        parts: list[str] = []
        for row in header_rows:
            if column >= len(row):
                continue
            value = row[column]
            if not value or value == "勘查类型" or "工程间距" in value or value in parts:
                continue
            parts.append(value)
        labels.append("-".join(parts[-2:]) or f"第{column}列")

    measurements = []
    for column, value in enumerate(target_row[1:], start=1):
        label = labels[column] if column < len(labels) else f"第{column}列"
        distance = value.replace("~", "～")
        measurements.append(f"{label} {distance} m")
    if not measurements:
        return None

    title = caption.strip() or "参考基本勘查工程间距"
    text = f"{title}。控制资源量勘查工程间距：{target_type}类型；" + "；".join(measurements) + "。"
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _service_material_table_quote(matrix: list[Any], caption: str, limit: int) -> str | None:
    rows = [row for row in matrix if isinstance(row, list) and any(str(cell).strip() for cell in row)]
    if len(rows) < 2:
        return None
    headers = [normalize_table_cell(cell).replace("*", "") for cell in rows[0]]
    material_index = next(
        (
            index
            for index, header in enumerate(headers)
            if any(marker in header for marker in ("材料名称", "提交材料名称", "申请材料"))
        ),
        1 if len(headers) > 1 else 0,
    )
    lines = [caption.strip() or "申请材料目录"]
    for row in rows[1:]:
        if material_index >= len(row):
            continue
        material = re.sub(r"\s+", " ", str(row[material_index])).strip()
        if not material:
            continue
        sequence = re.sub(r"\s+", "", str(row[0])).strip() if row else ""
        line = f"{sequence}. {material}" if sequence else material
        candidate = "\n".join([*lines, line])
        if len(candidate) > limit:
            break
        lines.append(line)
    if len(lines) == 1:
        return None
    return "\n".join(lines)


def table_quote(
    table_json: str | None,
    fallback: str,
    query: str = "",
    limit: int = QUOTE_LIMIT,
    plan: QueryPlan | None = None,
) -> str:
    if not table_json:
        return quote_text(fallback, query, limit)
    try:
        table = json.loads(table_json)
    except json.JSONDecodeError:
        return quote_text(fallback, query, limit)

    caption = table.get("caption") or ""
    matrix = table.get("matrix") or []
    query_plan = plan or understand_query(query)
    if query_plan.output_mode == "table":
        markdown_quote = _markdown_table_quote(matrix, str(caption), limit)
        if markdown_quote:
            return markdown_quote
    target_quote = _target_table_quote(matrix, str(caption), query_plan, limit)
    if target_quote:
        return target_quote
    if query_plan.intent == "service_materials":
        material_quote = _service_material_table_quote(matrix, str(caption), limit)
        if material_quote:
            return material_quote

    lines: list[str] = []
    if caption:
        lines.append(str(caption).strip())
    terms = query_terms(query)
    selected_rows = []
    for row in matrix:
        if not isinstance(row, list):
            continue
        cells = [normalize_table_cell(cell) for cell in row]
        row_text = " | ".join(cells)
        if any(cells) and (not terms or any(term in row_text for term in terms)):
            selected_rows.append(row_text)
        if len(selected_rows) >= 3:
            break
    if not selected_rows:
        for row in matrix[:3]:
            if not isinstance(row, list):
                continue
            cells = [normalize_table_cell(cell) for cell in row]
            if any(cells):
                selected_rows.append(" | ".join(cells))
    lines.extend(selected_rows[:3])
    text = "\n".join(lines).strip() or fallback
    return quote_text(text, query, limit, max_sentences=3)


def is_standard_selection_query(query: str) -> bool:
    return any(
        term in query
        for term in (
            "使用哪个标准",
            "用哪个标准",
            "适用哪个标准",
            "采用哪个标准",
            "使用哪个规范",
            "用哪个规范",
            "适用哪个规范",
            "采用哪个规范",
            "应该使用",
            "应该用",
        )
    )


def is_policy_management_query(query: str) -> bool:
    return any(
        term in query
        for term in (
            "审批",
            "管理",
            "通知",
            "办法",
            "条例",
            "法律",
            "国务院",
            "自然资源部",
            "政策",
            "文件",
            "文号",
            "战略性矿产资源目录",
        )
    )


def is_policy_authority_query(query: str) -> bool:
    return understand_query(query).intent == "authority_responsibility"


def is_standard_or_technical_query(query: str) -> bool:
    return any(term in query for term in ("标准", "规范", "规程", "技术", "工程间距", "勘查类型", "勘查规范"))


def query_terms(query: str, plan: QueryPlan | None = None) -> list[str]:
    effective_plan = plan or understand_query(query)
    normalized_query = effective_plan.normalized_query
    terms: list[str] = []
    raw_terms = re.findall(
        r"[A-Za-z0-9]+(?:/[A-Za-z0-9]+)?(?:[-.][A-Za-z0-9]+)*|[\u4e00-\u9fff]{2,}",
        normalized_query,
    )
    stopwords = [
        "关于",
        "哪个",
        "应该",
        "使用",
        "标准",
        "规范",
        "规定",
        "中的",
        "中",
        "是什么",
        "什么",
        "是否",
        "已有",
        "知识库",
        "关系",
        "现在",
        "还",
        "有效",
        "了",
        "的",
        "有",
        "帮我",
        "列举",
        "出来",
    ]
    key_phrases = [
        "基本工程间距",
        "工程间距",
        "矿体外推",
        "外推原则",
        "推断资源量工程间距",
        "基本工程间距",
        "实际工程间距",
        "同类型资源量工程间距",
        "资源量",
        "储量",
        "分类",
        "分类关系",
        "起草单位",
        "起草人",
        "发布",
        "实施",
        "代替",
        "方解石",
        "沙金",
        "砂金",
        "金属砂矿",
        "金属砂矿类",
        "岩金",
        "金矿",
        "充水矿床",
        "复杂程度",
        "分型表",
        "压覆",
        "压覆矿产资源",
        "审批",
        "建设项目",
        "战略性矿产",
        "矿产资源法实施条例",
        "矿产资源法",
        "采矿许可证",
        "勘查许可证",
        "采矿权延续登记",
        "采矿权申请资料清单及要求",
        "探矿权首次登记",
        "采矿许可变更（开采方式）",
        "矿产资源储量评审备案",
        "矿产资源开采方案",
        "申请材料",
        "申请资料",
        "申请材料目录",
        "申请材料提交",
        "办理基本流程",
        "办理方式",
        "办结时限",
        "附件4",
        "办理依据",
        "矿业权人",
        "真实性负责",
        "不得弄虚作假",
        "无限外推",
        "经验工程间距1/2尖推",
        "勘查实施方案",
        "评审或审查",
        "储量评审备案",
        "矿产资源储量评审备案",
        "评审备案范围和权限",
        "自然资源主管部门负责",
        "自然资源部负责",
        "省级自然资源主管部门负责",
    ]
    synonyms = {
        "金矿": ["岩金"],
        "岩金": ["金矿"],
        "沙金": ["砂金", "金属砂矿", "金属砂矿类"],
        "砂金": ["沙金", "金属砂矿", "金属砂矿类"],
        "金属砂矿": ["砂金", "沙金", "金属砂矿类"],
        "采矿证": ["采矿许可证"],
        "储量评审": ["矿产资源储量评审备案", "评审备案范围和权限"],
        "储量报告评审": ["矿产资源储量评审备案", "评审备案范围和权限"],
        "储量报告": ["矿产资源储量报告"],
        "去哪个机构": ["谁负责", "负责", "自然资源主管部门负责"],
        "哪个机构": ["谁负责", "负责", "自然资源主管部门负责"],
        "哪一级部门": ["省级自然资源主管部门负责", "自然资源部负责"],
    }
    for term in raw_terms:
        term = term.strip()
        if not term:
            continue
        terms.append(term)
        if re.fullmatch(r"[\u4e00-\u9fff]{4,}", term):
            reduced = term
            for stopword in stopwords:
                reduced = reduced.replace(stopword, " ")
            terms.extend(x for x in re.split(r"\s+", reduced) if len(x) >= 2)
        for phrase in key_phrases:
            if phrase in term or phrase in normalized_query:
                terms.append(phrase)
        for source, replacements in synonyms.items():
            if source in term or source in normalized_query:
                terms.extend(replacements)
    if effective_plan.target_exploration_type:
        ascii_type = {"Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III"}[effective_plan.target_exploration_type]
        terms.extend([f"{effective_plan.target_exploration_type}类型", ascii_type])
    terms.extend(lexicon_query_expansions(normalized_query))
    terms.extend(effective_plan.subject_terms)
    terms.extend(effective_plan.required_terms)
    terms.extend(effective_plan.alternative_terms)
    terms.extend(effective_plan.candidate_title_terms)
    terms.extend(effective_plan.standard_numbers)
    for group in effective_plan.required_evidence_groups:
        terms.extend(group)
    if effective_plan.intent == "authority_responsibility":
        terms.extend(
            [
                "自然资规〔2023〕6号",
                "明确评审备案范围和权限",
                "自然资源部",
                "省级自然资源主管部门",
                "自然资源部负责本级已颁发勘查许可证或采矿许可证",
                "其他由省级自然资源主管部门负责",
            ]
        )
    elif effective_plan.intent == "service_materials":
        terms.extend(effective_plan.candidate_title_terms)
        if "自然资规〔2023〕4号" not in effective_plan.standard_numbers:
            terms.extend(["申请材料", "申请材料目录", "提交材料名称", "材料名称"])
        else:
            terms.extend(
                [
                    "自然资规〔2023〕4号",
                    "采矿权延续登记",
                    "采矿权申请资料清单及要求",
                    "附件4",
                    "申请材料",
                ]
            )
    elif effective_plan.intent == "service_procedure_basis":
        terms.extend(effective_plan.candidate_title_terms)
        if "自然资规〔2023〕4号" not in effective_plan.standard_numbers:
            terms.extend(["办理基本流程", "办理方式", "申请材料提交"])
        else:
            terms.extend(
                [
                    "自然资规〔2023〕4号",
                    "矿产资源勘查开采登记管理",
                    "采矿权登记",
                    "采矿权申请资料清单及要求",
                ]
            )
    elif effective_plan.intent == "service_time_limit":
        terms.extend([*effective_plan.candidate_title_terms, "办结时限", "工作日"])
    elif effective_plan.intent == "projection_numeric_rule":
        terms.extend(
            [
                "DZ/T 0338.1-2020",
                "6.2.2.1",
                "无限外推",
                "经验工程间距1/2尖推",
            ]
        )
    elif effective_plan.intent == "legal_responsibility":
        terms.extend(
            [
                "国令第839号",
                "第四十三条",
                "矿业权人",
                "储量报告的真实性负责",
                "不得弄虚作假",
            ]
        )
    deduped: list[str] = []
    seen = set()
    for term in terms:
        if term and term not in seen:
            deduped.append(term)
            seen.add(term)
    return deduped


def _fts_phrase(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def fts_query(query: str, plan: QueryPlan | None = None, *, strict: bool = False) -> str:
    effective_plan = plan or understand_query(query)
    if strict and effective_plan.required_evidence_groups:
        groups = []
        for group in effective_plan.required_evidence_groups:
            values = [term for term in group if term and len(term.strip()) >= 2]
            if values:
                groups.append("(" + " OR ".join(_fts_phrase(term) for term in values[:8]) + ")")
        if groups:
            return " AND ".join(groups)
    terms = [
        term
        for term in query_terms(query, effective_plan)
        if not re.fullmatch(r"[0-9IVXⅠⅡⅢ]+", term, flags=re.IGNORECASE)
    ]
    if not terms:
        return ""
    return " OR ".join(_fts_phrase(term) for term in terms[:16])


def vector_tokens(text: str) -> list[str]:
    text = text.upper()
    words = re.findall(r"[A-Z0-9]+(?:/[A-Z0-9]+)?(?:[-.][A-Z0-9]+)*|[\u4e00-\u9fff]{2,}", text)
    out: list[str] = []
    for word in words:
        out.append(word)
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", word):
            for n in (2, 3, 4):
                out.extend(word[i : i + n] for i in range(0, max(0, len(word) - n + 1)))
    return out


def hashed_vector(text: str) -> dict[int, float]:
    counts = Counter(vector_tokens(text))
    buckets: dict[int, float] = {}
    for token, count in counts.items():
        idx = int(hashlib.sha1(token.encode("utf-8")).hexdigest()[:8], 16) % VECTOR_DIM
        buckets[idx] = buckets.get(idx, 0.0) + 1.0 + math.log(count)
    norm = math.sqrt(sum(value * value for value in buckets.values())) or 1.0
    return {idx: value / norm for idx, value in buckets.items()}


def cosine_sparse(left: dict[int, float], right_json: str) -> float:
    if not left or not right_json:
        return 0.0
    try:
        right = json.loads(right_json)
    except json.JSONDecodeError:
        return 0.0
    return sum(left.get(int(idx), 0.0) * float(value) for idx, value in right)


def parse_dense_vector(vector_json: str) -> list[float]:
    try:
        values = json.loads(vector_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(values, list):
        return []
    return [float(value) for value in values]


def table_has_exploration_type(table_json: str | None, target_type: str | None) -> bool:
    if not table_json or not target_type:
        return False
    try:
        table = json.loads(table_json)
    except json.JSONDecodeError:
        return False
    for row in table.get("matrix") or []:
        if isinstance(row, list) and row and canonical_exploration_type(row[0]) == target_type:
            return True
    return False


def row_has_engineering_distance_evidence(row: sqlite3.Row, plan: QueryPlan) -> bool:
    if plan.intent != "engineering_distance_lookup":
        return False
    title = row["title"] or ""
    if plan.candidate_title_terms and not any(term in title for term in plan.candidate_title_terms):
        return False
    section = row["section_path"] or ""
    text = row["text"] or ""
    context = f"{section}\n{text}"
    if "工程间距" not in context or not any(term in context for term in ("表 F.1", "表F.1", "参考基本勘查工程间距")):
        return False
    if not plan.target_exploration_type:
        return True
    if table_has_exploration_type(row["table_json"], plan.target_exploration_type):
        return True

    markers = {
        "Ⅰ": ("I", "Ⅰ", "工", "1"),
        "Ⅱ": ("II", "Ⅱ", "2"),
        "Ⅲ": ("III", "Ⅲ", "3"),
    }[plan.target_exploration_type]
    marker_pattern = "|".join(re.escape(marker) for marker in markers)
    distance_pattern = r"\d+(?:\.\d+)?\s*[~～-]\s*\d+(?:\.\d+)?"
    return bool(
        re.search(
            rf"(?:^|\n)\s*(?:{marker_pattern})\s*(?:\n|\t|\|)+\s*{distance_pattern}",
            text,
            flags=re.IGNORECASE,
        )
    )


STRICT_EVIDENCE_INTENTS = {
    "engineering_distance_lookup",
    "service_materials",
    "service_procedure_basis",
    "service_time_limit",
    "projection_numeric_rule",
    "legal_responsibility",
    "authority_responsibility",
    "exploration_to_mining_eligibility",
    "companion_resource_type",
    "exploration_type_factors",
    "basic_analysis_items",
}


def row_context(row: sqlite3.Row) -> str:
    return " ".join(
        str(row[key] or "")
        for key in ("title", "standard_no", "section_path", "clause_no", "text")
    )


def evidence_group_match_count(row: sqlite3.Row, plan: QueryPlan) -> int:
    context = row_context(row)
    return sum(
        1
        for group in plan.required_evidence_groups
        if any(term and term in context for term in group)
    )


def row_matches_required_evidence_groups(row: sqlite3.Row, plan: QueryPlan) -> bool:
    if not plan.required_evidence_groups:
        return True
    return evidence_group_match_count(row, plan) == len(plan.required_evidence_groups)


def negative_term_penalty(row: sqlite3.Row, plan: QueryPlan) -> float:
    if not plan.negative_terms:
        return 0.0
    context = row_context(row)
    return min(18.0, sum(4.5 for term in plan.negative_terms if term and term in context))


def row_matches_candidate_title(row: sqlite3.Row, plan: QueryPlan) -> bool:
    if not plan.candidate_title_terms:
        return True
    title = row["title"] or ""
    return any(term in title for term in plan.candidate_title_terms)


def service_application_section_terms(plan: QueryPlan) -> tuple[str, ...]:
    query = plan.normalized_query
    if any(term in query for term in ("延续", "续期")):
        return ("附件4 > 延续 >",)
    if "注销" in query:
        return ("附件4 > 注销 >",)
    if any(term in query for term in ("首次", "新立")):
        return ("附件4 > 新立 >",)
    if "变更" in query or any(term in query for term in ("转让", "转移")):
        if "扩大" in query:
            return ("附件4 > 变更 > 扩大矿区范围 >",)
        if "缩小" in query:
            return ("附件4 > 变更 > 缩小矿区范围 >",)
        if any(term in query for term in ("开采矿种", "开采主矿种", "开采方式")):
            return ("附件4 > 变更 > 开采主矿种、开采方式 >",)
        if "采矿权人名称" in query:
            return ("附件4 > 变更 > 采矿权人名称 >",)
        if any(term in query for term in ("转让", "转移")):
            return ("附件4 > 变更 > 转让 >",)
    return ()


def row_has_service_material_evidence(row: sqlite3.Row, plan: QueryPlan) -> bool:
    title = row["title"] or ""
    standard_no = row["standard_no"] or ""
    section = row["section_path"] or ""
    text = row["text"] or ""
    context = f"{title} {section} {text}"
    if row["document_type"] in {"service_guide", "administrative_service_guide"}:
        return (
            row["validation_status"] != "empty_source_section"
            and row_matches_candidate_title(row, plan)
            and (section == "申请材料" or section.startswith("申请材料 >"))
        )
    if row["document_type"] == "policy_attachment":
        section_terms = service_application_section_terms(plan)
        return (
            row["validation_status"] != "empty_source_section"
            and row_matches_candidate_title(row, plan)
            and row["chunk_type"] == "application_material_row"
            and bool(section_terms)
            and any(section.startswith(term) for term in section_terms)
        )
    return (
        "自然资规〔2023〕4号" in standard_no
        and "采矿权申请资料清单" in context
        and "延续" in context
        and "附件4" in context
    )


def row_has_service_procedure_evidence(row: sqlite3.Row, plan: QueryPlan) -> bool:
    title = row["title"] or ""
    standard_no = row["standard_no"] or ""
    section = row["section_path"] or ""
    text = row["text"] or ""
    context = f"{title} {section} {text}"
    if row["document_type"] in {"service_guide", "administrative_service_guide"}:
        return (
            row["validation_status"] != "empty_source_section"
            and row_matches_candidate_title(row, plan)
            and any(term in section for term in ("办理基本流程", "办理方式", "申请材料提交"))
        )
    return (
        "自然资规〔2023〕4号" in standard_no
        and "矿产资源勘查开采登记管理" in title
        and "采矿权" in context
        and any(term in context for term in ("申请资料", "登记管理", "附件4"))
    )


def row_has_service_time_limit_evidence(row: sqlite3.Row, plan: QueryPlan) -> bool:
    if row["document_type"] not in {"service_guide", "administrative_service_guide"}:
        return False
    return (
        row["validation_status"] != "empty_source_section"
        and row_matches_candidate_title(row, plan)
        and "办结时限" in (row["section_path"] or "")
        and bool((row["text"] or "").strip())
    )


def row_has_projection_numeric_evidence(row: sqlite3.Row) -> bool:
    standard_no = (row["standard_no"] or "").replace(" ", "").upper()
    clause = row["clause_no"] or ""
    text = re.sub(r"\s+", "", row["text"] or "")
    return (
        standard_no == "DZ/T0338.1-2020"
        and (clause == "6.2.2.1" or "6.2.2.1" in text)
        and "无限外推" in text
        and "经验工程间距1/2尖推" in text
    )


def row_has_legal_responsibility_evidence(row: sqlite3.Row) -> bool:
    standard_no = row["standard_no"] or ""
    clause = row["clause_no"] or ""
    text = re.sub(r"\s+", "", row["text"] or "")
    return (
        "国令第839号" in standard_no
        and (clause == "第四十三条" or "第四十三条" in text)
        and "矿业权人" in text
        and "储量报告的真实性负责" in text
        and "不得弄虚作假" in text
    )


def row_has_authority_evidence(row: sqlite3.Row) -> bool:
    text = re.sub(r"\s+", "", row["text"] or "")
    return (
        "自然资源部负责本级已颁发勘查许可证或采矿许可证" in text
        and "其他由省级自然资源主管部门负责" in text
    )


def row_has_transfer_eligibility_evidence(row: sqlite3.Row) -> bool:
    standard_no = (row["standard_no"] or "").replace(" ", "")
    text = re.sub(r"\s+", "", row["text"] or "")
    policy = (
        "自然资规〔2023〕4号" in standard_no
        and "探矿权转采矿权" in text
        and "经评审备案的矿产资源储量报告" in text
        and "详查（含）以上程度" in text
    )
    report_limit = (
        standard_no.upper() == "DZ/T0430-2023"
        and "不能替代探矿权转采矿权时应提交的地质勘查报告" in text
    )
    return policy or report_limit


def row_has_companion_resource_type_evidence(row: sqlite3.Row) -> bool:
    standard_no = (row["standard_no"] or "").replace(" ", "").upper()
    clause = row["clause_no"] or ""
    text = re.sub(r"\s+", "", row["text"] or "")
    if standard_no != "GB/T25283-2023":
        return False
    return clause in {"9.2", "9.3", "9.4"} or any(
        marker in text
        for marker in (
            "9.2当伴生矿产进行了基本分析",
            "9.3当伴生矿产进行了基本分析但未能满足",
            "9.4伴生矿产只进行了组合分析",
        )
    )


def row_has_exploration_factor_evidence(row: sqlite3.Row) -> bool:
    standard_no = (row["standard_no"] or "").replace(" ", "").upper()
    section = re.sub(r"\s+", "", row["section_path"] or "")
    clause = re.sub(r"\s+", "", row["clause_no"] or "")
    text = re.sub(r"\s+", "", row["text"] or "")
    if standard_no != "DZ/T0205-2020":
        return False
    return bool(
        re.search(r"表E\.[1-5](?:\D|$)", section)
        or clause == "E.1"
        or "矿床勘查类型划分因素见表E.1至表E.5" in text
    )


def row_has_basic_analysis_evidence(row: sqlite3.Row, plan: QueryPlan) -> bool:
    context = row_context(row)
    if "基本分析" not in context or "分析项目" not in context:
        return False
    if plan.standard_numbers:
        expected = {number.replace(" ", "").upper() for number in plan.standard_numbers}
        if (row["standard_no"] or "").replace(" ", "").upper() not in expected:
            return False
    return True


def row_matches_query_plan_evidence(row: sqlite3.Row, plan: QueryPlan) -> bool:
    if plan.intent == "engineering_distance_lookup":
        return row_has_engineering_distance_evidence(row, plan)
    if plan.intent == "service_materials":
        return row_has_service_material_evidence(row, plan)
    if plan.intent == "service_procedure_basis":
        return row_has_service_procedure_evidence(row, plan)
    if plan.intent == "service_time_limit":
        return row_has_service_time_limit_evidence(row, plan)
    if plan.intent == "projection_numeric_rule":
        return row_has_projection_numeric_evidence(row)
    if plan.intent == "legal_responsibility":
        return row_has_legal_responsibility_evidence(row)
    if plan.intent == "authority_responsibility":
        return row_has_authority_evidence(row)
    if plan.intent == "exploration_to_mining_eligibility":
        return row_has_transfer_eligibility_evidence(row)
    if plan.intent == "companion_resource_type":
        return row_has_companion_resource_type_evidence(row)
    if plan.intent == "exploration_type_factors":
        return row_has_exploration_factor_evidence(row)
    if plan.intent == "basic_analysis_items":
        return row_has_basic_analysis_evidence(row, plan)
    return row_matches_required_evidence_groups(row, plan)


def query_plan_score(row: sqlite3.Row, plan: QueryPlan) -> float:
    score = 0.0
    title = row["title"] or ""
    section = row["section_path"] or ""
    text = row["text"] or ""
    context = f"{title} {section} {text}"
    if plan.document_types:
        score += 3.0 if row["document_type"] in plan.document_types else -10.0
    score += sum(2.0 for term in plan.subject_terms if term and term in context)
    score += sum(2.5 for term in plan.required_terms if term and term in context)
    score += sum(1.0 for term in plan.alternative_terms if term and term in context)
    if plan.required_evidence_groups:
        matched_groups = evidence_group_match_count(row, plan)
        score += matched_groups * 3.0
        if matched_groups == len(plan.required_evidence_groups):
            score += 8.0
    score -= negative_term_penalty(row, plan)
    if plan.candidate_title_terms:
        if any(term in title for term in plan.candidate_title_terms):
            score += 8.0
        elif plan.has_hard_candidate_scope:
            score -= 6.0
    if plan.output_mode == "table" and row["chunk_type"] == "table":
        score += 12.0
    if plan.intent == "engineering_distance_lookup":
        if any(term in f"{section} {text}" for term in ("表 F.1", "表F.1", "参考基本勘查工程间距")):
            score += 6.0
        if row["chunk_type"] == "table":
            score += 8.0
            if table_has_exploration_type(row["table_json"], plan.target_exploration_type):
                score += 10.0
        if row_has_engineering_distance_evidence(row, plan):
            score += 10.0
    elif plan.intent == "service_materials":
        if row["document_type"] == "policy_attachment":
            score += 18.0
        if row["document_type"] in {"service_guide", "administrative_service_guide"}:
            score += 10.0
        if "自然资规〔2023〕4号" in (row["standard_no"] or ""):
            score += 7.0
        if section == "申请材料" or section.startswith("申请材料 >"):
            score += 7.0
        if row["chunk_type"] == "table":
            score += 7.0
        if row["chunk_type"] == "application_material_row":
            score += 16.0
        if any(section.startswith(term) for term in service_application_section_terms(plan)):
            score += 12.0
        if row_has_service_material_evidence(row, plan):
            score += 12.0
        if "自然资规〔2023〕6号" in (row["standard_no"] or ""):
            score -= 10.0
    elif plan.intent == "service_procedure_basis":
        if row["document_type"] in {"service_guide", "administrative_service_guide"}:
            score += 10.0
        if "自然资规〔2023〕4号" in (row["standard_no"] or ""):
            score += 8.0
        if "矿产资源勘查开采登记管理" in title:
            score += 5.0
        if "办理基本流程" in section:
            score += 10.0
        elif "办理方式" in section:
            score += 7.0
        elif "申请材料提交" in section:
            score += 6.0
        if row_has_service_procedure_evidence(row, plan):
            score += 12.0
        if "自然资规〔2023〕6号" in (row["standard_no"] or ""):
            score -= 10.0
    elif plan.intent == "service_time_limit":
        if row["document_type"] in {"service_guide", "administrative_service_guide"}:
            score += 10.0
        if "办结时限" in section:
            score += 12.0
        if row_has_service_time_limit_evidence(row, plan):
            score += 12.0
    elif plan.intent == "projection_numeric_rule":
        if (row["standard_no"] or "").replace(" ", "").upper() == "DZ/T0338.1-2020":
            score += 8.0
        if row["clause_no"] == "6.2.2.1":
            score += 8.0
        if row_has_projection_numeric_evidence(row):
            score += 15.0
    elif plan.intent == "legal_responsibility":
        if "国令第839号" in (row["standard_no"] or ""):
            score += 8.0
        if row["clause_no"] == "第四十三条":
            score += 8.0
        if row_has_legal_responsibility_evidence(row):
            score += 15.0
        if "评审备案范围和权限" in f"{section} {text}":
            score -= 12.0
    elif plan.intent == "authority_responsibility" and row_has_authority_evidence(row):
        score += 12.0
    elif plan.intent == "exploration_to_mining_eligibility" and row_has_transfer_eligibility_evidence(row):
        score += 18.0
    elif plan.intent == "companion_resource_type" and row_has_companion_resource_type_evidence(row):
        score += 18.0
    elif plan.intent == "exploration_type_factors" and row_has_exploration_factor_evidence(row):
        score += 18.0
    elif plan.intent == "basic_analysis_items" and row_has_basic_analysis_evidence(row, plan):
        score += 18.0
    elif plan.intent == "related_documents" and plan.focus_terms:
        anchor = max(plan.focus_terms, key=len)
        if anchor in context:
            score += 10.0
        else:
            score -= 8.0
        score += sum(2.0 for term in plan.focus_terms if term != anchor and term in context)
    return score


def lexical_score(row: sqlite3.Row, query: str, idx: int, plan: QueryPlan | None = None) -> float:
    terms = query_terms(query, plan)
    title = row["title"] or ""
    standard_no = row["standard_no"] or ""
    section = row["section_path"] or ""
    text = row["text"] or ""
    score = max(0.1, 1.0 - idx * 0.005)
    for term in terms:
        if term in standard_no:
            score += 5.0
        if term in title:
            score += 4.0
        if term in section:
            score += 2.5
        if term in text:
            score += 1.0
    if row["chunk_type"] == "table":
        score += 0.5
        if not re.search(r"表|分型|划分|指标|间距|规模|分类", query):
            score -= 1.2
    for critical in ["起草单位", "发布", "实施", "代替", "替代"]:
        if critical in query and critical not in text:
            score -= 3.0
        elif critical in query and critical in text:
            score += 3.0
    if ("哪个标准" in query or "哪个规范" in query) and any(term in title for term in terms):
        score += 2.0
    if any(term in query for term in ("金矿", "岩金")):
        if "岩金" in title:
            score += 4.0
        elif "金属砂矿" in title or "冶金矿山" in title:
            score -= 3.0
    return score


def intent_score(row: sqlite3.Row, query: str, plan: QueryPlan | None = None) -> float:
    score = 0.0
    source_type = row["source_type"] or ""
    document_type = row["document_type"] or ""
    if is_policy_management_query(query):
        if source_type == "official_fulltext":
            score += 3.5
        if document_type in {"law", "regulation", "department_rule", "policy_document"}:
            score += 1.5
        if source_type == "local_kb" and not is_standard_or_technical_query(query):
            score -= 1.0
    if is_standard_or_technical_query(query):
        if source_type == "local_kb":
            score += 1.5
        if document_type in {"standard", "national_standard", "industry_standard", "guidance"}:
            score += 1.0
    if (plan and plan.intent == "authority_responsibility") or is_policy_authority_query(query):
        text = row["text"] or ""
        title = row["title"] or ""
        standard_no = row["standard_no"] or ""
        section = row["section_path"] or ""
        if source_type == "official_fulltext":
            score += 2.0
        if "自然资规〔2023〕6号" in standard_no:
            score += 5.0
        if "深化矿产资源管理改革若干事项" in title:
            score += 2.0
        if "明确评审备案范围和权限" in section or row["clause_no"] == "十、":
            score += 4.0
        if "自然资源部负责本级已颁发勘查许可证或采矿许可证" in text:
            score += 10.0
        if "其他由省级自然资源主管部门负责" in text:
            score += 3.0
        if "矿产资源储量评审备案" in text and "负责" in text:
            score += 2.0
        evidence_text = " ".join([title, standard_no, section, text])
        is_target_authority_evidence = (
            "自然资源部负责本级已颁发勘查许可证或采矿许可证" in text
            or "明确评审备案范围和权限" in section
            or row["clause_no"] == "十、"
        )
        for negative in lexicon_negative_terms(query, intent_label="authority_responsibility"):
            if negative and negative not in query and negative in evidence_text and not is_target_authority_evidence:
                score -= 3.0
        if any(term in query for term in ("大型金矿", "金矿", "固体矿产")):
            if any(term in evidence_text for term in ("油气", "煤层气")) and not is_target_authority_evidence:
                score -= 30.0
    return score


def row_to_document(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "document_id": row["document_id"],
        "title": row["title"],
        "standard_no": row["standard_no"],
        "document_type": row["document_type"],
        "status": row["status"],
        "source_type": row["source_type"],
        "text_access": row["text_access"],
        "validation_status": row["validation_status"],
        "visibility": row["visibility"],
        "publish_date": row["publish_date"],
        "implementation_date": row["implementation_date"],
        "ingestion_time": row["ingestion_time"],
        "can_answer": bool(row["can_answer"]),
        "url": row["official_url"],
        "source_platform": row["source_platform"],
    }


def source_role(row: sqlite3.Row) -> str:
    document_type = row["document_type"] or ""
    if document_type == "policy_attachment":
        return "policy_attachment"
    if document_type in {"service_guide", "administrative_service_guide"}:
        return "service_guide"
    if row["standard_no"] == "自然资规〔2023〕4号":
        return "parent_policy"
    if document_type in {"law", "regulation", "department_rule", "policy_document"}:
        return "policy_document"
    return "standard_or_other"


def table_count(conn: sqlite3.Connection, table_name: str) -> int:
    exists = conn.execute(
        "select 1 from sqlite_master where type in ('table', 'virtual table') and name = ?",
        (table_name,),
    ).fetchone()
    if not exists:
        return 0
    return int(conn.execute(f"select count(*) from {table_name}").fetchone()[0])


class KnowledgeStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = db_path
        init_db(db_path)

    def health(self) -> dict[str, Any]:
        with connect(self.db_path) as conn:
            doc_count = conn.execute("select count(*) from documents").fetchone()[0]
            chunk_count = conn.execute("select count(*) from chunks").fetchone()[0]
            candidate_count = conn.execute("select count(*) from candidates").fetchone()[0]
            vector_count = table_count(conn, "chunk_vectors")
            embedding_count = table_count(conn, "chunk_embeddings")
            kg_entity_count = table_count(conn, "kg_entities")
            kg_relation_count = table_count(conn, "kg_relations")
        settings = get_settings()
        ann_manifest = None
        try:
            ann_manifest = get_ann_index(settings.ann_index_path, settings.ann_manifest_path).manifest()
        except (OSError, ValueError, KeyError):
            ann_manifest = None
        return {
            "ok": True,
            "service": "mining-knowledge-base",
            "storage": "sqlite_fts5",
            "db_path": str(self.db_path),
            "document_count": doc_count,
            "chunk_count": chunk_count,
            "candidate_count": candidate_count,
            "vector_count": vector_count,
            "embedding_count": embedding_count,
            "kg_entity_count": kg_entity_count,
            "kg_relation_count": kg_relation_count,
            "ann_available": bool(ann_manifest),
            "ann_count": ann_manifest.count if ann_manifest else 0,
            "ann_model": ann_manifest.model if ann_manifest else None,
            "ann_dtype": ann_manifest.dtype if ann_manifest else None,
        }

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        started = perf_counter()
        query = str(payload.get("query") or "").strip()
        plan = query_plan_from_payload(query, payload.get("retrieval_plan"))
        retrieval_query = plan.retrieval_query or plan.normalized_query or query
        filters = payload.get("filters") or {}
        options = payload.get("options") or {}
        top_k = int(options.get("top_k") or 10)
        top_k = max(1, min(top_k, 50))
        recall_limit = min(240, max(top_k * 4, 60))
        include_full_text = bool(options.get("include_full_text"))
        retrieval_round = max(1, int(options.get("retrieval_round") or 1))

        base_where = ["d.visibility in ('internal', 'public')"]
        base_params: list[Any] = []
        standard_no = filters.get("standard_no")
        if standard_no:
            base_where.append("d.standard_no = ?")
            base_params.append(standard_no)
        statuses = filters.get("status") or []
        if isinstance(statuses, str):
            statuses = [statuses]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            base_where.append(f"d.status in ({placeholders})")
            base_params.extend(statuses)
        requested_doc_types = filters.get("document_types") or []
        if isinstance(requested_doc_types, str):
            requested_doc_types = [requested_doc_types]
        planned_doc_types = list(plan.document_types)
        if requested_doc_types and planned_doc_types:
            doc_types = [value for value in requested_doc_types if value in planned_doc_types]
            if not doc_types:
                base_where.append("1 = 0")
        else:
            doc_types = requested_doc_types or planned_doc_types
        if doc_types:
            placeholders = ",".join("?" for _ in doc_types)
            base_where.append(f"d.document_type in ({placeholders})")
            base_params.extend(doc_types)

        scope_document_ids: list[str] = []
        scope_applied = False
        vector_ran = False
        vector_result = VectorCandidateResult()
        lexical_graph_ms = 0.0
        vector_ms = 0.0
        with connect(self.db_path) as conn:
            where, params, scope_document_ids = self._candidate_scope(
                conn,
                plan,
                base_where,
                base_params,
            )
            scope_applied = bool(scope_document_ids)
            lexical_started = perf_counter()
            candidate_rows = self._lexical_and_graph_candidates(
                conn,
                retrieval_query,
                plan,
                where,
                params,
                recall_limit,
            )
            lexical_graph_ms += (perf_counter() - lexical_started) * 1000

            if scope_applied and not self._route_evidence_found(candidate_rows, plan):
                where = list(base_where)
                params = list(base_params)
                scope_document_ids = []
                scope_applied = False
                lexical_started = perf_counter()
                candidate_rows = self._lexical_and_graph_candidates(
                    conn,
                    retrieval_query,
                    plan,
                    where,
                    params,
                    recall_limit,
                )
                lexical_graph_ms += (perf_counter() - lexical_started) * 1000

            if not self._evidence_sufficient_without_vectors(candidate_rows, plan, scope_applied):
                vector_ran = True
                vector_started = perf_counter()
                vector_result = self._vector_candidates(
                    conn,
                    retrieval_query,
                    plan,
                    where,
                    params,
                    min(recall_limit, 120),
                )
                vector_ms = (perf_counter() - vector_started) * 1000
                for rank, (row, score) in enumerate(vector_result.candidates, start=1):
                    self._add_candidate(candidate_rows, row, "vector", rank, score)

            self._add_referenced_table_candidates(
                conn,
                candidate_rows,
                plan,
                base_where,
                base_params,
            )

        items = []
        candidates = list(candidate_rows.values())
        for candidate in candidates:
            candidate["final_score"] = self._candidate_fusion_score(candidate, plan)
        ranked = sorted(
            enumerate(candidates),
            key=lambda item: item[1]["final_score"],
            reverse=True,
        )
        ranked = [item for item in ranked if item[1]["final_score"] > 0.05]
        if plan.intent in STRICT_EVIDENCE_INTENTS:
            evidence_ranked = [
                item for item in ranked if row_matches_query_plan_evidence(item[1]["row"], plan)
            ]
            if evidence_ranked:
                ranked = evidence_ranked
        if plan.intent == "engineering_distance_lookup":
            ranked.sort(
                key=lambda item: (
                    item[1]["row"]["chunk_type"] == "table"
                    and table_has_exploration_type(
                        item[1]["row"]["table_json"],
                        plan.target_exploration_type,
                    ),
                    item[1]["final_score"],
                ),
                reverse=True,
            )
        if plan.output_mode == "table":
            ranked.sort(
                key=lambda item: (
                    item[1]["row"]["chunk_type"] == "table",
                    "reference" in item[1]["hit_types"],
                    item[1]["final_score"],
                ),
                reverse=True,
            )
        if plan.intent in {"general", "regulation_lookup"} and not plan.standard_numbers:
            ranked = self._diversify_documents(ranked)
        if is_standard_selection_query(plan.normalized_query) and ranked:
            _, top_candidate = ranked[0]
            top_row = top_candidate["row"]
            platform = top_row["source_platform"] or "官方标准平台"
            items.append(
                {
                    "chunk_id": None,
                    "document_id": top_row["document_id"],
                    "title": top_row["title"],
                    "standard_no": top_row["standard_no"],
                    "section_path": "标准目录",
                    "clause_no": None,
                    "page_start": None,
                    "page_end": None,
                    "page": None,
                    "quote": f"标准目录命中：{top_row['standard_no'] or ''}《{top_row['title']}》。官方来源平台：{platform}。",
                    "score": 0.99,
                    "hit_type": ["catalog"],
                    "source_type": "official_metadata",
                    "text_access": "metadata_only",
                    "validation_status": "catalog_matched",
                    "url": top_row["official_url"],
                    "source_platform": top_row["source_platform"],
                    "document_type": top_row["document_type"],
                    "source_role": source_role(top_row),
                }
            )

        for idx, (_, candidate) in enumerate(ranked[:top_k], 1):
            row = candidate["row"]
            score = min(0.99, max(0.05, float(candidate["final_score"])))
            structured_quote, structured_clause = structured_intent_quote(row, plan)
            compact_quote = structured_quote or (
                table_quote(
                    row["table_json"],
                    row["text"],
                    plan.normalized_query,
                    limit=1800 if plan.output_mode == "table" else QUOTE_LIMIT,
                    plan=plan,
                )
                if row["chunk_type"] == "table"
                else quote_text(row["text"], plan.retrieval_query)
            )
            item = {
                "chunk_id": row["chunk_id"],
                "document_id": row["document_id"],
                "title": row["title"],
                "standard_no": row["standard_no"],
                "section_path": row["section_path"],
                "clause_no": structured_clause or row["clause_no"],
                "page_start": row["page_start"],
                "page_end": row["page_end"],
                "page": row["page_start"],
                "quote": compact_quote,
                "evidence_text": structured_quote or (
                    table_quote(
                        row["table_json"],
                        row["text"],
                        plan.retrieval_query,
                        limit=1800 if plan.output_mode == "table" else 800,
                        plan=plan,
                    )
                    if row["chunk_type"] == "table"
                    else quote_text(row["text"], plan.retrieval_query, limit=800, max_sentences=6)
                ),
                "score": round(score, 4),
                "hit_type": sorted(candidate["hit_types"]),
                "source_type": row["source_type"],
                "text_access": row["text_access"],
                "validation_status": row["validation_status"],
                "url": row["official_url"],
                "source_platform": row["source_platform"],
                "document_type": row["document_type"],
                "source_role": source_role(row),
            }
            if include_full_text:
                item["text"] = row["text"]
            items.append(item)

        route_evidence_found = self._route_evidence_found(candidate_rows, plan)
        direct_rows = [
            candidate["row"]
            for candidate in candidate_rows.values()
            if row_matches_query_plan_evidence(candidate["row"], plan)
        ]
        direct_documents = {str(row["document_id"]) for row in direct_rows}
        comparison = plan.search_mode in {"comparison", "exhaustive"} or plan.intent in {
            "projection_comparison",
            "clause_comparison",
        }
        has_hits = route_evidence_found if plan.intent in STRICT_EVIDENCE_INTENTS else bool(items)
        if plan.required_evidence_groups:
            has_hits = bool(direct_rows) and (not comparison or len(direct_documents) >= 2)
        has_clause_level_evidence = any(
            item.get("clause_no")
            or (
                item.get("chunk_id")
                and item.get("section_path")
                and any(marker in str(item["section_path"]) for marker in ("表", "附录"))
            )
            for item in items
        )
        if plan.intent in STRICT_EVIDENCE_INTENTS:
            has_clause_level_evidence = route_evidence_found
        if plan.required_evidence_groups:
            has_clause_level_evidence = has_hits
        total_ms = (perf_counter() - started) * 1000
        return {
            "query": query,
            "results": items,
            "retrieval": {
                "full_text_hits": sum(1 for c in candidates if "full_text" in c["hit_types"]),
                "vector_hits": sum(1 for c in candidates if "vector" in c["hit_types"]),
                "graph_hits": sum(1 for c in candidates if "graph" in c["hit_types"]),
                "web_hits": 0,
                "scoped_search": int(scope_applied),
                "vector_skipped": int(not vector_ran),
                "direct_evidence_hits": len(direct_rows),
                "candidate_count": len(candidates),
                "ann_used": int(vector_result.route == "ann"),
                "vector_route": vector_result.route,
                "retrieval_round": retrieval_round,
                "timings_ms": {
                    "lexical_graph": round(lexical_graph_ms, 3),
                    "embedding": round(vector_result.embedding_ms, 3),
                    "vector_search": round(vector_result.search_ms, 3),
                    "vector_total": round(vector_ms, 3),
                    "total": round(total_ms, 3),
                },
            },
            "coverage": {
                "has_clause_level_evidence": has_clause_level_evidence,
                "has_page_level_evidence": any(item.get("page_start") for item in items),
                "needs_web_supplement": not has_hits,
                "notes": [] if has_hits else ["本地知识库未命中可引用证据，建议进入联网补齐候选流程。"],
                "query_plan": {
                    "normalized_query": plan.normalized_query,
                    "intent": plan.intent,
                    "target_exploration_type": plan.target_exploration_type,
                    "candidate_document_ids": scope_document_ids,
                    "exhaustive_search": plan.exhaustive_search,
                    "planner_used": plan.planner_used,
                    "search_mode": plan.search_mode,
                    "required_evidence_groups": plan.required_evidence_groups,
                },
            },
        }

    def _candidate_scope(
        self,
        conn: sqlite3.Connection,
        plan: QueryPlan,
        base_where: list[str],
        base_params: list[Any],
    ) -> tuple[list[str], list[Any], list[str]]:
        if not plan.has_hard_candidate_scope:
            return list(base_where), list(base_params), []
        if plan.search_mode in {"comparison", "exhaustive"} and not plan.standard_numbers:
            return list(base_where), list(base_params), []

        scope_terms: list[str] = []
        scope_params: list[Any] = []
        for title_term in plan.candidate_title_terms:
            scope_terms.append("d.title like ?")
            scope_params.append(f"%{title_term}%")
        for number in plan.standard_numbers:
            scope_terms.append("replace(upper(d.standard_no), ' ', '') = ?")
            scope_params.append(number.upper().replace(" ", ""))
        if not scope_terms:
            return list(base_where), list(base_params), []

        rows = conn.execute(
            f"""
            select d.document_id
            from documents d
            where {' and '.join(base_where)} and ({' or '.join(scope_terms)})
            order by d.updated_at desc
            limit 20
            """,
            [*base_params, *scope_params],
        ).fetchall()
        document_ids = [str(row["document_id"]) for row in rows]
        if not document_ids:
            return list(base_where), list(base_params), []

        placeholders = ",".join("?" for _ in document_ids)
        return (
            [*base_where, f"d.document_id in ({placeholders})"],
            [*base_params, *document_ids],
            document_ids,
        )

    def _full_text_candidates(
        self,
        conn: sqlite3.Connection,
        query: str,
        plan: QueryPlan,
        where: list[str],
        params: list[Any],
        recall_limit: int,
    ) -> list[sqlite3.Row]:
        results: list[sqlite3.Row] = []
        seen: set[str] = set()

        def append_match(match: str, limit: int) -> None:
            if not match or len(results) >= recall_limit:
                return
            sql = f"""
                select c.*, d.document_type, d.status, d.official_url, d.source_platform, bm25(chunks_fts) as rank
                from chunks_fts
                join chunks c on c.chunk_id = chunks_fts.chunk_id
                join documents d on d.document_id = c.document_id
                where chunks_fts match ? and {' and '.join(where)}
                order by rank
                limit ?
            """
            try:
                rows = conn.execute(sql, [match, *params, limit]).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for row in rows:
                if row["chunk_id"] in seen:
                    continue
                results.append(row)
                seen.add(row["chunk_id"])
                if len(results) >= recall_limit:
                    break

        strict_budget = min(recall_limit, max(20, recall_limit // 2))
        append_match(fts_query(query, plan, strict=True), strict_budget)
        append_match(fts_query(query, plan), recall_limit)
        if len(results) >= recall_limit or not query:
            return results

        like_terms = query_terms(query, plan)[:10] or [query]
        like_where = [
            "(" + " or ".join(["c.text like ? or c.title like ? or c.standard_no like ?" for _ in like_terms]) + ")"
        ]
        like_params: list[Any] = []
        for term in like_terms:
            pattern = f"%{term}%"
            like_params.extend([pattern, pattern, pattern])
        sql = f"""
            select c.*, d.document_type, d.status, d.official_url, d.source_platform, 0.0 as rank
            from chunks c
            join documents d on d.document_id = c.document_id
            where c.validation_status != 'empty_source_section' and {' and '.join(where + like_where)}
            order by length(c.text) asc
            limit ?
        """
        for row in conn.execute(sql, [*params, *like_params, recall_limit]).fetchall():
            if row["chunk_id"] not in seen:
                results.append(row)
                seen.add(row["chunk_id"])
            if len(results) >= recall_limit:
                break
        return results

    def _lexical_and_graph_candidates(
        self,
        conn: sqlite3.Connection,
        query: str,
        plan: QueryPlan,
        where: list[str],
        params: list[Any],
        recall_limit: int,
    ) -> dict[str, dict[str, Any]]:
        candidate_rows: dict[str, dict[str, Any]] = {}
        for rank, row in enumerate(
            self._full_text_candidates(conn, query, plan, where, params, recall_limit),
            start=1,
        ):
            bm25_rank = float(row["rank"] or 0.0)
            self._add_candidate(candidate_rows, row, "full_text", rank, 1.0 / (1.0 + abs(bm25_rank)))
        for rank, (row, score) in enumerate(
            self._graph_candidates(conn, query, plan, where, params, recall_limit),
            start=1,
        ):
            self._add_candidate(candidate_rows, row, "graph", rank, score)
        return candidate_rows

    def _add_candidate(
        self,
        candidate_rows: dict[str, dict[str, Any]],
        row: sqlite3.Row,
        hit_type: str,
        rank: int,
        route_score: float,
    ) -> None:
        candidate = candidate_rows.setdefault(
            row["chunk_id"],
            {
                "row": row,
                "hit_types": set(),
                "route_ranks": {},
                "route_scores": {},
                "order": len(candidate_rows),
            },
        )
        candidate["hit_types"].add(hit_type)
        candidate["route_ranks"][hit_type] = min(
            int(candidate["route_ranks"].get(hit_type, rank)),
            max(1, rank),
        )
        candidate["route_scores"][hit_type] = max(
            float(candidate["route_scores"].get(hit_type, 0.0)),
            float(route_score),
        )

    def _add_referenced_table_candidates(
        self,
        conn: sqlite3.Connection,
        candidate_rows: dict[str, dict[str, Any]],
        plan: QueryPlan,
        base_where: list[str],
        base_params: list[Any],
    ) -> None:
        if plan.output_mode != "table" or not candidate_rows:
            return

        expected_numbers = {number.replace(" ", "").upper() for number in plan.standard_numbers}
        target_document_ids = {
            str(candidate["row"]["document_id"])
            for candidate in candidate_rows.values()
            if (
                not expected_numbers
                or (candidate["row"]["standard_no"] or "").replace(" ", "").upper() in expected_numbers
            )
        }
        if not target_document_ids:
            target_document_ids = {
                str(candidate["row"]["document_id"])
                for candidate in candidate_rows.values()
            }

        reference_context = " ".join(
            [
                plan.retrieval_query,
                *(
                    row_context(candidate["row"])
                    for candidate in candidate_rows.values()
                    if str(candidate["row"]["document_id"]) in target_document_ids
                ),
            ]
        )
        references = table_references(reference_context)
        if not references:
            return

        document_placeholders = ",".join("?" for _ in target_document_ids)
        reference_where = " or ".join(
            "replace(upper(c.section_path), ' ', '') like ?" for _ in references
        )
        rows = conn.execute(
            f"""
            select c.*, d.document_type, d.status, d.official_url, d.source_platform, 0.0 as rank
            from chunks c
            join documents d on d.document_id = c.document_id
            where {' and '.join(base_where)}
              and c.document_id in ({document_placeholders})
              and c.chunk_type = 'table'
              and ({reference_where})
            order by c.section_path
            """,
            [
                *base_params,
                *sorted(target_document_ids),
                *(f"%表{reference}%" for reference in references),
            ],
        ).fetchall()
        for rank, row in enumerate(rows, start=1):
            self._add_candidate(candidate_rows, row, "reference", rank, 1.0)

    def _candidate_fusion_score(self, candidate: dict[str, Any], plan: QueryPlan) -> float:
        row = candidate["row"]
        rrf = sum(
            ROUTE_WEIGHTS.get(route, 1.0) / (RRF_K + int(rank))
            for route, rank in candidate["route_ranks"].items()
        )
        max_rrf = sum(ROUTE_WEIGHTS.values()) / (RRF_K + 1)
        route_score = min(1.0, rrf / max_rrf) if max_rrf else 0.0
        heuristic_raw = (
            lexical_score(row, plan.normalized_query, candidate["order"], plan)
            + intent_score(row, plan.normalized_query, plan)
            + query_plan_score(row, plan)
        )
        heuristic_score = 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, heuristic_raw)) / 8.0))
        direct_bonus = 0.08 if row_matches_query_plan_evidence(row, plan) else 0.0
        return min(0.99, max(0.0, route_score * 0.68 + heuristic_score * 0.32 + direct_bonus))

    @staticmethod
    def _diversify_documents(ranked: list[tuple[int, dict[str, Any]]]) -> list[tuple[int, dict[str, Any]]]:
        first_per_document: list[tuple[int, dict[str, Any]]] = []
        remaining: list[tuple[int, dict[str, Any]]] = []
        seen: set[str] = set()
        for item in ranked:
            document_id = str(item[1]["row"]["document_id"] or "")
            if document_id not in seen:
                first_per_document.append(item)
                seen.add(document_id)
            else:
                remaining.append(item)
        return [*first_per_document, *remaining]

    def _route_evidence_found(self, candidate_rows: dict[str, dict[str, Any]], plan: QueryPlan) -> bool:
        rows = [candidate["row"] for candidate in candidate_rows.values()]
        if plan.intent in STRICT_EVIDENCE_INTENTS:
            return any(row_matches_query_plan_evidence(row, plan) for row in rows)
        if plan.required_evidence_groups:
            direct_rows = [row for row in rows if row_matches_required_evidence_groups(row, plan)]
            if plan.search_mode in {"comparison", "exhaustive"} or plan.intent in {
                "projection_comparison",
                "clause_comparison",
            }:
                return len({str(row["document_id"]) for row in direct_rows}) >= 2
            return bool(direct_rows)
        if plan.intent == "standard_selection" and plan.candidate_title_terms:
            return any(any(term in (row["title"] or "") for term in plan.candidate_title_terms) for row in rows)
        if plan.standard_numbers:
            expected = {number.upper().replace(" ", "") for number in plan.standard_numbers}
            return any((row["standard_no"] or "").upper().replace(" ", "") in expected for row in rows)
        if plan.candidate_title_terms:
            return any(any(term in (row["title"] or "") for term in plan.candidate_title_terms) for row in rows)
        return bool(rows)

    def _evidence_sufficient_without_vectors(
        self,
        candidate_rows: dict[str, dict[str, Any]],
        plan: QueryPlan,
        scope_applied: bool,
    ) -> bool:
        if plan.exhaustive_search or not candidate_rows:
            return False
        if plan.intent in STRICT_EVIDENCE_INTENTS | {"standard_selection"}:
            return self._route_evidence_found(candidate_rows, plan)
        return False

    def _vector_candidates(
        self,
        conn: sqlite3.Connection,
        query: str,
        plan: QueryPlan,
        where: list[str],
        params: list[Any],
        limit: int,
    ) -> VectorCandidateResult:
        dense = self._dense_embedding_candidates(conn, query, plan, where, params, limit)
        if dense.succeeded:
            return VectorCandidateResult(
                candidates=tuple((row, min(1.0, score + 0.08)) for row, score in dense.candidates),
                route=dense.route,
                embedding_ms=dense.embedding_ms,
                search_ms=dense.search_ms,
                error=dense.error,
            )
        if not self._has_narrow_sql_scope(where):
            return dense
        started = perf_counter()
        local = self._local_hash_vector_candidates(conn, query, plan, where, params, limit)
        return VectorCandidateResult(
            candidates=tuple(local),
            route="local_hash" if local else dense.route,
            embedding_ms=dense.embedding_ms,
            search_ms=dense.search_ms + (perf_counter() - started) * 1000,
            error=dense.error,
        )

    def _dense_embedding_candidates(
        self,
        conn: sqlite3.Connection,
        query: str,
        plan: QueryPlan,
        where: list[str],
        params: list[Any],
        limit: int,
    ) -> VectorCandidateResult:
        settings = get_settings()
        config = embedding_config(settings)
        if not config.enabled:
            return VectorCandidateResult(error="embedding_not_configured")
        try:
            exists = conn.execute(
                f"""
                select 1
                from chunk_embeddings e
                join chunks c on c.chunk_id = e.chunk_id
                join documents d on d.document_id = c.document_id
                where e.vector_model = ? and {' and '.join(where)}
                limit 1
                """,
                [config.model, *params],
            ).fetchone()
        except sqlite3.OperationalError:
            return VectorCandidateResult(error="embedding_table_unavailable")
        if not exists:
            return VectorCandidateResult(error="embedding_scope_empty")
        embedding_started = perf_counter()
        try:
            q_vec = EmbeddingProvider(config, timeout_seconds=settings.request_timeout_seconds).embed(
                [query + " " + " ".join(query_terms(query, plan)[:24])]
            )[0]
        except Exception as error:
            return VectorCandidateResult(
                embedding_ms=(perf_counter() - embedding_started) * 1000,
                error=type(error).__name__,
            )
        embedding_ms = (perf_counter() - embedding_started) * 1000

        search_started = perf_counter()
        if settings.ann_search_enabled:
            try:
                ann = get_ann_index(settings.ann_index_path, settings.ann_manifest_path)
                manifest = ann.manifest()
                if manifest and self._ann_manifest_matches(conn, manifest, config.model, len(q_vec)):
                    matches = ann.search(q_vec, max(120, limit * 4))
                    chunk_ids = [chunk_id for chunk_id, _ in matches]
                    if chunk_ids:
                        placeholders = ",".join("?" for _ in chunk_ids)
                        rows = conn.execute(
                            f"""
                            select c.*, d.document_type, d.status, d.official_url, d.source_platform, 0.0 as rank
                            from chunks c
                            join documents d on d.document_id = c.document_id
                            where c.chunk_id in ({placeholders}) and {' and '.join(where)}
                            """,
                            [*chunk_ids, *params],
                        ).fetchall()
                        row_map = {str(row["chunk_id"]): row for row in rows}
                        candidates = tuple(
                            (row_map[chunk_id], similarity)
                            for chunk_id, similarity in matches
                            if chunk_id in row_map and similarity > 0.15
                        )[:limit]
                        return VectorCandidateResult(
                            candidates=candidates,
                            route="ann",
                            embedding_ms=embedding_ms,
                            search_ms=(perf_counter() - search_started) * 1000,
                        )
            except (OSError, ValueError, KeyError, sqlite3.OperationalError) as error:
                ann_error = type(error).__name__
            except Exception as error:
                ann_error = type(error).__name__
        else:
            ann_error = "ann_disabled"

        if not self._has_narrow_sql_scope(where):
            return VectorCandidateResult(
                route="none",
                embedding_ms=embedding_ms,
                search_ms=(perf_counter() - search_started) * 1000,
                error=locals().get("ann_error", "ann_unavailable"),
            )
        try:
            rows = conn.execute(
                f"""
                select c.*, d.document_type, d.status, d.official_url, d.source_platform, e.vector_json, 0.0 as rank
                from chunk_embeddings e
                join chunks c on c.chunk_id = e.chunk_id
                join documents d on d.document_id = c.document_id
                where e.vector_model = ? and {' and '.join(where)}
                """,
                [config.model, *params],
            ).fetchall()
        except sqlite3.OperationalError:
            return VectorCandidateResult(
                embedding_ms=embedding_ms,
                search_ms=(perf_counter() - search_started) * 1000,
                error="exact_dense_query_failed",
            )
        scored = []
        for row in rows:
            score = cosine_dense(q_vec, parse_dense_vector(row["vector_json"]))
            if score > 0.2:
                scored.append((row, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return VectorCandidateResult(
            candidates=tuple(scored[:limit]),
            route="exact_dense",
            embedding_ms=embedding_ms,
            search_ms=(perf_counter() - search_started) * 1000,
            error=locals().get("ann_error"),
        )

    @staticmethod
    def _has_narrow_sql_scope(where: list[str]) -> bool:
        return any("d.document_id in" in clause or "d.standard_no =" in clause for clause in where)

    @staticmethod
    def _ann_manifest_matches(
        conn: sqlite3.Connection,
        manifest: AnnManifest,
        model: str,
        dimensions: int,
    ) -> bool:
        if manifest.model != model or manifest.dimensions != dimensions:
            return False
        row = conn.execute(
            """
            select count(*) as count, min(dimensions) as min_dimensions,
                   max(dimensions) as max_dimensions, max(updated_at) as max_updated_at
            from chunk_embeddings
            where vector_model = ?
            """,
            (model,),
        ).fetchone()
        return bool(
            row
            and int(row["count"] or 0) == manifest.count
            and int(row["min_dimensions"] or 0) == manifest.dimensions
            and int(row["max_dimensions"] or 0) == manifest.dimensions
            and str(row["max_updated_at"] or "") == manifest.max_updated_at
        )

    def _local_hash_vector_candidates(
        self,
        conn: sqlite3.Connection,
        query: str,
        plan: QueryPlan,
        where: list[str],
        params: list[Any],
        limit: int,
    ) -> list[tuple[sqlite3.Row, float]]:
        q_vec = hashed_vector(query + " " + " ".join(query_terms(query, plan)))
        if not q_vec:
            return []
        try:
            rows = conn.execute(
                f"""
                select c.*, d.document_type, d.status, d.official_url, d.source_platform, v.vector_json, 0.0 as rank
                from chunk_vectors v
                join chunks c on c.chunk_id = v.chunk_id
                join documents d on d.document_id = c.document_id
                where {' and '.join(where)}
                """,
                params,
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        scored = []
        for row in rows:
            score = cosine_sparse(q_vec, row["vector_json"])
            if score > 0.08:
                scored.append((row, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]

    def _graph_candidates(
        self,
        conn: sqlite3.Connection,
        query: str,
        plan: QueryPlan,
        where: list[str],
        params: list[Any],
        limit: int,
    ) -> list[tuple[sqlite3.Row, float]]:
        preferred_terms = [
            *plan.subject_terms,
            *plan.required_terms,
            *plan.alternative_terms,
            *plan.candidate_title_terms,
            *plan.standard_numbers,
        ]
        terms = [
            term
            for term in dict.fromkeys([*preferred_terms, *query_terms(query, plan)])
            if 2 <= len(term) <= 80
        ][:12]
        if not terms:
            return []
        term_where = " or ".join("e.name like ?" for _ in terms)
        term_params = [f"%{term}%" for term in terms]
        relation_map = {
            "authority_responsibility": ("RESPONSIBLE_FOR", "STATES_RESPONSIBILITY"),
            "legal_responsibility": ("RESPONSIBLE_FOR", "STATES_RESPONSIBILITY"),
            "service_materials": ("REQUIRES_MATERIAL", "SPECIFIES_MATERIAL", "HAS_REQUIREMENT"),
            "service_procedure_basis": ("APPLIES_TO", "DECIDED_BY", "SUPPORTS_GUIDE"),
            "service_time_limit": ("HAS_TIME_LIMIT",),
            "standard_selection": ("APPLIES_TO_MINERAL", "HAS_CODE"),
            "related_documents": ("REFERENCES_STANDARD", "REPLACES"),
        }
        relation_types = relation_map.get(plan.intent, ())
        relation_where = ""
        relation_params: list[Any] = []
        if relation_types:
            placeholders = ",".join("?" for _ in relation_types)
            relation_where = f" and r.relation_type in ({placeholders})"
            relation_params.extend(relation_types)
        def run_graph(extra_where: str, extra_params: list[Any]) -> list[sqlite3.Row]:
            try:
                return conn.execute(
                    f"""
                    select c.*, d.document_type, d.status, d.official_url, d.source_platform, 0.0 as rank,
                           max(r.confidence) as graph_score
                    from kg_entities e
                    join kg_relations r on r.target_entity_id = e.entity_id or r.source_entity_id = e.entity_id
                    join chunks c on c.chunk_id = r.evidence_chunk_id
                    join documents d on d.document_id = c.document_id
                    where ({term_where}){extra_where} and {' and '.join(where)}
                    group by c.chunk_id
                    order by graph_score desc
                    limit ?
                    """,
                    [*term_params, *extra_params, *params, limit],
                ).fetchall()
            except sqlite3.OperationalError:
                return []

        rows = run_graph(relation_where, relation_params)
        if not rows and relation_types:
            rows = run_graph("", [])
        return [(row, float(row["graph_score"] or 0.5)) for row in rows]

    def standards(self, params: dict[str, Any]) -> dict[str, Any]:
        page = max(1, int(params.get("page") or 1))
        page_size = max(1, min(int(params.get("page_size") or 20), 100))
        where = ["1=1"]
        values: list[Any] = []
        q = params.get("q")
        if q:
            where.append("(title like ? or standard_no like ?)")
            values.extend([f"%{q}%", f"%{q}%"])
        for request_key, column in [
            ("standard_no", "standard_no"),
            ("status", "status"),
            ("text_access", "text_access"),
            ("visibility", "visibility"),
            ("document_type", "document_type"),
            ("validation_status", "validation_status"),
        ]:
            value = params.get(request_key)
            if value:
                where.append(f"{column} = ?")
                values.append(value)
        where_sql = " and ".join(where)
        offset = (page - 1) * page_size
        with connect(self.db_path) as conn:
            total = conn.execute(f"select count(*) from documents where {where_sql}", values).fetchone()[0]
            rows = conn.execute(
                f"""
                select * from documents
                where {where_sql}
                order by coalesce(standard_no, ''), title
                limit ? offset ?
                """,
                [*values, page_size, offset],
            ).fetchall()
        return {
            "items": [row_to_document(row) for row in rows],
            "pagination": {"page": page, "page_size": page_size, "total": total},
        }

    def document(self, document_id: str) -> dict[str, Any] | None:
        with connect(self.db_path) as conn:
            row = conn.execute("select * from documents where document_id = ?", (document_id,)).fetchone()
        return row_to_document(row) if row else None

    def chunk(self, chunk_id: str, include_full_text: bool = False) -> dict[str, Any] | None:
        with connect(self.db_path) as conn:
            row = conn.execute("select * from chunks where chunk_id = ?", (chunk_id,)).fetchone()
        if not row:
            return None
        data = {
            "chunk_id": row["chunk_id"],
            "document_id": row["document_id"],
            "title": row["title"],
            "standard_no": row["standard_no"],
            "section_path": row["section_path"],
            "clause_no": row["clause_no"],
            "page_start": row["page_start"],
            "page_end": row["page_end"],
            "quote": quote_text(row["text"]),
            "source_type": row["source_type"],
            "text_access": row["text_access"],
            "validation_status": row["validation_status"],
        }
        if include_full_text:
            data["text"] = row["text"]
        return data

    def create_candidate(self, payload: dict[str, Any]) -> dict[str, Any]:
        candidate_id = payload.get("candidate_id") or f"candidate-{uuid.uuid4().hex[:12]}"
        now = utc_now()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                insert into candidates (
                  candidate_id, triggering_question, standard_no, title, source_url,
                  source_type, text_access, page_range, extracted_text, ocr_confidence,
                  ocr_engine, ocr_engine_version, review_status, copyright_note,
                  created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    payload.get("triggering_question"),
                    payload.get("standard_no"),
                    payload.get("title"),
                    payload.get("source_url"),
                    payload.get("source_type"),
                    payload.get("text_access"),
                    payload.get("page_range"),
                    payload.get("extracted_text"),
                    payload.get("ocr_confidence"),
                    payload.get("ocr_engine"),
                    payload.get("ocr_engine_version"),
                    payload.get("review_status") or "candidate_found",
                    payload.get("copyright_note"),
                    now,
                    now,
                ),
            )
        return {"ok": True, "candidate_id": candidate_id, "review_status": payload.get("review_status") or "candidate_found"}

    def candidates(self, page: int = 1, page_size: int = 50) -> dict[str, Any]:
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        offset = (page - 1) * page_size
        with connect(self.db_path) as conn:
            total = conn.execute("select count(*) from candidates").fetchone()[0]
            rows = conn.execute(
                "select * from candidates order by created_at desc limit ? offset ?",
                (page_size, offset),
            ).fetchall()
        return {"items": [dict(row) for row in rows], "pagination": {"page": page, "page_size": page_size, "total": total}}
