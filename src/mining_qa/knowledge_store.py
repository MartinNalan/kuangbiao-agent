from __future__ import annotations

import json
import hashlib
import math
import re
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KB_ROOT = PROJECT_ROOT / "data" / "knowledge_base"
DEFAULT_DB_PATH = DEFAULT_KB_ROOT / "db" / "knowledge_base.sqlite"
QUOTE_LIMIT = 260
VECTOR_DIM = 512


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
    rows = conn.execute(
        "select document_id, standard_no from documents where standard_no is not null and (official_url is null or official_url = '')"
    ).fetchall()
    for row in rows:
        platform, url = official_source(row["standard_no"])
        if url:
            conn.execute(
                "update documents set official_url = ?, source_platform = ? where document_id = ?",
                (url, platform, row["document_id"]),
            )


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


def table_quote(table_json: str | None, fallback: str, query: str = "", limit: int = QUOTE_LIMIT) -> str:
    if not table_json:
        return quote_text(fallback, query, limit)
    try:
        table = json.loads(table_json)
    except json.JSONDecodeError:
        return quote_text(fallback, query, limit)

    caption = table.get("caption") or ""
    matrix = table.get("matrix") or []
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


def is_standard_or_technical_query(query: str) -> bool:
    return any(term in query for term in ("标准", "规范", "规程", "技术", "工程间距", "勘查类型", "勘查规范"))


