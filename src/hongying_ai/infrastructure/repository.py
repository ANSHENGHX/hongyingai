from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from hongying_ai.domain.models import RenderRun, RunStage


class MySqlRunRepository:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=5,
            max_overflow=10,
        )

    async def get(self, run_id: str, tenant_id: int | None = None) -> RenderRun | None:
        query = "SELECT * FROM ai_render_run WHERE run_id = :run_id"
        params: dict[str, object] = {"run_id": run_id}
        if tenant_id is not None:
            query += " AND tenant_id = :tenant_id"
            params["tenant_id"] = tenant_id
        async with self.engine.connect() as connection:
            row = (await connection.execute(text(query), params)).mappings().first()
        return _to_run(row) if row else None

    async def list_recent(self, tenant_id: int, limit: int = 20) -> list[RenderRun]:
        statement = text(
            """
            SELECT *
            FROM ai_render_run
            WHERE tenant_id = :tenant_id
            ORDER BY updated_at DESC, created_at DESC
            LIMIT :limit
            """
        )
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    statement,
                    {"tenant_id": tenant_id, "limit": max(1, min(limit, 100))},
                )
            ).mappings()
            return [_to_run(row) for row in rows]

    async def upsert(self, run: RenderRun) -> None:
        query = text(
            """
            INSERT INTO ai_render_run (
              run_id, task_id, tenant_id, run_no, stage, progress, sequence_no,
              worker_id, lease_until, attempt, output_object_key, error_code,
              error_summary, metadata_json, created_at, updated_at
            ) VALUES (
              :run_id, :task_id, :tenant_id, :run_no, :stage, :progress, :sequence_no,
              :worker_id, :lease_until, :attempt, :output_object_key, :error_code,
              :error_summary, :metadata_json, :created_at, :updated_at
            ) AS incoming
            ON DUPLICATE KEY UPDATE
              stage = IF(
                incoming.sequence_no >= ai_render_run.sequence_no,
                incoming.stage,
                ai_render_run.stage
              ),
              progress = IF(
                incoming.sequence_no >= ai_render_run.sequence_no,
                incoming.progress,
                ai_render_run.progress
              ),
              sequence_no = GREATEST(ai_render_run.sequence_no, incoming.sequence_no),
              worker_id = IF(
                incoming.sequence_no >= ai_render_run.sequence_no,
                incoming.worker_id,
                ai_render_run.worker_id
              ),
              lease_until = IF(
                incoming.sequence_no >= ai_render_run.sequence_no,
                incoming.lease_until,
                ai_render_run.lease_until
              ),
              attempt = IF(
                incoming.sequence_no >= ai_render_run.sequence_no,
                incoming.attempt,
                ai_render_run.attempt
              ),
              output_object_key = IF(
                incoming.sequence_no >= ai_render_run.sequence_no,
                incoming.output_object_key,
                ai_render_run.output_object_key
              ),
              error_code = IF(
                incoming.sequence_no >= ai_render_run.sequence_no,
                incoming.error_code,
                ai_render_run.error_code
              ),
              error_summary = IF(
                incoming.sequence_no >= ai_render_run.sequence_no,
                incoming.error_summary,
                ai_render_run.error_summary
              ),
              metadata_json = IF(
                incoming.sequence_no >= ai_render_run.sequence_no,
                incoming.metadata_json,
                ai_render_run.metadata_json
              ),
              updated_at = IF(
                incoming.sequence_no >= ai_render_run.sequence_no,
                incoming.updated_at,
                ai_render_run.updated_at
              )
            """
        )
        value = run.model_dump()
        value.update(
            {
                "stage": run.stage.value,
                "sequence_no": run.sequence,
                "metadata_json": json.dumps(run.metadata, ensure_ascii=False),
                "created_at": run.created_at.replace(tzinfo=None),
                "updated_at": run.updated_at.replace(tzinfo=None),
                "lease_until": run.lease_until.replace(tzinfo=None) if run.lease_until else None,
            }
        )
        async with self.engine.begin() as connection:
            await connection.execute(query, value)

    async def health(self) -> bool:
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def record_model_call(
        self,
        tenant_id: int,
        task_id: int,
        trace_id: str,
        value: dict[str, object],
    ) -> None:
        query = text(
            """
            INSERT INTO ai_model_call_record (
              tenant_id, task_id, trace_id, provider, model, prompt_id, prompt_version,
              prompt_hash, input_tokens, output_tokens, latency_ms, cost_usd,
              safety_status, fallback_used, schema_failure
            ) VALUES (
              :tenant_id, :task_id, :trace_id, :provider, :model, :prompt_id, :prompt_version,
              :prompt_hash, :input_tokens, :output_tokens, :latency_ms, :cost_usd,
              :safety_status, :fallback_used, :schema_failure
            )
            """
        )
        params = {
            "tenant_id": tenant_id,
            "task_id": task_id,
            "trace_id": trace_id,
            "provider": value.get("provider", "fallback"),
            "model": value.get("model", "rules-v1"),
            "prompt_id": value.get("promptId", "video-plan"),
            "prompt_version": value.get("promptVersion", "1.0.0"),
            "prompt_hash": value.get("promptHash", "0" * 64),
            "input_tokens": value.get("inputTokens"),
            "output_tokens": value.get("outputTokens"),
            "latency_ms": value.get("latencyMs", 0),
            "cost_usd": value.get("costUsd"),
            "safety_status": value.get("safety", "UNKNOWN"),
            "fallback_used": bool(value.get("fallback", False)),
            "schema_failure": bool(value.get("schemaFailure", False)),
        }
        async with self.engine.begin() as connection:
            await connection.execute(query, params)

    async def record_cost(
        self,
        tenant_id: int,
        task_id: int,
        run_id: str,
        value: dict[str, object],
    ) -> None:
        query = text(
            """
            INSERT INTO ai_cost_record (
              tenant_id, task_id, run_id, model_tokens, gpu_seconds, cpu_seconds,
              input_media_seconds, output_media_seconds, storage_bytes, transfer_bytes,
              estimated_cost_usd
            ) VALUES (
              :tenant_id, :task_id, :run_id, :model_tokens, :gpu_seconds, :cpu_seconds,
              :input_media_seconds, :output_media_seconds, :storage_bytes, :transfer_bytes,
              :estimated_cost_usd
            )
            """
        )
        params = {
            "tenant_id": tenant_id,
            "task_id": task_id,
            "run_id": run_id,
            "model_tokens": value.get("modelTokens", 0),
            "gpu_seconds": value.get("gpuSeconds", 0),
            "cpu_seconds": value.get("cpuSeconds", 0),
            "input_media_seconds": value.get("inputMediaSeconds", 0),
            "output_media_seconds": value.get("outputMediaSeconds", 0),
            "storage_bytes": value.get("storageBytes", 0),
            "transfer_bytes": value.get("transferBytes", 0),
            "estimated_cost_usd": value.get("estimatedCostUsd", 0),
        }
        async with self.engine.begin() as connection:
            await connection.execute(query, params)

    async def search_knowledge(
        self, tenant_id: int, query: str, limit: int = 8
    ) -> list[dict[str, object]]:
        statement = text(
            """
            SELECT source_id, source_type, title, content, metadata_json,
                   MATCH(title, content) AGAINST (:query IN NATURAL LANGUAGE MODE) AS score
            FROM ai_brand_knowledge
            WHERE tenant_id = :tenant_id
              AND enabled = TRUE
              AND MATCH(title, content) AGAINST (:query IN NATURAL LANGUAGE MODE)
            ORDER BY score DESC, updated_at DESC
            LIMIT :limit
            """
        )
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    statement,
                    {"tenant_id": tenant_id, "query": query[:500], "limit": min(limit, 20)},
                )
            ).mappings()
            return [
                {
                    "sourceId": row["source_id"],
                    "sourceType": row["source_type"],
                    "title": row["title"],
                    "content": row["content"],
                    "metadata": (
                        json.loads(row["metadata_json"])
                        if isinstance(row["metadata_json"], str)
                        else row["metadata_json"]
                    ),
                    "score": float(row["score"]),
                }
                for row in rows
            ]

    async def close(self) -> None:
        await self.engine.dispose()


