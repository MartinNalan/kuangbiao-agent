from __future__ import annotations

import os
import time
from typing import Any

import httpx


API_BASE_URL = os.getenv("MINING_QA_API_URL", "http://127.0.0.1:18080")
API_KEY = os.getenv("MINING_QA_API_KEY", "dev-local-key")


def request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    headers = kwargs.pop("headers", {})
    headers["X-API-Key"] = API_KEY
    with httpx.Client(base_url=API_BASE_URL, timeout=60.0, trust_env=False) as client:
        response = client.request(method, path, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json()


def ask(question: str) -> dict[str, Any]:
    return request("POST", "/api/ask", json={"question": question})


def research(
    question: str,
    session_id: str | None = None,
    source_request_id: str | None = None,
) -> dict[str, Any]:
    task = request(
        "POST",
        "/api/research/tasks",
        json={
            "question": question,
            "session_id": session_id,
            "source_request_id": source_request_id,
        },
    )
    terminal = {"completed", "partial", "insufficient_evidence", "failed", "cancelled"}
    while task["status"] not in terminal:
        time.sleep(1.2)
        task = request("GET", f"/api/research/tasks/{task['task_id']}")
    if not task.get("result_available"):
        return task
    return request("GET", f"/api/research/tasks/{task['task_id']}/result")


def standards(standard_no: str) -> dict[str, Any]:
    return request("GET", "/api/standards", params={"standard_no": standard_no, "page_size": 5})


def feedback(session_id: str, question: str) -> dict[str, Any]:
    return request(
        "POST",
        "/api/feedback",
        json={
            "session_id": session_id,
            "rating": "satisfied",
            "question": question,
        },
    )


def main() -> None:
    question = "哪个标准规定了金矿基本工程间距？"
    answer = ask(question)
    print("status:", answer["status"])
    print("answer:", answer["answer"])
    print("sources:", len(answer.get("sources", [])))

    catalog = standards("DZ/T 0205-2020")
    print("catalog_total:", catalog["pagination"]["total"])

    result = feedback(answer["session_id"], question)
    print("feedback:", result)


if __name__ == "__main__":
    main()