def query_terms(query: str) -> list[str]:
    terms: list[str] = []
    raw_terms = re.findall(r"[A-Za-z0-9]+(?:/[A-Za-z0-9]+)?(?:[-.][A-Za-z0-9]+)*|[\u4e00-\u9fff]{2,}", query)
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
    ]
    synonyms = {
        "金矿": ["岩金"],
        "岩金": ["金矿"],
        "沙金": ["砂金", "金属砂矿", "金属砂矿类"],
        "砂金": ["沙金", "金属砂矿", "金属砂矿类"],
        "金属砂矿": ["砂金", "沙金", "金属砂矿类"],
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
            if phrase in term or phrase in query:
                terms.append(phrase)
        for source, replacements in synonyms.items():
            if source in term or source in query:
                terms.extend(replacements)
    deduped: list[str] = []
    seen = set()
    for term in terms:
        if term and term not in seen:
            deduped.append(term)
            seen.add(term)
    return deduped


def fts_query(query: str) -> str:
    terms = query_terms(query)
    if not terms:
        return ""
    escaped = [term.replace('"', '""') for term in terms[:8]]
    return " OR ".join(f'"{term}"' for term in escaped)


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


def lexical_score(row: sqlite3.Row, query: str, idx: int) -> float:
    terms = query_terms(query)
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


def intent_score(row: sqlite3.Row, query: str) -> float:
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
            kg_entity_count = table_count(conn, "kg_entities")
            kg_relation_count = table_count(conn, "kg_relations")
        return {
            "ok": True,
            "service": "mining-knowledge-base",
            "storage": "sqlite_fts5",
            "db_path": str(self.db_path),
            "document_count": doc_count,
            "chunk_count": chunk_count,
            "candidate_count": candidate_count,
            "vector_count": vector_count,
            "kg_entity_count": kg_entity_count,
            "kg_relation_count": kg_relation_count,
        }

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query") or "").strip()
        filters = payload.get("filters") or {}
        options = payload.get("options") or {}
        top_k = int(options.get("top_k") or 10)
        top_k = max(1, min(top_k, 50))
        recall_limit = max(top_k * 20, 60)
        include_full_text = bool(options.get("include_full_text"))

        where = ["d.visibility in ('internal', 'public')"]
        params: list[Any] = []
        standard_no = filters.get("standard_no")
        if standard_no:
            where.append("d.standard_no = ?")
            params.append(standard_no)
        statuses = filters.get("status") or []
        if isinstance(statuses, str):
            statuses = [statuses]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where.append(f"d.status in ({placeholders})")
            params.extend(statuses)
        doc_types = filters.get("document_types") or []
        if isinstance(doc_types, str):
            doc_types = [doc_types]
        if doc_types:
            placeholders = ",".join("?" for _ in doc_types)
            where.append(f"d.document_type in ({placeholders})")
            params.extend(doc_types)

        results: list[sqlite3.Row] = []
        with connect(self.db_path) as conn:
            match = fts_query(query)
            if match:
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
                    results = conn.execute(sql, [match, *params, recall_limit]).fetchall()
                except sqlite3.OperationalError:
                    results = []
            if len(results) < recall_limit and query:
                like_terms = query_terms(query)[:5] or [query]
                like_where = ["(" + " or ".join(["c.text like ? or c.title like ? or c.standard_no like ?" for _ in like_terms]) + ")"]
                like_params: list[Any] = []
                for term in like_terms:
                    pattern = f"%{term}%"
                    like_params.extend([pattern, pattern, pattern])
                sql = f"""
                    select c.*, d.document_type, d.status, d.official_url, d.source_platform, 0.0 as rank
                    from chunks c
                    join documents d on d.document_id = c.document_id
                    where {' and '.join(where + like_where)}
                    order by length(c.text) asc
                    limit ?
                """
                seen = {row["chunk_id"] for row in results}
                for row in conn.execute(sql, [*params, *like_params, recall_limit]).fetchall():
                    if row["chunk_id"] not in seen:
                        results.append(row)
                    if len(results) >= recall_limit:
                        break

        items = []
        candidate_rows: dict[str, dict[str, Any]] = {}
        for idx, row in enumerate(results):
            candidate_rows.setdefault(row["chunk_id"], {"row": row, "hit_types": set(), "boost": 0.0, "order": idx})
            candidate_rows[row["chunk_id"]]["hit_types"].add("full_text")
            candidate_rows[row["chunk_id"]]["boost"] += 2.0
        with connect(self.db_path) as conn:
            for row, score in self._vector_candidates(conn, query, where, params, recall_limit):
                candidate_rows.setdefault(row["chunk_id"], {"row": row, "hit_types": set(), "boost": 0.0, "order": len(candidate_rows)})
                candidate_rows[row["chunk_id"]]["hit_types"].add("vector")
                candidate_rows[row["chunk_id"]]["boost"] += score * 3.0
            for row, score in self._graph_candidates(conn, query, where, params, recall_limit):
                candidate_rows.setdefault(row["chunk_id"], {"row": row, "hit_types": set(), "boost": 0.0, "order": len(candidate_rows)})
                candidate_rows[row["chunk_id"]]["hit_types"].add("graph")
                candidate_rows[row["chunk_id"]]["boost"] += score * 2.5

        candidates = list(candidate_rows.values())
        ranked = sorted(
            enumerate(candidates),
            key=lambda item: lexical_score(item[1]["row"], query, item[1]["order"])
            + intent_score(item[1]["row"], query)
            + item[1]["boost"],
            reverse=True,
        )
        if is_standard_selection_query(query) and ranked:
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
                }
            )

        for idx, (_, candidate) in enumerate(ranked[:top_k], 1):
            row = candidate["row"]
            raw_score = lexical_score(row, query, idx - 1) + intent_score(row, query) + candidate["boost"]
            score = min(0.99, max(0.05, raw_score / 12.0))
            item = {
                "chunk_id": row["chunk_id"],
                "document_id": row["document_id"],
                "title": row["title"],
                "standard_no": row["standard_no"],
                "section_path": row["section_path"],
                "clause_no": row["clause_no"],
                "page_start": row["page_start"],
                "page_end": row["page_end"],
                "page": row["page_start"],
                "quote": table_quote(row["table_json"], row["text"], query)
                if row["chunk_type"] == "table"
                else quote_text(row["text"], query),
                "score": round(score, 4),
                "hit_type": sorted(candidate["hit_types"]),
                "source_type": row["source_type"],
                "text_access": row["text_access"],
                "validation_status": row["validation_status"],
                "url": row["official_url"],
                "source_platform": row["source_platform"],
            }
            if include_full_text:
                item["text"] = row["text"]
            items.append(item)

        has_hits = bool(items)
        return {
            "query": query,
            "results": items,
            "retrieval": {
                "full_text_hits": sum(1 for c in candidates if "full_text" in c["hit_types"]),
                "vector_hits": sum(1 for c in candidates if "vector" in c["hit_types"]),
                "graph_hits": sum(1 for c in candidates if "graph" in c["hit_types"]),
                "web_hits": 0,
            },
            "coverage": {
                "has_clause_level_evidence": any(item.get("clause_no") for item in items),
                "has_page_level_evidence": any(item.get("page_start") for item in items),
                "needs_web_supplement": not has_hits,
                "notes": [] if has_hits else ["本地知识库未命中可引用证据，建议进入联网补齐候选流程。"],
            },
        }

    def _vector_candidates(
        self, conn: sqlite3.Connection, query: str, where: list[str], params: list[Any], limit: int
    ) -> list[tuple[sqlite3.Row, float]]:
        q_vec = hashed_vector(query + " " + " ".join(query_terms(query)))
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
        self, conn: sqlite3.Connection, query: str, where: list[str], params: list[Any], limit: int
    ) -> list[tuple[sqlite3.Row, float]]:
        terms = [term for term in query_terms(query) if len(term) >= 2][:8]
        if not terms:
            return []
        term_where = " or ".join("e.name like ?" for _ in terms)
        term_params = [f"%{term}%" for term in terms]
        try:
            rows = conn.execute(
                f"""
                select c.*, d.document_type, d.status, d.official_url, d.source_platform, 0.0 as rank,
                       max(r.confidence) as graph_score
                from kg_entities e
                join kg_relations r on r.target_entity_id = e.entity_id or r.source_entity_id = e.entity_id
                join chunks c on c.chunk_id = r.evidence_chunk_id
                join documents d on d.document_id = c.document_id
                where ({term_where}) and {' and '.join(where)}
                group by c.chunk_id
                limit ?
                """,
                [*term_params, *params, limit],
            ).fetchall()
        except sqlite3.OperationalError:
            return []
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
