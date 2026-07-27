from __future__ import annotations

import asyncio
import mimetypes
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
from typing import Annotated, Any
from uuid import uuid4

import uvicorn
from fastapi import Depends, FastAPI, File, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from hongying_ai import __version__
from hongying_ai.application.media import sha256_file
from hongying_ai.application.preflight import render_preflight
from hongying_ai.application.render import RenderService
from hongying_ai.application.studio import StudioWorkflowService, asset_from_analysis
from hongying_ai.application.templates import TEMPLATES
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
from hongying_ai.contracts.studio import (
    StudioAssetResult,
    StudioGenerateRequest,
    StudioGenerateResult,
)
from hongying_ai.domain.errors import ErrorCode, PlatformError
from hongying_ai.domain.models import AnalysisProfile, RenderRun
from hongying_ai.domain.timeline import assert_safe_object_key, validate_timeline

REQUESTS = Counter("hongying_api_requests_total", "API 请求数", ["path", "method", "status"])
LATENCY = Histogram("hongying_api_request_duration_seconds", "API 延迟", ["path", "method"])


@dataclass(frozen=True, slots=True)
class RequestContext:
    service_name: str
    tenant_id: int
    trace_id: str
    request_id: str


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    if value:
        return str(value)
    value = request.headers.get("x-request-id") or f"req_{uuid4().hex}"
    request.state.request_id = value
    return value


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
    render_service = RenderService(
        settings=resolved_settings,
        store=resolved_container.store,
        coordination=resolved_container.coordination,
        repository=resolved_container.repository,
        runner=resolved_container.runner,
        quality=resolved_container.quality,
        bus=resolved_container.bus,
    )
    studio_service = StudioWorkflowService(
        resolved_settings,
        resolved_container.planner,
        render_service,
        resolved_container.repository,
        resolved_container.store,
        resolved_container.bus,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_settings.app_work_dir.mkdir(parents=True, exist_ok=True)
        ensure_bucket = getattr(resolved_container.store, "ensure_bucket", None)
        if ensure_bucket:
            await ensure_bucket()
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
    app.state.studio = studio_service
    web_dir = Path(__file__).with_name("web")
    app.mount("/studio/static", StaticFiles(directory=web_dir), name="studio-static")

    @app.middleware("http")
    async def observe(request: Request, call_next: Any) -> Any:
        _request_id(request)
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

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "code": ErrorCode.INTERNAL_ERROR.value,
                "requestId": _request_id(request),
                "message": "服务内部错误",
                "errors": [{"code": type(exc).__name__, "message": "请求执行失败"}],
            },
        )

    @app.get("/internal/health/liveness")
    async def liveness(request: Request) -> dict[str, Any]:
        return {"status": "UP", "version": __version__, "requestId": _request_id(request)}

    @app.get("/internal/health/readiness")
    async def readiness(request: Request) -> JSONResponse:
        try:
            values = await asyncio.wait_for(
                asyncio.gather(
                    resolved_container.repository.health(),
                    resolved_container.coordination.health(),
                    resolved_container.store.health(),
                    resolved_container.bus.health(),
                ),
                timeout=3,
            )
        except TimeoutError:
            values = [False, False, False, False]
        dependencies = dict(zip(("mysql", "redis", "minio", "rabbitmq"), values, strict=True))
        result = ReadinessResult(ready=all(values), dependencies=dependencies)
        return JSONResponse(
            status_code=200 if result.ready else 503,
            content=result.model_dump(by_alias=True, mode="json"),
        )

    @app.get("/internal/metrics", response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/studio")

    @app.get("/studio", include_in_schema=False)
    async def studio_page() -> FileResponse:
        return FileResponse(web_dir / "index.html")

    @app.get("/internal/v1/studio/templates")
    async def studio_templates(context: Context) -> ApiResponse[dict[str, Any]]:
        return ApiResponse(
            requestId=context.request_id,
            data={"templates": [template.to_dict() for template in TEMPLATES]},
        )

    @app.post("/internal/v1/studio/assets/upload")
    async def studio_upload_asset(
        context: Context,
        file: Annotated[UploadFile, File()],
    ) -> ApiResponse[StudioAssetResult]:
        suffix = Path(file.filename or "material.bin").suffix.lower()[:12]
        allowed = {
            ".mp4",
            ".mov",
            ".mkv",
            ".webm",
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".mp3",
            ".wav",
            ".m4a",
            ".aac",
        }
        if suffix not in allowed:
            raise PlatformError(ErrorCode.MEDIA_UNSUPPORTED, f"不支持的素材格式: {suffix}")
        asset_id = f"asset_{uuid4().hex}"
        work_dir = Path(
            mkdtemp(prefix=f"upload-{context.tenant_id}-", dir=resolved_settings.app_work_dir)
        )
        try:
            local = work_dir / f"original{suffix}"
            size = 0
            with local.open("wb") as stream:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > resolved_settings.studio_max_upload_bytes:
                        raise PlatformError(ErrorCode.RESOURCE_EXHAUSTED, "上传素材超过大小限制")
                    stream.write(chunk)
            if size == 0:
                raise PlatformError(ErrorCode.INVALID_COMMAND, "上传素材为空")
            sha256 = sha256_file(local)
            object_key = (
                f"{resolved_settings.environment_object_prefix}/{context.tenant_id}/material/"
                f"{asset_id}/v1/original{suffix}"
            )
            await resolved_container.store.upload(
                local,
                object_key,
                file.content_type or mimetypes.guess_type(local.name)[0] or "application/octet-stream",
            )
            analysis = await resolved_container.media.analyze(
                tenant_id=context.tenant_id,
                asset_id=asset_id,
                object_key=object_key,
                expected_sha256=sha256,
                expected_size=size,
                profile=AnalysisProfile.STANDARD,
            )
            asset = asset_from_analysis(
                tenant_id=context.tenant_id,
                asset_id=asset_id,
                object_key=object_key,
                sha256=sha256,
                size_bytes=size,
                analysis=analysis,
            )
            analysis_key = (
                f"{resolved_settings.environment_object_prefix}/{context.tenant_id}/studio/"
                f"assets/{asset_id}.json"
            )
            catalog = {
                "schemaVersion": "1.0",
                "asset": asset.model_dump(by_alias=True, mode="json"),
                "fileName": file.filename or local.name,
                "analysisObjectKey": analysis_key,
                "analysis": analysis,
            }
            await resolved_container.store.put_json(catalog, analysis_key)
            thumbnail_url = (
                await resolved_container.store.presigned_get(analysis["thumbnailObjectKey"])
                if analysis.get("thumbnailObjectKey")
                else None
            )
            return ApiResponse(
                requestId=context.request_id,
                data=StudioAssetResult(
                    asset=asset,
                    fileName=catalog["fileName"],
                    thumbnailUrl=thumbnail_url,
                    analysisObjectKey=analysis_key,
                    analysis=analysis,
                ),
            )
        finally:
            await file.close()
            shutil.rmtree(work_dir, ignore_errors=True)

    @app.get("/internal/v1/studio/assets")
    async def studio_assets(context: Context) -> ApiResponse[dict[str, Any]]:
        prefix = (
            f"{resolved_settings.environment_object_prefix}/{context.tenant_id}/studio/assets/"
        )
        objects = await resolved_container.store.list(prefix)
        assets = []
        for item in objects:
            if not str(item["objectKey"]).endswith(".json"):
                continue
            catalog = await resolved_container.store.get_json(str(item["objectKey"]))
            thumbnail_key = catalog.get("analysis", {}).get("thumbnailObjectKey")
            catalog["thumbnailUrl"] = (
                await resolved_container.store.presigned_get(thumbnail_key)
                if thumbnail_key
                else None
            )
            assets.append(catalog)
        return ApiResponse(requestId=context.request_id, data={"assets": assets})

    @app.get("/internal/v1/studio/objects")
    async def studio_object(object_key: str, context: Context) -> RedirectResponse:
        assert_safe_object_key(
            object_key,
            context.tenant_id,
            resolved_settings.environment_object_prefix,
        )
        return RedirectResponse(await resolved_container.store.presigned_get(object_key))

    @app.post("/internal/v1/studio/generations")
    async def studio_generate(
        body: StudioGenerateRequest,
        context: Context,
    ) -> ApiResponse[StudioGenerateResult]:
        run = await studio_service.start(
            body,
            tenant_id=context.tenant_id,
            trace_id=context.trace_id,
        )
        return ApiResponse(
            requestId=context.request_id,
            data=StudioGenerateResult(
                taskId=run.task_id,
                runId=run.run_id,
                stage=run.stage.value,
                statusUrl=f"/internal/v1/runs/{run.run_id}",
            ),
        )

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
