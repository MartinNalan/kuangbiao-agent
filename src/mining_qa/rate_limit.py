import time
from dataclasses import dataclass

from fastapi import HTTPException, status

from .auth import key_fingerprint
from .config import Settings

try:
    import redis.asyncio as redis_async
except ImportError:  # pragma: no cover
    redis_async = None


@dataclass
class RateLimitResult:
    allowed: bool
    count: int
    limit: int
    backend: str
    retry_after_seconds: int = 0


class RateLimiter:
    def __init__(self) -> None:
        self._memory_counts: dict[str, tuple[int, float]] = {}
        self._redis_clients: dict[str, object] = {}
        self.last_backend = "memory"

    async def check(self, api_key: str, settings: Settings) -> RateLimitResult:
        if not settings.rate_limit_enabled:
            return RateLimitResult(True, 0, settings.rate_limit_per_minute, "disabled")

        limit = settings.rate_limit_per_minute
        if limit <= 0:
            return RateLimitResult(True, 0, limit, "disabled")

        key_hash = key_fingerprint(api_key) or "anonymous"
        redis_result = await self._check_redis(key_hash, settings)
        if redis_result is not None:
            self.last_backend = redis_result.backend
            return redis_result

        memory_result = self._check_memory(key_hash, limit)
        self.last_backend = memory_result.backend
        return memory_result

    async def _check_redis(self, key_hash: str, settings: Settings) -> RateLimitResult | None:
        if redis_async is None or not settings.redis_url:
            return None

        try:
            client = self._redis_clients.get(settings.redis_url)
            if client is None:
                client = redis_async.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
                self._redis_clients[settings.redis_url] = client

            bucket = int(time.time() // 60)
            redis_key = f"rate_limit:{key_hash}:{bucket}"
            count = await client.incr(redis_key)
            if count == 1:
                await client.expire(redis_key, 70)
            return RateLimitResult(
                allowed=count <= settings.rate_limit_per_minute,
                count=count,
                limit=settings.rate_limit_per_minute,
                backend="redis",
                retry_after_seconds=self._seconds_until_next_minute(),
            )
        except Exception:
            return None

    def _check_memory(self, key_hash: str, limit: int) -> RateLimitResult:
        now = time.time()
        window_start = now - (now % 60)
        count, current_window = self._memory_counts.get(key_hash, (0, window_start))
        if current_window != window_start:
            count = 0
            current_window = window_start

        count += 1
        self._memory_counts[key_hash] = (count, current_window)
        return RateLimitResult(
            allowed=count <= limit,
            count=count,
            limit=limit,
            backend="memory",
            retry_after_seconds=self._seconds_until_next_minute(),
        )

    def raise_if_limited(self, result: RateLimitResult) -> None:
        if result.allowed:
            return
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "RATE_LIMITED",
                "message": "API rate limit exceeded.",
                "limit_per_minute": result.limit,
                "current_count": result.count,
                "backend": result.backend,
                "retry_after_seconds": result.retry_after_seconds,
            },
            headers={"Retry-After": str(result.retry_after_seconds)},
        )

    def _seconds_until_next_minute(self) -> int:
        return max(1, 60 - int(time.time() % 60))
