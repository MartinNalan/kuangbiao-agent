from __future__ import annotations

import os
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
