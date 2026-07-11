from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str
    api_key: str
    base_url: str
    model: str
    dimensions: int
    batch_size: int

    @property
    def enabled(self) -> bool:
        return bool(self.provider and self.api_key and self.base_url and self.model)


def embedding_config(settings: Settings) -> EmbeddingConfig:
    api_key = settings.embedding_api_key or settings.dashscope_api_key
    return EmbeddingConfig(
        provider=settings.embedding_provider.strip().lower(),
        api_key=api_key.strip(),
        base_url=settings.embedding_base_url.rstrip("/"),
        model=settings.embedding_model.strip(),
        dimensions=settings.embedding_dimensions,
        batch_size=max(1, settings.embedding_batch_size),
    )


class EmbeddingProvider:
    def __init__(self, config: EmbeddingConfig, timeout_seconds: float = 60.0):
        self.config = config
        self.timeout_seconds = timeout_seconds

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.config.enabled:
            raise RuntimeError("embedding provider is not configured")
        if self.config.provider not in {"aliyun", "dashscope", "openai_compatible"}:
            raise RuntimeError(f"unsupported embedding provider: {self.config.provider}")
        return self._embed_openai_compatible(texts)

    def _embed_openai_compatible(self, texts: list[str]) -> list[list[float]]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "input": texts,
        }
        if self.config.dimensions > 0:
            payload["dimensions"] = self.config.dimensions
        with httpx.Client(timeout=self.timeout_seconds, trust_env=False) as client:
            response = client.post(
                f"{self.config.base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        rows = sorted(data.get("data") or [], key=lambda item: int(item.get("index", 0)))
        vectors = [normalize_dense_vector(row.get("embedding") or []) for row in rows]
        if len(vectors) != len(texts):
            raise RuntimeError(f"embedding provider returned {len(vectors)} vectors for {len(texts)} inputs")
        return vectors


def normalize_dense_vector(vector: list[Any]) -> list[float]:
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [round(value / norm, 8) for value in values]


def cosine_dense(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right))
