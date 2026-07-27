from __future__ import annotations

from redis.asyncio import Redis


class RedisCoordinationStore:
    def __init__(self, url: str) -> None:
        self.client = Redis.from_url(url, decode_responses=True)

    async def acquire_lease(self, run_id: str, worker_id: str, ttl_seconds: int) -> bool:
        return bool(await self.client.set(f"ai:lease:{run_id}", worker_id, ex=ttl_seconds, nx=True))

    async def renew_lease(self, run_id: str, worker_id: str, ttl_seconds: int) -> bool:
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
          return redis.call('expire', KEYS[1], ARGV[2])
        end
        return 0
        """
        return bool(
            await self.client.eval(script, 1, f"ai:lease:{run_id}", worker_id, ttl_seconds)
        )

    async def release_lease(self, run_id: str, worker_id: str) -> None:
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
          return redis.call('del', KEYS[1])
        end
        return 0
        """
        await self.client.eval(script, 1, f"ai:lease:{run_id}", worker_id)

    async def request_cancel(self, run_id: str, tenant_id: int, reason: str) -> bool:
        key = f"ai:cancel:{tenant_id}:{run_id}"
        created = await self.client.set(key, reason, ex=86400, nx=True)
        return bool(created)

    async def is_cancelled(self, run_id: str, tenant_id: int) -> bool:
        return bool(await self.client.exists(f"ai:cancel:{tenant_id}:{run_id}"))

    async def claim_command(self, idempotency_key: str, ttl_seconds: int = 604800) -> bool:
        return bool(
            await self.client.set(f"ai:idempotency:{idempotency_key}", "1", ex=ttl_seconds, nx=True)
        )

    async def release_command(self, idempotency_key: str) -> None:
        await self.client.delete(f"ai:idempotency:{idempotency_key}")

    async def health(self) -> bool:
        try:
            return bool(await self.client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        await self.client.aclose()