def _to_run(row: dict[str, object]) -> RenderRun:
    def utc(value: object) -> datetime | None:
        if not isinstance(value, datetime):
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    metadata = row.get("metadata_json")
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return RenderRun(
        runId=row["run_id"],
        taskId=row["task_id"],
        tenantId=row["tenant_id"],
        runNo=row["run_no"],
        stage=RunStage(str(row["stage"])),
        progress=float(row["progress"]),
        sequence=int(row["sequence_no"]),
        workerId=row.get("worker_id"),
        leaseUntil=utc(row.get("lease_until")),
        attempt=int(row["attempt"]),
        outputObjectKey=row.get("output_object_key"),
        errorCode=row.get("error_code"),
        errorSummary=row.get("error_summary"),
        metadata=metadata or {},
        createdAt=utc(row.get("created_at")) or datetime.now(UTC),
        updatedAt=utc(row.get("updated_at")) or datetime.now(UTC),
    )


class MemoryRunRepository:
    def __init__(self) -> None:
        self.values: dict[str, RenderRun] = {}

    async def get(self, run_id: str, tenant_id: int | None = None) -> RenderRun | None:
        value = self.values.get(run_id)
        if value and tenant_id is not None and value.tenant_id != tenant_id:
            return None
        return value

    async def list_recent(self, tenant_id: int, limit: int = 20) -> list[RenderRun]:
        values = [run for run in self.values.values() if run.tenant_id == tenant_id]
        values.sort(key=lambda run: run.updated_at, reverse=True)
        return values[: max(1, min(limit, 100))]

    async def upsert(self, run: RenderRun) -> None:
        current = self.values.get(run.run_id)
        if not current or run.sequence >= current.sequence:
            self.values[run.run_id] = run

    async def health(self) -> bool:
        return True

    async def record_model_call(
        self,
        tenant_id: int,
        task_id: int,
        trace_id: str,
        value: dict[str, object],
    ) -> None:
        return None

    async def record_cost(
        self,
        tenant_id: int,
        task_id: int,
        run_id: str,
        value: dict[str, object],
    ) -> None:
        return None

    async def search_knowledge(
        self, tenant_id: int, query: str, limit: int = 8
    ) -> list[dict[str, object]]:
        return []
