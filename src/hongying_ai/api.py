from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import uuid4

import uvicorn
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from hongying_ai import __version__
from hongying_ai.application.preflight import render_preflight
from hongying_ai.config import Settings, get_settings
from hongying_ai.container import Container
from hongying_ai.contracts.api import (
    ApiResponse,
    CancelRequest,
    MediaProbeRequest,
    ReadinessResult,
    RenderPreflightRequest,
    TimelineValidateRequest,
    TimelineValidateResult,
)
from hongying_ai.domain.errors import ErrorCode, PlatformError
from hongying_ai.domain.models import RenderRun
from hongying_ai.domain.timeline import validate_timeline

REQUESTS = Counter("hongying_api_requests_total", "API 请求数", ["path", "method", "status"])
LATENCY = Histogram("hongying_api_request_duration_seconds", "API 延迟", ["path", "method"])


@dataclass(frozen=True, slots=True)
class RequestContext:
    service_name: str
    tenant_id: int
    trace_id: str
    request_id: str


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or f"req_{uuid4().hex}"


async def internal_context(
    request: Request,
    x_service_name: Annotated[str, Header(alias="X-Service-Name")],
    x_tenant_id: Annotated[int, Header(alias="X-Tenant-Id", gt=0)],
    x_trace_id: Annotated[str, Header(alias="X-Trace-Id", min_length=1, max_length=128)],
) -> RequestContext:
    settings: Settings = request.app.state.container.settings
    if x_service_name not in settings.allowed_services:
        raise PlatformError(ErrorCode.INVALID_COMMAND, "服务身份不在允许列表中")
    return RequestContext(x_service_name, x_tenant_id, x_trace_id, _request_id(request))


Context = Annotated[RequestContext, Depends(internal_context)]


def create_app(
    settings: Settings | None = None,
    container: Container | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    resolved_container = container or Container.build(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_settings.app_work_dir.mkdir(parents=True, exist_ok=True)
        yield
        await resolved_container.close()

    app = FastAPI(
        title="宏映AI Python智能视频平台",
        version=__version__,
        lifespan=lifespan,
        docs_url="/internal/docs",
        redoc_url="/internal/redoc",
        openapi_url="/internal/openapi.json",
        swagger_ui_oauth2_redirect_url="/internal/docs/oauth2-redirect",
    )
    app.state.container = resolved_container

    @app.middleware("http")
    async def observe(request: Request, call_next: Any) -> Any:
        path = request.url.path
        with LATENCY.labels(path=path, method=request.method).time():
            response = await call_next(request)
        REQUESTS.labels(path=path, method=request.method, status=response.status_code).inc()
        response.headers["X-Request-Id"] = _request_id(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(PlatformError)
    async def platform_error(request: Request, exc: PlatformError) -> JSONResponse:
        status = 404 if exc.code == ErrorCode.RUN_NOT_FOUND else 422
        if exc.retryable:
            status = 503
        return JSONResponse(
            status_code=status,
            content={
                "code": exc.code.value,
                "requestId": _request_id(request),
                "message": exc.message,
                "errors": exc.details,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def request_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "path": "/" + "/".join(str(item) for item in error["loc"][1:]),
                "code": error["type"],
                "message": error["msg"],
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "code": ErrorCode.INVALID_COMMAND.value,
                "requestId": _request_id(request),
                "message": "请求字段校验失败",
                "errors": errors,
            },
        )

    @app.get("/internal/health/liveness")
    async def liveness(request: Request) -> dict[str, Any]:
        return {"status": "UP", "version": __version__, "requestId": _request_id(request)}

    @app.get("/internal/health/readiness")
    async def readiness(request: Request) -> JSONResponse:
        values = await asyncio.gather(
            resolved_container.repository.health(),
            resolved_container.coordination.health(),
            resolved_container.store.health(),
            resolved_container.bus.health(),
        )
        dependencies = dict(zip(("mysql", "redis", "minio", "rabbitmq"), values, strict=True))
        result = ReadinessResult(ready=all(values), dependencies=dependencies)
        return JSONResponse(
            status_code=200 if result.ready else 503,
            content=result.model_dump(by_alias=True, mode="json"),
        )

    @app.get("/internal/metrics", response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/internal/v1/media/probe")
    async def media_probe(
        body: MediaProbeRequest,
        context: Context,
    ) -> ApiResponse[dict[str, Any]]:
        try:
            profile = await asyncio.wait_for(
                resolved_container.media.probe_object(
                    tenant_id=context.tenant_id,
                    object_key=body.object_key,
                    expected_sha256=body.sha256,
                    expected_size=body.size_bytes,
                    asset_id=body.asset_id,
                ),
                timeout=3,
            )
        except TimeoutError as exc:
            raise PlatformError(ErrorCode.TIMEOUT, "媒体探测超过 3 秒") from exc
        return ApiResponse(
            requestId=context.request_id,
            data={"profile": profile.model_dump(by_alias=True, mode="json")},
        )

    @app.post("/internal/v1/timelines/validate")
    async def timeline_validate(
        body: TimelineValidateRequest,
        context: Context,
    ) -> ApiResponse[TimelineValidateResult]:
        issues = validate_timeline(body.timeline, body.input_manifest)
        data = TimelineValidateResult(
            valid=not issues,
            errors=tuple(
                {"path": issue.path, "code": issue.code, "message": issue.message}
                for issue in issues
            ),
        )
        return ApiResponse(requestId=context.request_id, data=data)

    @app.post("/internal/v1/renders/preflight")
    async def preflight(
        body: RenderPreflightRequest,
        context: Context,
    ) -> ApiResponse[Any]:
        result = render_preflight(body.timeline, body.input_manifest, body.output_profile)
        return ApiResponse(requestId=context.request_id, data=result)

    @app.get("/internal/v1/runs/{run_id}")
    async def get_run(run_id: str, context: Context) -> ApiResponse[RenderRun]:
        run = await resolved_container.repository.get(run_id, context.tenant_id)
        if not run:
            raise PlatformError(ErrorCode.RUN_NOT_FOUND, "Run 不存在")
        return ApiResponse(requestId=context.request_id, data=run)

    @app.post("/internal/v1/runs/{run_id}:cancel")
    async def cancel_run(
        run_id: str,
        body: CancelRequest,
        context: Context,
    ) -> ApiResponse[dict[str, Any]]:
        run = await resolved_container.repository.get(run_id, context.tenant_id)
        if not run:
            raise PlatformError(ErrorCode.RUN_NOT_FOUND, "Run 不存在")
        created = await resolved_container.coordination.request_cancel(
            run_id, context.tenant_id, body.reason
        )
        return ApiResponse(
            requestId=context.request_id,
            data={
                "runId": run_id,
                "cancelRequested": True,
                "alreadyRequested": not created,
                "requestedBy": body.requested_by,
            },
        )

    return app


app = create_app()


def run() -> None:
    uvicorn.run("hongying_ai.api:app", host="0.0.0.0", port=8080, reload=False)  # noqa: S104
