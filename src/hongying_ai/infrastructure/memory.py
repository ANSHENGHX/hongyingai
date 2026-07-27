from __future__ import annotations


class MemoryCoordinationStore:
    def __init__(self) -> None:
        self.leases: dict[str, str] = {}
        self.cancellations: set[tuple[int, str]] = set()
        self.commands: set[str] = set()

    async def acquire_lease(self, run_id: str, worker_id: str, ttl_seconds: int) -> bool:
        if run_id in self.leases:
            return False
        self.leases[run_id] = worker_id
        return True

    async def renew_lease(self, run_id: str, worker_id: str, ttl_seconds: int) -> bool:
        return self.leases.get(run_id) == worker_id

    async def release_lease(self, run_id: str, worker_id: str) -> None:
        if self.leases.get(run_id) == worker_id:
            self.leases.pop(run_id)

    async def request_cancel(self, run_id: str, tenant_id: int, reason: str) -> bool:
        key = (tenant_id, run_id)
        created = key not in self.cancellations
        self.cancellations.add(key)
        return created

    async def is_cancelled(self, run_id: str, tenant_id: int) -> bool:
        return (tenant_id, run_id) in self.cancellations

    async def claim_command(self, idempotency_key: str, ttl_seconds: int = 604800) -> bool:
        if idempotency_key in self.commands:
            return False
        self.commands.add(idempotency_key)
        return True

    async def release_command(self, idempotency_key: str) -> None:
        self.commands.discard(idempotency_key)

    async def health(self) -> bool:
        return True
