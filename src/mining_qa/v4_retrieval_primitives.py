from __future__ import annotations

"""Production-only retrieval primitives for the v4 fixed-20 runtime.

The original implementations were accepted through the T029/T036/T039/T063
experiments.  This module is the clean production copy: it contains no Gold
cases, expected evidence identifiers, reference answers, or normative text,
and it never imports an experiment under :mod:`scripts`.
"""

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import sqlite3
from statistics import mean
import threading
import time
from time import perf_counter
from typing import Any, Iterable
import unicodedata
from urllib.parse import urlsplit

import httpx
import numpy as np

from .query_understanding import normalize_user_query


INDEX_SCHEMA_VERSION = "v4-fts5-cjk-bigram-trigram.v2"
BM25_COLUMN_WEIGHTS = (0.0, 4.0, 5.0, 3.0, 1.0)
MAX_FTS_TERMS = 64
RRF_K = 60
LEXICAL_TOP_K = 50
DENSE_TOP_K = 60
STAGE1_DOCUMENT_COUNT = 30
STAGE2_INTERNAL_TOP_K = 200
FINAL_TOP_K = 20
LEXICAL_HEAD_DEPTH = 1
DENSE_HEAD_DEPTH = 4
MAX_HEAD_DEPTH = 10


@dataclass(frozen=True)
class Unit:
    unit_id: str
    document_id: str
    corpus: str
    title: str
    standard_no: str
    unit_order: int
    unit_type: str
    section_path: str
    clause_no: str
    page_start: int | None
    clean_text: str
    search_text: str
    heading_text: str
    document_text: str
    char_length: int


@dataclass(frozen=True)
class RankedUnit:
    unit: Unit
    score: float
    matched_terms: int


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = value.replace("勘察", "勘查").replace("工程距离", "工程间距")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def load_retrieval_units(path: Path) -> list[Unit]:
    units: list[Unit] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not row.get("search_eligible", False):
                continue
            search_text = normalize_text(
                row.get("search_text") or row.get("citation_text") or ""
            )
            heading = normalize_text(
                f"{row.get('section_path') or ''} {row.get('clause_no') or ''}"
            )
            document_text = normalize_text(
                f"{row.get('title') or ''} {row.get('standard_no') or ''}"
            )
            units.append(
                Unit(
                    unit_id=row["retrieval_unit_id"],
                    document_id=row["document_id"],
                    corpus=row["corpus"],
                    title=row["title"],
                    standard_no=row.get("standard_no") or "",
                    unit_order=line_number,
                    unit_type=row["unit_type"],
                    section_path=row.get("section_path") or "",
                    clause_no=row.get("clause_no") or "",
                    page_start=row.get("page_start"),
                    clean_text=row.get("citation_text") or "",
                    search_text=search_text,
                    heading_text=heading,
                    document_text=document_text,
                    char_length=max(1, len(search_text)),
                )
            )
    if not units:
        raise RuntimeError(f"no search-eligible retrieval units found in {path}")
    return units


QUERY_STOP_PHRASES = (
    "我现在",
    "我有一个",
    "我的",
    "这种情况",
    "应该",
    "应当",
    "需要",
    "请问",
    "究竟",
    "哪些",
    "哪个",
    "什么",
    "多少",
    "如何",
    "怎么",
    "能不能",
    "是否",
    "为了",
    "可以",
)
NGRAM_STOP = {
    "一个",
    "这种",
    "情况",
    "应该",
    "应当",
    "需要",
    "哪些",
    "哪个",
    "什么",
    "多少",
    "如何",
    "怎么",
    "是否",
    "可以",
    "进行",
    "其中",
    "对于",
    "时候",
    "问题",
}


def add_term(terms: dict[str, float], term: str, weight: float) -> None:
    term = normalize_text(term)
    term = re.sub(r"^[\W_]+|[\W_]+$", "", term)
    if len(term) < 2 or term in NGRAM_STOP:
        return
    terms[term] = max(weight, terms.get(term, 0.0))


