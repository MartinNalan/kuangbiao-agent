import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_URL = os.getenv("API_URL", "http://127.0.0.1:18080")
KB_URL = os.getenv("KB_URL", "http://127.0.0.1:18081")
API_KEY = os.getenv("API_KEY", "test-key")


def url_port(url: str) -> str:
    parsed = urlparse(url)
    if parsed.port is None:
        raise ValueError(f"URL must include an explicit port: {url}")
    return str(parsed.port)


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


def wait_for(url: str, timeout_seconds: float = 10.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error = None
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=1.0, trust_env=False)
            if response.status_code < 500:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def assert_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def post_ask(question: str, api_key: str | None = API_KEY) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    return httpx.post(f"{API_URL}/api/ask", headers=headers, json={"question": question}, timeout=10.0, trust_env=False)


def main() -> int:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": "src",
            "OPENAI_API_KEY": "",
            "KNOWLEDGE_BASE_URL": KB_URL,
            "API_KEYS": API_KEY,
            "RATE_LIMIT_PER_MINUTE": "100",
            "RATE_LIMIT_ENABLED": "true",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
        }
    )

    python = str(PROJECT_ROOT / ".venv" / "bin" / "python")
    uvicorn = [python, "-m", "uvicorn"]
    processes = [
        start_process(uvicorn + ["mining_qa.mock_kb:app", "--host", "127.0.0.1", "--port", url_port(KB_URL)], env),
        start_process(uvicorn + ["mining_qa.api:app", "--host", "127.0.0.1", "--port", url_port(API_URL)], env),
    ]

    try:
        wait_for(f"{KB_URL}/knowledge/health")
        wait_for(f"{API_URL}/health")

        unauthorized = post_ask("哪个规范规定了铁矿的推荐工程间距？", api_key=None)
        assert_equal(unauthorized.status_code, 401, "missing api key status")

        out_of_scope = post_ask("1+1=几？")
        assert_equal(out_of_scope.status_code, 200, "out-of-scope http status")
        assert_equal(out_of_scope.json()["status"], "out_of_scope", "out-of-scope response status")
        assert_equal(out_of_scope.json()["knowledge_gap_task"], None, "out-of-scope gap task")
        assert_equal(out_of_scope.json()["retrieval"]["full_text_hits"], 0, "out-of-scope retrieval")

        no_evidence = post_ask("哪个规范规定了铁矿的推荐工程间距？")
        assert_equal(no_evidence.status_code, 200, "no-evidence http status")
        assert_equal(no_evidence.json()["status"], "queued_for_enrichment", "no-evidence response status")
        if not no_evidence.json().get("knowledge_gap_task"):
            raise AssertionError("no-evidence response should include knowledge_gap_task")

        with_evidence = post_ask("哪个标准规定了金矿基本工程间距？")
        assert_equal(with_evidence.status_code, 200, "with-evidence http status")
        assert_equal(with_evidence.json()["status"], "answered", "with-evidence response status")
        assert_equal(with_evidence.json()["limitations"]["has_clause_level_evidence"], True, "with-evidence clause flag")
        if not with_evidence.json().get("sources"):
            raise AssertionError("with-evidence response should include sources")

        placer_gold = post_ask("沙金应该使用哪个标准")
        assert_equal(placer_gold.status_code, 200, "placer-gold http status")
        assert_equal(placer_gold.json()["status"], "answered", "placer-gold response status")
        if "DZ/T 0208-2020" not in placer_gold.json()["answer"]:
            raise AssertionError("placer-gold answer should identify DZ/T 0208-2020")

        projection = post_ask("关于矿体外推所依据的距离，是否存在不同标准规定不一致的情况，请帮我列举出来")
        assert_equal(projection.status_code, 200, "projection-comparison http status")
        assert_equal(projection.json()["status"], "answered", "projection-comparison response status")
        projection_answer = projection.json()["answer"]
        if "理论工程间距" not in projection_answer or "推断资源量工程间距" not in projection_answer:
            raise AssertionError("projection-comparison answer should compare distance bases")
        if not projection.json().get("sources") or len(projection.json()["sources"]) < 2:
            raise AssertionError("projection-comparison response should include multiple sources")

        standards = httpx.get(
            f"{API_URL}/api/standards",
            headers={"X-API-Key": API_KEY},
            params={"standard_no": "DZ/T 0321-2018"},
            timeout=10.0,
            trust_env=False,
        )
        assert_equal(standards.status_code, 200, "standards http status")
        assert_equal(standards.json()["pagination"]["total"], 1, "standards total")
        assert_equal(standards.json()["items"][0]["url"], "mock://standards/dzt-0321-2018", "standards source url")

        feedback = httpx.post(
            f"{API_URL}/api/feedback",
            headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
            json={
                "session_id": with_evidence.json()["session_id"],
                "rating": "unsatisfied",
                "question": "哪个标准规定了金矿基本工程间距？",
                "reason": "quote_too_long",
                "comment": "regression test feedback",
            },
            timeout=10.0,
            trust_env=False,
        )
        assert_equal(feedback.status_code, 200, "feedback http status")
        assert_equal(feedback.json()["ok"], True, "feedback response")

        usage = httpx.get(f"{API_URL}/api/usage", headers={"X-API-Key": API_KEY}, timeout=10.0, trust_env=False)
        assert_equal(usage.status_code, 200, "usage http status")
        if usage.json()["usage"]["total_calls"] < 1:
            raise AssertionError("usage should report at least one call")

        print("API regression passed.")
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
