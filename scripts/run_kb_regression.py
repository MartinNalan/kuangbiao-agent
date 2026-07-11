from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
KB_URL = os.getenv("KB_URL", "http://127.0.0.1:18081")
API_URL = os.getenv("API_URL", "http://127.0.0.1:18080")
API_KEY = os.getenv("API_KEY", "dev-local-key")
KB_DB_PATH = PROJECT_ROOT / "data" / "knowledge_base" / "db" / "knowledge_base.sqlite"


def service_address(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = str(parsed.port or (443 if parsed.scheme == "https" else 80))
    return host, port


def start_process(command: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        preexec_fn=os.setsid,
    )


def get_json(url: str, **kwargs: Any) -> dict[str, Any] | None:
    try:
        response = httpx.get(url, timeout=2.0, trust_env=False, **kwargs)
        if response.status_code < 500:
            return response.json()
    except Exception:  # noqa: BLE001
        return None
    return None


def wait_for(url: str, timeout_seconds: float = 20.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if get_json(url) is not None:
            return
        time.sleep(0.3)
    raise RuntimeError(f"Timed out waiting for {url}")


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    response = httpx.post(url, json=payload, headers=headers, timeout=30.0, trust_env=False)
    response.raise_for_status()
    return response.json()


def assert_true(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)


def assert_search(query: str, expected_title: str, expected_source_type: str | None = None) -> None:
    result = post_json(
        f"{KB_URL}/knowledge/search",
        {"query": query, "options": {"top_k": 5, "include_full_text": False}},
    )
    hits = result.get("results") or []
    retrieval = result.get("retrieval") or {}
    assert_true(bool(hits), f"{query}: no search hits")
    assert_true(retrieval.get("full_text_hits", 0) > 0, f"{query}: no full-text hits")
    assert_true(
        retrieval.get("vector_hits", 0) > 0 or retrieval.get("vector_skipped", 0) == 1,
        f"{query}: vector retrieval neither used nor deliberately skipped",
    )
    assert_true(retrieval.get("graph_hits", 0) > 0, f"{query}: no graph hits")
    top_titles = [hit.get("title") or "" for hit in hits[:3]]
    assert_true(any(expected_title in title for title in top_titles), f"{query}: expected title not in top 3: {top_titles}")
    if expected_source_type:
        top_source_types = [hit.get("source_type") for hit in hits[:3]]
        assert_true(
            expected_source_type in top_source_types,
            f"{query}: expected source type {expected_source_type!r} not in top 3: {top_source_types}",
        )
    for hit in hits[:3]:
        quote = hit.get("quote") or ""
        assert_true(len(quote) <= 360, f"{query}: quote too long: {len(quote)} chars")
        assert_true("text" not in hit, f"{query}: raw full text leaked without include_full_text")
    print(f"OK search: {query}")


def assert_catalog() -> None:
    response = httpx.get(
        f"{KB_URL}/knowledge/standards",
        params={"q": "中华人民共和国矿产资源法实施条例", "page_size": 5},
        timeout=10.0,
        trust_env=False,
    )
    response.raise_for_status()
    data = response.json()
    assert_true(data["pagination"]["total"] >= 1, "policy catalog lookup should find documents")
    print("OK catalog: policy document lookup")


def assert_policy_authority() -> None:
    queries = [
        "我的采矿证是自然资源部颁发的，我的储量评审应该去哪个机构",
        "我是一个大型的金矿，我的储量报告评审应该去哪个机构",
    ]
    for query in queries:
        result = post_json(
            f"{KB_URL}/knowledge/search",
            {"query": query, "options": {"top_k": 5, "include_full_text": False}},
        )
        hits = result.get("results") or []
        top3 = hits[:3]
        expected = [
            hit
            for hit in top3
            if hit.get("standard_no") == "自然资规〔2023〕6号"
            and hit.get("clause_no") == "十、"
            and "自然资源部负责本级已颁发勘查许可证或采矿许可证" in (hit.get("quote") or "")
        ]
        assert_true(bool(expected), f"policy authority target clause not in top 3 for {query}: {top3}")
        target = expected[0]
        assert_true(bool(target.get("url")), f"policy authority target should include official URL for {query}")
        assert_true(
            "f.mnr.gov.cn" in target.get("url", ""),
            f"policy authority URL should be MNR official URL for {query}: {target.get('url')}",
        )
        assert_true(
            target.get("source_platform") == "自然资源部政策法规库",
            f"policy authority source platform should be MNR policy DB for {query}: {target.get('source_platform')}",
        )
        if "大型" in query and "金矿" in query:
            unrelated = [
                hit
                for hit in top3
                if any(term in " ".join(str(hit.get(key) or "") for key in ("title", "section_path", "quote")) for term in ("油气", "煤层气"))
                and hit is not target
            ]
            assert_true(
                not unrelated,
                f"solid-mineral authority query should downrank unrelated oil/gas evidence for {query}: {unrelated}",
            )
        assert_true(result.get("retrieval", {}).get("graph_hits", 0) > 0, "policy authority should use graph hits")
    print("OK search: policy authority retrieval")


def assert_background_context_does_not_trigger_authority() -> None:
    result = post_json(
        f"{KB_URL}/knowledge/search",
        {"query": "大型金矿基本工程间距是多少", "options": {"top_k": 5, "include_full_text": False}},
    )
    hits = result.get("results") or []
    top3 = hits[:3]
    assert_true(bool(top3), "large gold engineering spacing query should return evidence")
    assert_true(
        any("矿产地质勘查规范 岩金" in (hit.get("title") or "") for hit in top3),
        f"large gold engineering spacing should prioritize rock-gold technical standard evidence: {top3}",
    )
    polluted = [
        hit
        for hit in top3
        if hit.get("standard_no") == "自然资规〔2023〕6号"
        or "评审备案范围和权限" in (hit.get("section_path") or "")
    ]
    assert_true(
        not polluted,
        f"background mineral/scale wording should not trigger policy-authority evidence for technical questions: {polluted}",
    )
    print("OK search: background context does not trigger authority")


def assert_equivalent_engineering_distance_questions() -> None:
    questions = [
        "金矿勘查1类型的推荐工程间距是多少？",
        "金矿勘查Ⅰ类型的推荐工程间距是多少？",
        "金矿勘查一类型的推荐工程间距是多少？",
    ]
    signatures = []
    for query in questions:
        result = post_json(
            f"{KB_URL}/knowledge/search",
            {"query": query, "options": {"top_k": 5, "include_full_text": False}},
        )
        hits = result.get("results") or []
        assert_true(bool(hits), f"{query}: no evidence")
        top = hits[0]
        assert_true(top.get("standard_no") == "DZ/T 0205-2020", f"{query}: wrong standard: {top}")
        quote = top.get("quote") or ""
        for label in ("坑探-穿脉", "坑探-沿脉", "钻探-走向", "钻探-倾斜"):
            assert_true(f"{label} 80～160 m" in quote, f"{query}: missing {label}: {quote}")
        retrieval = result.get("retrieval") or {}
        assert_true(retrieval.get("scoped_search") == 1, f"{query}: candidate standard scope not used")
        assert_true(retrieval.get("vector_skipped") == 1, f"{query}: unnecessary vector retrieval ran")
        signatures.append((top.get("standard_no"), top.get("section_path"), quote))
    assert_true(len(set(signatures)) == 1, f"equivalent questions returned different evidence: {signatures}")
    print("OK search: equivalent engineering-distance questions")


def assert_high_value_intent_routes() -> None:
    cases = [
        (
            "采矿证办理应该依据哪个文件",
            "自然资规〔2023〕4号",
            "附件4",
        ),
        (
            "资源量估算中，无限外推是推1/2还是1/4",
            "DZ/T 0338.1-2020",
            "经验工程间距1/2尖推",
        ),
        (
            "根据矿产资源法实施条例，资源储量报告的真实性由谁负责",
            "国令第839号",
            "储量报告的真实性负责",
        ),
    ]
    for query, expected_no, expected_quote in cases:
        result = post_json(
            f"{KB_URL}/knowledge/search",
            {"query": query, "options": {"top_k": 5, "include_full_text": False}},
        )
        hits = result.get("results") or []
        assert_true(bool(hits), f"{query}: no evidence")
        top = hits[0]
        assert_true(top.get("standard_no") == expected_no, f"{query}: wrong top document: {top}")
        compact_quote = "".join((top.get("quote") or "").split())
        assert_true(expected_quote in compact_quote, f"{query}: missing required evidence: {top.get('quote')}")
        assert_true(
            result.get("coverage", {}).get("has_clause_level_evidence") is True,
            f"{query}: strict evidence route was not accepted",
        )
        assert_true(
            not any(hit.get("standard_no") == "自然资规〔2023〕6号" for hit in hits[:3])
            or expected_no == "自然资规〔2023〕6号",
            f"{query}: unrelated authority policy polluted top 3",
        )
    print("OK search: high-value intent routes")


def assert_service_guide_inventory() -> None:
    conn = sqlite3.connect(KB_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            select document_id, official_url, source_trace_json
            from documents
            where document_type = 'service_guide' and status = 'current' and can_answer = 1
            """
        ).fetchall()
    finally:
        conn.close()
    urls = [row["official_url"] for row in rows]
    page_ids = []
    for row in rows:
        trace = json.loads(row["source_trace_json"] or "{}")
        page_ids.append(str(trace.get("source_page_id") or ""))
    assert_true(len(rows) == 40, f"expected 40 active service guides, found {len(rows)}")
    assert_true(all(urls) and len(set(urls)) == 40, "service-guide source URLs must be non-empty and unique")
    assert_true(all(page_ids) and len(set(page_ids)) == 40, "service-guide source page IDs must be non-empty and unique")
    print("OK inventory: 40 unique service guides")


def assert_service_guide_search() -> None:
    cases = [
        (
            "自然资源部探矿权首次登记需要哪些材料？",
            "探矿权首次登记临时服务指南",
            ("申请材料",),
            "探矿权登记申请书",
        ),
        (
            "采矿许可变更开采方式怎么办理？",
            "采矿许可变更（开采方式）申请临时服务指南",
            ("办理基本流程", "办理方式", "申请材料提交"),
            "接收报件和受理",
        ),
        (
            "矿产资源储量评审备案需要提交什么材料？",
            "矿产资源储量评审备案服务指南",
            ("申请材料",),
            "矿产资源储量评审备案申请函",
        ),
        (
            "矿产资源开采方案的办结时限是多久？",
            "矿产资源开采方案临时服务指南",
            ("办结时限",),
            "10个工作日",
        ),
    ]
    for query, expected_title, allowed_sections, expected_quote in cases:
        result = post_json(
            f"{KB_URL}/knowledge/search",
            {"query": query, "options": {"top_k": 5, "include_full_text": False}},
        )
        hits = result.get("results") or []
        assert_true(bool(hits), f"{query}: no service-guide evidence")
        assert_true(
            all(hit.get("title") == expected_title for hit in hits),
            f"{query}: unrelated guide returned: {[hit.get('title') for hit in hits]}",
        )
        assert_true(
            all(any(section in (hit.get("section_path") or "") for section in allowed_sections) for hit in hits),
            f"{query}: unrelated section returned: {[hit.get('section_path') for hit in hits]}",
        )
        assert_true(
            expected_quote in " ".join(hit.get("quote") or "" for hit in hits),
            f"{query}: expected evidence not returned: {hits}",
        )
        for hit in hits:
            assert_true(
                (hit.get("url") or "").startswith("https://www.mnr.gov.cn/"),
                f"{query}: missing official MNR guide URL: {hit}",
            )
            assert_true(
                hit.get("source_platform") == "自然资源部政务服务办事指南",
                f"{query}: wrong source platform: {hit.get('source_platform')}",
            )
            assert_true("text" not in hit, f"{query}: raw full text leaked")
        retrieval = result.get("retrieval") or {}
        assert_true(retrieval.get("full_text_hits", 0) > 0, f"{query}: no full-text retrieval")
        assert_true(retrieval.get("graph_hits", 0) > 0, f"{query}: no graph retrieval")
        assert_true(result.get("coverage", {}).get("has_clause_level_evidence") is True, f"{query}: evidence rejected")
    print("OK search: MNR service-guide routes")


T018_EXTENSION_MATERIALS = [
    "采矿权申请登记书或申请书",
    "矿产资源储量评审备案文件",
    "外商投资企业批准证书",
    "采矿许可证正、副本",
    "矿山地质环境保护与土地复垦方案公告结果",
    "矿产资源开发利用方案和专家审查意见",
    "对外合作合同副本等有关批准文件",
    "矿业权出让收益（价款）缴纳或有偿处置证明材料",
    "申请人的企业营业执照副本",
    "省级自然资源主管部门意见",
]


def assert_t018_inventory() -> None:
    conn = sqlite3.connect(KB_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        document = conn.execute(
            "select * from documents where document_type = 'policy_attachment' and standard_no = '自然资规〔2023〕4号附件4'"
        ).fetchone()
        assert_true(document is not None, "T018 attachment document missing")
        chunks = conn.execute(
            "select chunk_type, section_path, table_json from chunks where document_id = ?",
            (document["document_id"],),
        ).fetchall()
    finally:
        conn.close()
    trace = json.loads(document["source_trace_json"] or "{}")
    quality = json.loads(document["quality_json"] or "{}")
    material_rows = [row for row in chunks if row["chunk_type"] == "application_material_row"]
    application_keys = [json.loads(row["table_json"])["application_key"] for row in material_rows]
    assert_true(len(chunks) == 93, f"T018 expected 93 chunks, found {len(chunks)}")
    assert_true(len(material_rows) == 80, f"T018 expected 80 material rows, found {len(material_rows)}")
    assert_true(len(set(application_keys)) == 8, f"T018 expected 8 isolated leaf application types: {set(application_keys)}")
    assert_true(quality.get("physical_table_count") == 2, "T018 physical table count mismatch")
    assert_true(quality.get("logical_table_count") == 1, "T018 logical table count mismatch")
    assert_true(quality.get("logical_row_count") == 21, "T018 source material row count mismatch")
    assert_true(quality.get("ambiguous_merge_count") == 0, "T018 should have no ambiguous merged cells")
    assert_true(trace.get("parent_document_id") == "policy-d4869b5b5bf8804f", "T018 parent link missing")
    assert_true(len(trace.get("service_guide_links") or []) == 17, "T018 should link 17 matching service guides")
    assert_true(document["official_url"] == "https://f.mnr.gov.cn/202305/P020230512660474974800.doc", "T018 official URL mismatch")
    comparison = PROJECT_ROOT / "data" / "knowledge_base" / "manifests" / "t018_service_guide_comparison.json"
    assert_true(comparison.exists(), "T018 service-guide comparison manifest missing")
    print("OK inventory: T018 structured attachment")


def assert_t018_search() -> None:
    query = "采矿证延续需要提交什么材料？"
    result = post_json(
        f"{KB_URL}/knowledge/search",
        {"query": query, "options": {"top_k": 10, "include_full_text": False}},
    )
    hits = result.get("results") or []
    assert_true(len(hits) == 10, f"{query}: expected complete 10-row extension list, found {len(hits)}")
    assert_true(
        all(hit.get("title") == "采矿权申请资料清单及要求" for hit in hits),
        f"{query}: unrelated source mixed into extension list: {hits}",
    )
    assert_true(
        all(hit.get("source_role") == "policy_attachment" for hit in hits),
        f"{query}: source role should identify attachment evidence",
    )
    assert_true(
        all("附件4 > 延续 > 材料" in (hit.get("section_path") or "") for hit in hits),
        f"{query}: another application type leaked into extension rows",
    )
    sequences = sorted(
        int(re.search(r"材料\s*(\d+)", hit.get("section_path") or "").group(1))
        for hit in hits
    )
    assert_true(sequences == [1, 2, 3, 4, 5, 7, 18, 19, 20, 21], f"{query}: wrong row isolation {sequences}")
    combined_quotes = "\n".join(hit.get("quote") or "" for hit in hits)
    for material in T018_EXTENSION_MATERIALS:
        assert_true(material in combined_quotes, f"{query}: missing material {material}")
    assert_true(
        all(hit.get("url") == "https://f.mnr.gov.cn/202305/P020230512660474974800.doc" for hit in hits),
        f"{query}: attachment URL missing",
    )
    assert_true(not any(hit.get("standard_no") == "自然资规〔2023〕6号" for hit in hits), f"{query}: unrelated policy leaked")
    assert_true(result.get("retrieval", {}).get("graph_hits", 0) > 0, f"{query}: no graph evidence")
    assert_true(result.get("coverage", {}).get("has_clause_level_evidence") is True, f"{query}: evidence rejected")

    basis_query = "采矿证办理应该依据哪个文件？"
    basis = post_json(
        f"{KB_URL}/knowledge/search",
        {"query": basis_query, "options": {"top_k": 5, "include_full_text": False}},
    )
    basis_hits = basis.get("results") or []
    assert_true(bool(basis_hits), f"{basis_query}: no evidence")
    assert_true(basis_hits[0].get("standard_no") == "自然资规〔2023〕4号", f"{basis_query}: wrong parent policy")
    assert_true(basis_hits[0].get("source_role") == "parent_policy", f"{basis_query}: parent policy role missing")
    assert_true(basis_hits[0].get("url") == "https://f.mnr.gov.cn/202305/t20230512_2786192.html", f"{basis_query}: parent URL missing")
    print("OK search: T018 attachment isolation and parent-policy routing")


def assert_api() -> None:
    headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
    data = post_json(f"{API_URL}/api/ask", {"question": "哪个标准规定了金矿基本工程间距？"}, headers=headers)
    assert_true(data.get("status") == "answered", "API ask should be answered")
    assert_true(data.get("retrieval", {}).get("full_text_hits", 0) > 0, "API ask should report selected evidence")
    assert_true(data.get("retrieval", {}).get("vector_hits", 0) == 0, "simple scoped lookup should skip vectors")
    assert_true(data.get("retrieval", {}).get("graph_hits", 0) > 0, "API ask should report graph hits")
    numeric = post_json(
        f"{API_URL}/api/ask",
        {"question": "资源量估算中，无限外推是推1/2还是1/4"},
        headers=headers,
    )
    assert_true(numeric.get("status") == "answered", "numeric projection should be answered")
    assert_true("1/2 尖推" in numeric.get("answer", ""), "numeric projection should answer 1/2尖推")
    authenticity = post_json(
        f"{API_URL}/api/ask",
        {"question": "根据矿产资源法实施条例，资源储量报告的真实性由谁负责"},
        headers=headers,
    )
    assert_true(authenticity.get("status") == "answered", "authenticity responsibility should be answered")
    assert_true("矿业权人负责" in authenticity.get("answer", ""), "authenticity answer should name矿业权人")
    assert_true("许可证颁发层级" not in authenticity.get("answer", ""), "authenticity answer used authority routing")
    service_cases = [
        ("自然资源部探矿权首次登记需要哪些材料？", "探矿权首次登记临时服务指南", "探矿权登记申请书"),
        ("采矿许可变更开采方式怎么办理？", "采矿许可变更（开采方式）申请临时服务指南", "接收报件和受理"),
        ("矿产资源储量评审备案需要提交什么材料？", "矿产资源储量评审备案服务指南", "矿产资源储量评审备案申请函"),
        ("矿产资源开采方案的办结时限是多久？", "矿产资源开采方案临时服务指南", "10个工作日"),
    ]
    for question, expected_title, expected_text in service_cases:
        service = post_json(f"{API_URL}/api/ask", {"question": question}, headers=headers)
        assert_true(service.get("status") == "answered", f"{question}: API should answer from service guide")
        assert_true(expected_text in service.get("answer", ""), f"{question}: answer missing direct guide evidence")
        matching_sources = [source for source in service.get("sources") or [] if source.get("title") == expected_title]
        assert_true(bool(matching_sources), f"{question}: matching guide source missing")
        assert_true(
            all((source.get("url") or "").startswith("https://www.mnr.gov.cn/") for source in matching_sources),
            f"{question}: official guide URL missing",
        )
    extension_question = "采矿证延续需要提交什么材料？"
    extension = post_json(f"{API_URL}/api/ask", {"question": extension_question}, headers=headers)
    assert_true(extension.get("status") == "answered", "T018 extension materials should be answered")
    assert_true("当前限制" not in extension.get("answer", ""), "T018 answer still reports attachment as unparsed")
    for material in T018_EXTENSION_MATERIALS:
        assert_true(material in extension.get("answer", ""), f"T018 API answer missing material {material}")
    extension_sources = extension.get("sources") or []
    assert_true(len(extension_sources) == 10, f"T018 API should cite 10 isolated material rows: {extension_sources}")
    assert_true(
        all(source.get("source_role") == "policy_attachment" for source in extension_sources),
        "T018 API sources should identify policy_attachment",
    )
    assert_true(
        all(source.get("url") == "https://f.mnr.gov.cn/202305/P020230512660474974800.doc" for source in extension_sources),
        "T018 API sources should expose the official attachment URL",
    )
    basis_question = "采矿证办理应该依据哪个文件？"
    basis = post_json(f"{API_URL}/api/ask", {"question": basis_question}, headers=headers)
    assert_true(basis.get("status") == "answered", "T018 parent-policy basis question should be answered")
    assert_true("自然资规〔2023〕4号" in basis.get("answer", ""), "T018 basis answer should cite parent policy")
    assert_true(
        any(source.get("source_role") == "parent_policy" for source in basis.get("sources") or []),
        "T018 basis API source should identify parent_policy",
    )
    print("OK API: /api/ask end-to-end")


def main() -> int:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": "src",
            "KNOWLEDGE_BASE_URL": KB_URL,
            "API_KEYS": API_KEY,
            "RATE_LIMIT_ENABLED": "false",
            "OPENAI_API_KEY": "",
        }
    )
    python = str(PROJECT_ROOT / ".venv" / "bin" / "python")
    processes: list[subprocess.Popen[str]] = []

    try:
        kb_host, kb_port = service_address(KB_URL)
        api_host, api_port = service_address(API_URL)
        if get_json(f"{KB_URL}/knowledge/health") is None:
            processes.append(
                start_process(
                    [python, "-m", "uvicorn", "mining_qa.knowledge_service:app", "--host", kb_host, "--port", kb_port],
                    env,
                )
            )
        wait_for(f"{KB_URL}/knowledge/health")

        health = get_json(f"{KB_URL}/knowledge/health") or {}
        assert_true(health.get("document_count", 0) >= 110, "KB should include governed standards and MNR corpora")
        assert_true(health.get("chunk_count", 0) >= 20000, "KB should include clause-level chunks")
        assert_true(health.get("vector_count", 0) >= 20000, "KB health should include vector_count")
        assert_true(health.get("kg_entity_count", 0) >= 20000, "KB health should include kg_entity_count")
        assert_true(health.get("kg_relation_count", 0) >= 40000, "KB health should include kg_relation_count")
        print("OK health")

        assert_search("压覆矿产资源审批需要注意什么", "压覆矿产资源", "official_fulltext")
        assert_search("矿产资源法实施条例 战略性矿产资源目录", "中华人民共和国矿产资源法实施条例", "official_fulltext")
        assert_search("哪个标准规定了金矿基本工程间距？", "矿产地质勘查规范 岩金", "local_kb")
        assert_policy_authority()
        assert_background_context_does_not_trigger_authority()
        assert_equivalent_engineering_distance_questions()
        assert_high_value_intent_routes()
        assert_service_guide_inventory()
        assert_service_guide_search()
        assert_t018_inventory()
        assert_t018_search()
        assert_catalog()

        if get_json(f"{API_URL}/health") is None:
            processes.append(
                start_process(
                    [python, "-m", "uvicorn", "mining_qa.api:app", "--host", api_host, "--port", api_port],
                    env,
                )
            )
        wait_for(f"{API_URL}/health")
        assert_api()
        print("KB regression passed.")
        return 0
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)


if __name__ == "__main__":
    raise SystemExit(main())
