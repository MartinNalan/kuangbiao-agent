from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from threading import Lock, local
from time import perf_counter
import types
from typing import Any

import numpy as np

from .config import Settings, get_settings
from .query_understanding import QueryPlan, query_plan_from_payload
from .v4_retrieval_store import (
    DIMENSION,
    MODEL,
    QUERY_INSTRUCT,
    STATUS_MARKERS,
    V4KnowledgeStore,
    _load_t068,
    _resolve_artifact,
    sha256_file,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME_MANIFEST = (
    PROJECT_ROOT
    / "data"
    / "knowledge_base_v4"
    / "runtime_private"
    / "hybrid_fixed20_v2"
    / "runtime_manifest.json"
)


class ResilientQwenQueryEmbedder:
    """Bounded, cached and single-flight query Embedding for the v4 runtime."""

    def __init__(
        self,
        t068: types.ModuleType | None = None,
        *,
        settings: Settings | None = None,
        client: Any | None = None,
    ) -> None:
        runtime_settings = settings or get_settings()
        if client is None:
            runtime = t068 or _load_t068()
            api_key = (
                runtime_settings.embedding_api_key
                or runtime_settings.dashscope_api_key
            ).strip()
            base_url = runtime_settings.embedding_base_url.strip().rstrip("/")
            configured_model = os.getenv("V4_EMBEDDING_MODEL", MODEL).strip() or MODEL
            configured_dimension = int(
                os.getenv("V4_EMBEDDING_DIMENSIONS", str(DIMENSION)) or DIMENSION
            )
            if not api_key or not base_url:
                raise RuntimeError("v4 query embedding is not configured")
            if configured_model != MODEL:
                raise RuntimeError(
                    f"v4 runtime requires {MODEL}, configured {configured_model}"
                )
            if configured_dimension != DIMENSION:
                raise RuntimeError(
                    f"v4 runtime requires dimension {DIMENSION}, "
                    f"configured {configured_dimension}"
                )
            client = runtime.t063.t036.DashscopeNativeClient(
                api_key=api_key,
                base_url=base_url,
                model=MODEL,
                dimension=DIMENSION,
                timeout_seconds=runtime_settings.v4_embedding_timeout_seconds,
                max_retries=runtime_settings.v4_embedding_max_retries,
                max_connections=4,
            )
        self._client = client
        self._cache_size = runtime_settings.v4_query_embedding_cache_size
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._inflight: dict[str, Future[np.ndarray]] = {}
        self._state_lock = Lock()
        self._call_state = local()
        self.timeout_seconds = runtime_settings.v4_embedding_timeout_seconds
        self.max_retries = runtime_settings.v4_embedding_max_retries

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _set_metrics(self, **values: Any) -> None:
        self._call_state.metrics = values

    def last_call_metrics(self) -> dict[str, Any]:
        return dict(getattr(self._call_state, "metrics", {}) or {})

    def embed_query(self, text: str) -> np.ndarray:
        key = self._key(text)
        wait_started = perf_counter()
        with self._state_lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                self._set_metrics(
                    cache_hit=True,
                    singleflight_wait=False,
                    provider_called=False,
                    wait_ms=0.0,
                    provider_ms=0.0,
                    timeout_seconds=self.timeout_seconds,
                    max_retries=self.max_retries,
                )
                return cached
            future = self._inflight.get(key)
            owner = future is None
            if future is None:
                future = Future()
                self._inflight[key] = future

        if not owner:
            vector = future.result()
            self._set_metrics(
                cache_hit=False,
                singleflight_wait=True,
                provider_called=False,
                wait_ms=round((perf_counter() - wait_started) * 1000.0, 3),
                provider_ms=0.0,
                timeout_seconds=self.timeout_seconds,
                max_retries=self.max_retries,
            )
            return vector

        provider_started = perf_counter()
        try:
            vectors, _, _ = self._client.embed(
                [text],
                text_type="query",
                instruct=QUERY_INSTRUCT,
            )
            vector = np.asarray(vectors[0], dtype=np.float32).copy()
            vector.setflags(write=False)
        except Exception as exc:
            with self._state_lock:
                self._inflight.pop(key, None)
                future.set_exception(exc)
            self._set_metrics(
                cache_hit=False,
                singleflight_wait=False,
                provider_called=True,
                wait_ms=0.0,
                provider_ms=round((perf_counter() - provider_started) * 1000.0, 3),
                timeout_seconds=self.timeout_seconds,
                max_retries=self.max_retries,
                error_type=type(exc).__name__,
            )
            raise

        provider_ms = (perf_counter() - provider_started) * 1000.0
        with self._state_lock:
            self._cache[key] = vector
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
            self._inflight.pop(key, None)
            future.set_result(vector)
        self._set_metrics(
            cache_hit=False,
            singleflight_wait=False,
            provider_called=True,
            wait_ms=0.0,
            provider_ms=round(provider_ms, 3),
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
        )
        return vector

    def close(self) -> None:
        self._client.close()


class ResilientV4KnowledgeStore(V4KnowledgeStore):
    """v2 runtime: fast lexical fallback plus duplicate-search single-flight."""

    def __init__(
        self,
        manifest_path: Path = DEFAULT_RUNTIME_MANIFEST,
        *,
        query_embedder: Any | None = None,
        legacy_admin_store: Any | None = None,
        validate_hashes: bool = True,
    ) -> None:
        configuration_error: str | None = None
        if query_embedder is None:
            try:
                query_embedder = ResilientQwenQueryEmbedder()
            except RuntimeError as exc:
                configuration_error = str(exc)
                # A temporary placeholder prevents the v1 constructor from
                # silently restoring the old 60-second Embedding contract.
                query_embedder = _UnavailableQueryEmbedder(configuration_error)
        super().__init__(
            manifest_path,
            query_embedder=query_embedder,
            legacy_admin_store=legacy_admin_store,
            validate_hashes=validate_hashes,
        )
        if configuration_error:
            self.query_embedder = None
            self.embedding_configuration_error = configuration_error
        self._search_state_lock = Lock()
        self._search_inflight: dict[str, Future[dict[str, Any]]] = {}
        self._search_call_state = local()

    def _validate_manifest(self, *, validate_hashes: bool) -> None:
        super()._validate_manifest(validate_hashes=validate_hashes)
        if not validate_hashes:
            return
        base = self.manifest["runtime_sources"].get("base_adapter")
        if not base or sha256_file(_resolve_artifact(base)) != base["sha256"]:
            raise RuntimeError("v4 runtime source changed: base_adapter")

    @staticmethod
    def _route_texts(query: str, plan: QueryPlan) -> tuple[str, str, str]:
        if plan.governed_intent and plan.governed_intent != "other":
            return (
                plan.lexical_query or plan.normalized_query or query,
                plan.semantic_query or plan.normalized_query or query,
                plan.structural_query or plan.normalized_query or query,
            )
        return query, query, plan.structural_query or query

    def _search_key(self, payload: dict[str, Any]) -> str:
        query = str(payload.get("query") or "").strip()
        plan = query_plan_from_payload(query, payload.get("retrieval_plan"))
        lexical, semantic, structural = self._route_texts(query, plan)
        status_numbers = list(plan.standard_numbers) if any(
            marker in query for marker in STATUS_MARKERS
        ) else []
        contract = {
            "query": query,
            "lexical": lexical,
            "semantic": semantic,
            "structural": structural,
            "retrieval_allowed": plan.retrieval_allowed,
            "status_numbers": status_numbers,
            "filters": payload.get("filters") or {},
            "options": payload.get("options") or {},
        }
        encoded = json.dumps(
            contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _embed_query(self, text: str) -> tuple[np.ndarray | None, float, str | None]:
        result = super()._embed_query(text)
        metrics = getattr(self.query_embedder, "last_call_metrics", None)
        self._search_call_state.embedding_runtime = (
            metrics() if callable(metrics) else {}
        )
        return result

    def _run_uncached_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._search_call_state.embedding_runtime = {}
        response = super().search(payload)
        embedding_runtime = dict(
            getattr(self._search_call_state, "embedding_runtime", {}) or {}
        )
        if embedding_runtime:
            response.setdefault("retrieval", {})["embedding_runtime"] = embedding_runtime
        response.setdefault("retrieval", {})["search_singleflight"] = {
            "coalesced": False,
            "wait_ms": 0.0,
        }
        return response

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = self._search_key(payload)
        wait_started = perf_counter()
        with self._search_state_lock:
            future = self._search_inflight.get(key)
            owner = future is None
            if future is None:
                future = Future()
                self._search_inflight[key] = future

        if not owner:
            response = deepcopy(future.result())
            wait_ms = (perf_counter() - wait_started) * 1000.0
            retrieval = response.setdefault("retrieval", {})
            retrieval["embedding_runtime"] = {
                "cache_hit": False,
                "singleflight_wait": True,
                "provider_called": False,
                "wait_ms": round(wait_ms, 3),
                "provider_ms": 0.0,
            }
            retrieval["search_singleflight"] = {
                "coalesced": True,
                "wait_ms": round(wait_ms, 3),
            }
            return response

        try:
            response = self._run_uncached_search(payload)
        except Exception as exc:
            with self._search_state_lock:
                self._search_inflight.pop(key, None)
                future.set_exception(exc)
            raise
        with self._search_state_lock:
            self._search_inflight.pop(key, None)
            future.set_result(deepcopy(response))
        return response


class _UnavailableQueryEmbedder:
    def __init__(self, error: str) -> None:
        self.error = error

    def embed_query(self, _: str) -> np.ndarray:
        raise RuntimeError(self.error)

    def close(self) -> None:
        return None
