from typing import Any

import httpx

from .config import Settings


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.openai_api_key.strip())

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds,
                trust_env=False,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
    ) -> str:
        if not self.enabled:
            return "模型 API Key 未配置，当前只能返回检索证据和限制说明。"

        payload: dict[str, Any] = {
            "model": self.settings.openai_model,
            "messages": messages,
            "temperature": self.settings.answer_temperature,
        }
        if max_tokens and max_tokens > 0:
            payload["max_tokens"] = int(max_tokens)
        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        response = await self._http_client().post(
            self.settings.chat_completions_url,
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        return data["choices"][0]["message"]["content"].strip()

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
    ) -> str:
        if not self.enabled:
            return ""

        payload: dict[str, Any] = {
            "model": self.settings.openai_model,
            "messages": messages,
            "temperature": self.settings.structured_temperature,
            "response_format": {"type": "json_object"},
        }
        if (
            "deepseek" in self.settings.openai_base_url.lower()
            or self.settings.openai_model.lower().startswith("deepseek")
        ):
            payload["thinking"] = {"type": "disabled"}
        if max_tokens and max_tokens > 0:
            payload["max_tokens"] = int(max_tokens)
        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        response = await self._http_client().post(
            self.settings.chat_completions_url,
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        return data["choices"][0]["message"]["content"].strip()