def query_terms(query: str, explicit_terms: Iterable[str] = ()) -> dict[str, float]:
    normalized = normalize_text(normalize_user_query(query))
    terms: dict[str, float] = {}
    for term in explicit_terms:
        add_term(terms, term, 3.0)
    reduced = normalized
    for phrase in QUERY_STOP_PHRASES:
        reduced = reduced.replace(phrase, " ")
    chunks = re.findall(
        r"[\u4e00-\u9fff]+|[a-z]+(?:[/.-][a-z0-9]+)*|\d+(?:\.\d+)?",
        reduced,
    )
    for chunk in chunks:
        if re.fullmatch(r"[a-z0-9./-]+", chunk):
            add_term(terms, chunk, 2.5)
            continue
        if 2 <= len(chunk) <= 14:
            add_term(terms, chunk, 2.2)
        for size, weight in ((4, 1.55), (3, 1.0), (2, 0.42)):
            if len(chunk) < size:
                continue
            for index in range(len(chunk) - size + 1):
                add_term(terms, chunk[index : index + size], weight)
    if len(terms) > 80:
        selected = sorted(
            terms.items(), key=lambda item: (-item[1], -len(item[0]), item[0])
        )[:80]
        return dict(selected)
    return terms


class FullScanSearcher:
    def __init__(self, units: list[Unit]):
        self.units = units
        self.avg_length = mean(unit.char_length for unit in units)

    def search(
        self,
        query: str,
        *,
        explicit_terms: Iterable[str] = (),
        top_k: int = 200,
    ) -> tuple[list[RankedUnit], dict[str, Any]]:
        started = perf_counter()
        terms = query_terms(query, explicit_terms)
        if not terms:
            return [], {
                "elapsed_ms": 0.0,
                "term_count": 0,
                "scanned_units": len(self.units),
            }
        document_frequency = {term: 0 for term in terms}
        sparse_matches: list[tuple[Unit, dict[str, float]]] = []
        for unit in self.units:
            matched: dict[str, float] = {}
            for term in terms:
                content_count = unit.search_text.count(term)
                heading_count = unit.heading_text.count(term)
                document_count = unit.document_text.count(term)
                tf = content_count + 1.35 * heading_count + 0.62 * document_count
                if tf > 0:
                    matched[term] = tf
                    document_frequency[term] += 1
            if matched:
                sparse_matches.append((unit, matched))
        total_units = len(self.units)
        ranked: list[RankedUnit] = []
        k1 = 1.2
        b = 0.75
        for unit, matched in sparse_matches:
            length_norm = 1.0 - b + b * unit.char_length / self.avg_length
            score = 0.0
            for term, tf in matched.items():
                df = document_frequency[term]
                idf = math.log(1.0 + (total_units - df + 0.5) / (df + 0.5))
                saturation = tf * (k1 + 1.0) / (tf + k1 * length_norm)
                score += idf * saturation * terms[term]
            score += 0.18 * len(matched) / len(terms)
            ranked.append(
                RankedUnit(unit=unit, score=score, matched_terms=len(matched))
            )
        ranked.sort(
            key=lambda item: (
                -item.score,
                -item.matched_terms,
                item.unit.standard_no,
                item.unit.document_id,
                item.unit.unit_order,
            )
        )
        elapsed_ms = (perf_counter() - started) * 1000.0
        return ranked[:top_k], {
            "elapsed_ms": round(elapsed_ms, 3),
            "term_count": len(terms),
            "matched_units": len(ranked),
            "scanned_units": total_units,
            "terms": terms,
        }


def multigram_tokens(text: str) -> list[str]:
    normalized = normalize_text(text)
    tokens: list[str] = []
    for chunk in re.findall(r"[\u4e00-\u9fff]+|[a-z0-9]+", normalized):
        if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
            for size in (2, 3):
                for index in range(max(0, len(chunk) - size + 1)):
                    tokens.append(chunk[index : index + size])
        elif len(chunk) >= 2:
            tokens.append(chunk)
    return tokens


def fts_terms(query: str, explicit_terms: Iterable[str] = ()) -> list[str]:
    weighted = query_terms(query, explicit_terms)
    selected: list[str] = []
    seen: set[str] = set()
    for term, _weight in sorted(
        weighted.items(), key=lambda item: (-item[1], -len(item[0]), item[0])
    ):
        for value in multigram_tokens(term):
            if value in seen:
                continue
            seen.add(value)
            selected.append(value)
            if len(selected) >= MAX_FTS_TERMS:
                return selected
    return selected


