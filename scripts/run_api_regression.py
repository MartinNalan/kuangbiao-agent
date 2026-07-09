import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_URL = "http://127.0.0.1:18080"
KB_URL = "http://127.0.0.1:18081"
API_KEY = "test-key"


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
        start_process(uvicorn + ["mining_qa.mock_kb:app", "--host", "127.0.0.1", "--port", "18081"], env),
        start_process(uvicorn + ["mining_qa.api:app", "--host", "127.0.0.1", "--port", "18080"], env),
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

        standards = httpx.get(
            f"{API_URL}/api/standards",
            headers={"X-API-Key": API_KEY},
            params={"standard_no": "DZ/T 0321-2018"},
            timeout=10.0,
            trust_env=False,
        )
        assert_equal(standards.status_code, 200, "standards http status")
        assert_equal(standards.json()["pagination"]["total"], 1, "standards total")

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
