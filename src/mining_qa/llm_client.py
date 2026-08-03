from dataclasses import dataclass
from time import perf_counter
from typing import Any

import httpx

from .config import Settings
from .llm_observability import LLMUsageLedger


@dataclass(frozen=True)
class CompletionResult:
    content: str
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        self._usage_ledger = LLMUsageLedger(settings)

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
        temperature: float | None = None,
    ) -> str:
        result = await self.complete_detailed(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return result.content

    async def complete_detailed(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> CompletionResult:
        if not self.enabled:
            return CompletionResult("模型 API Key 未配置，当前只能返回检索证据和限制说明。")

        payload: dict[str, Any] = {
            "model": self.settings.openai_model,
            "messages": messages,
            "temperature": self.settings.answer_temperature if temperature is None else temperature,
        }
        if max_tokens and max_tokens > 0:
            payload["max_tokens"] = int(max_tokens)
        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        started = perf_counter()
        try:
            response = await self._http_client().post(
                self.settings.chat_completions_url,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as error:
            self._usage_ledger.write_call(
                model=self.settings.openai_model,
                messages=messages,
                response_format="text",
                latency_ms=(perf_counter() - started) * 1000,
                success=False,
                error_type=type(error).__name__,
            )
            raise
        choice = data["choices"][0]
        usage = data.get("usage") or {}
        normalized_usage = self._usage_ledger.normalized_usage(usage)
        content = choice["message"]["content"].strip()
        self._usage_ledger.write_call(
            model=self.settings.openai_model,
            messages=messages,
            response_format="text",
            latency_ms=(perf_counter() - started) * 1000,
            success=True,
            usage=usage,
            output_chars=len(content),
            finish_reason=choice.get("finish_reason"),
        )
        return CompletionResult(
            content=content,
            finish_reason=choice.get("finish_reason"),
            **normalized_usage,
        )

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
    ) -> str:
        result = await self.complete_json_detailed(messages, max_tokens=max_tokens)
        return result.content

    async def complete_json_detailed(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
    ) -> CompletionResult:
        if not self.enabled:
            return CompletionResult("")

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
        started = perf_counter()
        try:
            response = await self._http_client().post(
                self.settings.chat_completions_url,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as error:
            self._usage_ledger.write_call(
                model=self.settings.openai_model,
                messages=messages,
                response_format="json_object",
                latency_ms=(perf_counter() - started) * 1000,
                success=False,
                error_type=type(error).__name__,
            )
            raise
        choice = data["choices"][0]
        usage = data.get("usage") or {}
        normalized_usage = self._usage_ledger.normalized_usage(usage)
        content = choice["message"]["content"].strip()
        self._usage_ledger.write_call(
            model=self.settings.openai_model,
            messages=messages,
            response_format="json_object",
            latency_ms=(perf_counter() - started) * 1000,
            success=True,
            usage=usage,
            output_chars=len(content),
            finish_reason=choice.get("finish_reason"),
        )
        return CompletionResult(
            content=content,
            finish_reason=choice.get("finish_reason"),
            **normalized_usage,
        )