def quote_fts_phrase(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def fts_match_expression(terms: list[str]) -> str:
    return " OR ".join(quote_fts_phrase(term) for term in terms)


class FtsSearcher:
    def __init__(self, path: Path, units: list[Unit]):
        self.path = path
        self.units = units
        self.unit_by_id = {unit.unit_id: unit for unit in units}
        self.connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.connection.row_factory = sqlite3.Row
        metadata = dict(self.connection.execute("select key,value from index_metadata"))
        if metadata.get("schema_version") != INDEX_SCHEMA_VERSION:
            raise RuntimeError(f"unexpected experimental FTS schema: {metadata}")
        if int(metadata.get("unit_count", "-1")) != len(units):
            raise RuntimeError("experimental FTS unit count does not match retrieval leaves")
        self.connection.execute("select count(*) from retrieval_fts").fetchone()

    def close(self) -> None:
        self.connection.close()

    def candidates(
        self, query: str, *, explicit_terms: Iterable[str] = ()
    ) -> tuple[list[Unit], dict[str, Any]]:
        started = perf_counter()
        terms = fts_terms(query, explicit_terms)
        if not terms:
            return [], {
                "elapsed_ms": 0.0,
                "candidate_count": 0,
                "fts_terms": [],
                "match_expression": "",
            }
        expression = fts_match_expression(terms)
        rows = self.connection.execute(
            "select unit_id from retrieval_fts where retrieval_fts match ?",
            (expression,),
        ).fetchall()
        units = [self.unit_by_id[row["unit_id"]] for row in rows]
        elapsed_ms = (perf_counter() - started) * 1000.0
        return units, {
            "elapsed_ms": round(elapsed_ms, 3),
            "candidate_count": len(units),
            "fts_terms": terms,
            "match_expression": expression,
        }


def rerank_fts_candidates(
    candidates: list[Unit],
    query: str,
    *,
    explicit_terms: Iterable[str] = (),
    candidate_trace: dict[str, Any],
    top_k: int = 200,
) -> tuple[list[RankedUnit], dict[str, Any]]:
    started = perf_counter()
    if candidates:
        ranking, rerank_trace = FullScanSearcher(candidates).search(
            query, explicit_terms=explicit_terms, top_k=top_k
        )
    else:
        ranking = []
        rerank_trace = {"elapsed_ms": 0.0, "term_count": 0, "scanned_units": 0}
    rerank_ms = (perf_counter() - started) * 1000.0
    return ranking, {
        **candidate_trace,
        "elapsed_ms": round(candidate_trace["elapsed_ms"] + rerank_ms, 3),
        "candidate_lookup_ms": candidate_trace["elapsed_ms"],
        "rerank_ms": round(rerank_ms, 3),
        "rerank_trace": rerank_trace,
    }


def ordered_documents(ranking: list[Any]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in ranking:
        document_id = item.unit.document_id
        if document_id in seen:
            continue
        seen.add(document_id)
        output.append(document_id)
    return output


def equal_rrf(
    lexical: list[RankedUnit], dense: list[RankedUnit]
) -> tuple[list[RankedUnit], dict[str, dict[str, int | None]]]:
    lexical_rank = {row.unit.unit_id: rank for rank, row in enumerate(lexical, 1)}
    dense_rank = {row.unit.unit_id: rank for rank, row in enumerate(dense, 1)}
    unit_by_id = {row.unit.unit_id: row.unit for row in lexical + dense}
    details: dict[str, dict[str, int | None]] = {}
    ranking: list[RankedUnit] = []
    for unit_id in lexical_rank.keys() | dense_rank.keys():
        ranks = [
            rank
            for rank in (lexical_rank.get(unit_id), dense_rank.get(unit_id))
            if rank is not None
        ]
        score = sum(1.0 / (RRF_K + rank) for rank in ranks)
        details[unit_id] = {
            "lexical_rank": lexical_rank.get(unit_id),
            "dense_rank": dense_rank.get(unit_id),
            "route_count": len(ranks),
            "best_route_rank": min(ranks),
        }
        ranking.append(
            RankedUnit(
                unit=unit_by_id[unit_id], score=score, matched_terms=len(ranks)
            )
        )
    ranking.sort(
        key=lambda row: (
            -row.score,
            -int(details[row.unit.unit_id]["route_count"] or 0),
            int(details[row.unit.unit_id]["best_route_rank"] or 10_000),
            row.unit.standard_no,
            row.unit.document_id,
            row.unit.unit_order,
        )
    )
    return ranking, details


def apply_head_admission(
    base_ranking: list[RankedUnit],
    lexical: list[RankedUnit],
    dense: list[RankedUnit],
    *,
    lexical_head_depth: int,
    dense_head_depth: int,
) -> tuple[list[RankedUnit], dict[str, Any]]:
    if not (0 <= lexical_head_depth <= MAX_HEAD_DEPTH):
        raise ValueError("lexical head depth outside experiment grid")
    if not (0 <= dense_head_depth <= MAX_HEAD_DEPTH):
        raise ValueError("dense head depth outside experiment grid")
    protected_ids = {
        row.unit.unit_id for row in lexical[:lexical_head_depth]
    } | {row.unit.unit_id for row in dense[:dense_head_depth]}
    if len(protected_ids) > FINAL_TOP_K:
        raise RuntimeError("protected route heads exceed final Top-20")
    base_top_ids = {row.unit.unit_id for row in base_ranking[:FINAL_TOP_K]}
    selected = [row for row in base_ranking if row.unit.unit_id in protected_ids]
    selected_ids = {row.unit.unit_id for row in selected}
    selected.extend(
        row for row in base_ranking if row.unit.unit_id not in selected_ids
    )
    selected = selected[:FINAL_TOP_K]
    final_top_ids = {row.unit.unit_id for row in selected}
    base_order = {
        row.unit.unit_id: rank for rank, row in enumerate(base_ranking, 1)
    }
    selected.sort(key=lambda row: base_order[row.unit.unit_id])
    final_ranking = selected + [
        row for row in base_ranking if row.unit.unit_id not in final_top_ids
    ]
    final_order = {
        row.unit.unit_id: rank for rank, row in enumerate(final_ranking, 1)
    }
    promoted = sorted(protected_ids - base_top_ids, key=base_order.__getitem__)
    displaced = sorted(base_top_ids - final_top_ids, key=base_order.__getitem__)
    return final_ranking, {
        "lexical_head_depth": lexical_head_depth,
        "dense_head_depth": dense_head_depth,
        "protected_count": len(protected_ids),
        "promoted_count": len(promoted),
        "displaced_count": len(displaced),
        "protected_ids": sorted(protected_ids, key=base_order.__getitem__),
        "promoted": [
            {
                "unit_id": unit_id,
                "base_rrf_rank": base_order[unit_id],
                "final_rank": final_order[unit_id],
            }
            for unit_id in promoted
        ],
        "displaced": [
            {
                "unit_id": unit_id,
                "base_rrf_rank": base_order[unit_id],
                "final_rank": final_order[unit_id],
            }
            for unit_id in displaced
        ],
        "rrf_score_modified": False,
        "relative_rrf_order_preserved": True,
    }


def retrieve_candidate_frontier(
    *,
    question: str,
    query_vector: np.ndarray,
    units: list[Unit],
    units_by_document: dict[str, list[Unit]],
    row_by_id: dict[str, dict[str, Any]],
    fts: FtsSearcher,
    document_vectors: np.ndarray,
) -> dict[str, Any]:
    del row_by_id  # retained in the accepted public call contract
    candidates, candidate_trace = fts.candidates(question)
    if candidates:
        stage1, stage1_trace = rerank_fts_candidates(
            candidates,
            question,
            candidate_trace=candidate_trace,
            top_k=len(candidates),
        )
        fallback = None
    else:
        stage1, full_trace = FullScanSearcher(units).search(
            question, top_k=len(units)
        )
        stage1_trace = {**candidate_trace, "rerank_trace": full_trace}
        fallback = "zero_fts_candidates_to_fullscan"
    document_ids = ordered_documents(stage1)[:STAGE1_DOCUMENT_COUNT]
    if len(document_ids) != STAGE1_DOCUMENT_COUNT:
        raise RuntimeError("T068 fewer than 30 lexical documents")
    stage2_units = [
        unit for document_id in document_ids for unit in units_by_document[document_id]
    ]
    lexical_full, stage2_trace = FullScanSearcher(stage2_units).search(
        question, top_k=STAGE2_INTERNAL_TOP_K
    )
    lexical = lexical_full[:LEXICAL_TOP_K]

    dense_scores = np.asarray(document_vectors @ query_vector, dtype=np.float32)
    dense_order = np.argsort(-dense_scores, kind="stable")[:DENSE_TOP_K]
    dense = [
        RankedUnit(
            unit=units[int(index)],
            score=float(dense_scores[int(index)]),
            matched_terms=0,
        )
        for index in dense_order
    ]
    fused, route_details = equal_rrf(lexical, dense)
    protected, admission_trace = apply_head_admission(
        fused,
        lexical,
        dense,
        lexical_head_depth=LEXICAL_HEAD_DEPTH,
        dense_head_depth=DENSE_HEAD_DEPTH,
    )
    candidate_ids = list(
        dict.fromkeys(
            [row.unit.unit_id for row in lexical]
            + [row.unit.unit_id for row in dense]
        )
    )
    candidate_order = [row.unit.unit_id for row in protected]
    if set(candidate_order) != set(candidate_ids):
        raise RuntimeError("T068 protected order changed candidate union")
    return {
        "candidate_ids": candidate_ids,
        "candidate_order": candidate_order,
        "trace": {
            "lexical_candidate_leaf_count": len(candidates),
            "lexical_fallback": fallback,
            "lexical_top50_ids": [row.unit.unit_id for row in lexical],
            "dense_top60_ids": [row.unit.unit_id for row in dense],
            "candidate_union_count": len(candidate_ids),
            "stage1_document_ids": document_ids,
            "stage1_trace": stage1_trace,
            "stage2_trace": stage2_trace,
            "route_details": route_details,
            "admission_trace": admission_trace,
        },
    }


def derive_native_endpoint(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("invalid embedding base URL")
    return (
        f"{parsed.scheme}://{parsed.netloc}"
        "/api/v1/services/embeddings/text-embedding/text-embedding"
    )


def normalize_rows(values: Any, dimension: int) -> np.ndarray:
    array = np.asarray(values, dtype=np.float32)
    if array.ndim != 2 or array.shape[1] != dimension:
        raise RuntimeError(f"unexpected embedding shape: {array.shape}")
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    if np.any(~np.isfinite(norms)) or np.any(norms <= 0):
        raise RuntimeError("embedding contains a non-finite or zero vector")
    return array / norms


class DashscopeNativeClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        dimension: int,
        timeout_seconds: float,
        max_retries: int,
        max_connections: int,
    ) -> None:
        if not api_key:
            raise RuntimeError("embedding API key is missing")
        self.api_key = api_key
        self.endpoint = derive_native_endpoint(base_url)
        self.endpoint_host = urlsplit(self.endpoint).hostname or ""
        self.model = model
        self.dimension = dimension
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._client = httpx.Client(
            timeout=timeout_seconds,
            trust_env=False,
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_connections,
            ),
        )
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._client.close()

    def embed(
        self,
        texts: list[str],
        *,
        text_type: str,
        instruct: str | None = None,
    ) -> tuple[np.ndarray, int, float]:
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32), 0, 0.0
        if text_type not in {"query", "document"}:
            raise RuntimeError(f"invalid text_type: {text_type}")
        parameters: dict[str, Any] = {
            "text_type": text_type,
            "dimension": self.dimension,
            "output_type": "dense",
        }
        if instruct:
            if text_type != "query":
                raise RuntimeError("instruct is allowed only for query embeddings")
            parameters["instruct"] = instruct
        payload = {
            "model": self.model,
            "input": {"texts": texts},
            "parameters": parameters,
        }
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            started = perf_counter()
            try:
                response = self._client.post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                body = response.json()
                if response.status_code >= 400 or body.get("code"):
                    code = body.get("code") or f"http_{response.status_code}"
                    raise RuntimeError(f"DashScope embedding request failed: {code}")
                rows = sorted(
                    ((body.get("output") or {}).get("embeddings") or []),
                    key=lambda item: int(item.get("text_index", 0)),
                )
                if len(rows) != len(texts):
                    raise RuntimeError(
                        f"embedding row count mismatch: {len(rows)} != {len(texts)}"
                    )
                vectors = normalize_rows(
                    [row.get("embedding") or [] for row in rows], self.dimension
                )
                tokens = int((body.get("usage") or {}).get("total_tokens") or 0)
                return vectors, tokens, (perf_counter() - started) * 1000.0
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(min(2**attempt, 16))
        raise RuntimeError("DashScope embedding failed after retries") from last_error


__all__ = (
    "DENSE_HEAD_DEPTH",
    "DENSE_TOP_K",
    "DashscopeNativeClient",
    "FtsSearcher",
    "FullScanSearcher",
    "LEXICAL_HEAD_DEPTH",
    "LEXICAL_TOP_K",
    "RankedUnit",
    "STAGE1_DOCUMENT_COUNT",
    "Unit",
    "apply_head_admission",
    "equal_rrf",
    "load_retrieval_units",
    "ordered_documents",
    "rerank_fts_candidates",
    "retrieve_candidate_frontier",
)
