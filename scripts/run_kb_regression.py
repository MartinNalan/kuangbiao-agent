from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
KB_URL = os.getenv("KB_URL", "http://127.0.0.1:18081")
API_URL = os.getenv("API_URL", "http://127.0.0.1:18080")
API_KEY = os.getenv("API_KEY", "dev-local-key")


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
    assert_true(retrieval.get("vector_hits", 0) > 0, f"{query}: no vector hits")
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
    query = "我的采矿证是自然资源部颁发的，我的储量评审应该去哪个机构"
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
    assert_true(bool(expected), f"policy authority target clause not in top 3: {top3}")
    assert_true(result.get("retrieval", {}).get("graph_hits", 0) > 0, "policy authority should use graph hits")
    print("OK search: policy authority retrieval")


def assert_api() -> None:
    headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
    data = post_json(f"{API_URL}/api/ask", {"question": "哪个标准规定了金矿基本工程间距？"}, headers=headers)
    assert_true(data.get("status") == "answered", "API ask should be answered")
    assert_true(data.get("retrieval", {}).get("vector_hits", 0) > 0, "API ask should report vector hits")
    assert_true(data.get("retrieval", {}).get("graph_hits", 0) > 0, "API ask should report graph hits")
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
        if get_json(f"{KB_URL}/knowledge/health") is None:
            processes.append(
                start_process(
                    [python, "-m", "uvicorn", "mining_qa.knowledge_service:app", "--host", "127.0.0.1", "--port", "18081"],
                    env,
                )
            )
        wait_for(f"{KB_URL}/knowledge/health")

        health = get_json(f"{KB_URL}/knowledge/health") or {}
        assert_true(health.get("document_count", 0) >= 380, "KB should include standards and MNR policies")
        assert_true(health.get("chunk_count", 0) >= 20000, "KB should include clause-level chunks")
        assert_true(health.get("vector_count", 0) >= 20000, "KB health should include vector_count")
        assert_true(health.get("kg_entity_count", 0) >= 20000, "KB health should include kg_entity_count")
        assert_true(health.get("kg_relation_count", 0) >= 40000, "KB health should include kg_relation_count")
        print("OK health")

        assert_search("压覆矿产资源审批需要注意什么", "压覆矿产资源", "official_fulltext")
        assert_search("矿产资源法实施条例 战略性矿产资源目录", "中华人民共和国矿产资源法实施条例", "official_fulltext")
        assert_search("哪个标准规定了金矿基本工程间距？", "矿产地质勘查规范 岩金", "local_kb")
        assert_policy_authority()
        assert_catalog()

        if get_json(f"{API_URL}/health") is None:
            processes.append(
                start_process(
                    [python, "-m", "uvicorn", "mining_qa.api:app", "--host", "127.0.0.1", "--port", "18080"],
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
