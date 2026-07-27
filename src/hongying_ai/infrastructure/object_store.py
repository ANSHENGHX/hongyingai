from __future__ import annotations

import asyncio
import io
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from minio import Minio
from minio.commonconfig import CopySource
from minio.error import S3Error

from hongying_ai.config import Settings
from hongying_ai.domain.errors import ErrorCode, PlatformError


class MinioObjectStore:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.minio_bucket
        self.client = Minio(
            settings.minio_host,
            access_key=settings.minio_access_key or None,
            secret_key=settings.minio_secret_key or None,
            secure=settings.minio_secure,
        )

    async def ensure_bucket(self) -> None:
        exists = await asyncio.to_thread(self.client.bucket_exists, self.bucket)
        if not exists:
            await asyncio.to_thread(self.client.make_bucket, self.bucket)

    async def list(self, prefix: str) -> list[dict[str, Any]]:
        try:
            values = await asyncio.to_thread(
                lambda: list(self.client.list_objects(self.bucket, prefix=prefix, recursive=True))
            )
            return [
                {
                    "objectKey": value.object_name,
                    "size": value.size,
                    "etag": value.etag,
                    "lastModified": (
                        value.last_modified.isoformat() if value.last_modified else None
                    ),
                }
                for value in values
            ]
        except S3Error as exc:
            raise PlatformError(
                ErrorCode.OBJECT_STORE_UNAVAILABLE,
                f"列举对象失败: {prefix}",
                retryable=True,
            ) from exc

    async def presigned_get(self, object_key: str, expires_seconds: int = 3600) -> str:
        try:
            return await asyncio.to_thread(
                self.client.presigned_get_object,
                self.bucket,
                object_key,
                expires=timedelta(seconds=max(60, min(expires_seconds, 86400))),
            )
        except S3Error as exc:
            raise PlatformError(
                ErrorCode.OBJECT_STORE_UNAVAILABLE,
                f"生成对象访问地址失败: {object_key}",
                retryable=True,
            ) from exc

    async def download(self, object_key: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(
                self.client.fget_object,
                self.bucket,
                object_key,
                str(destination),
            )
        except S3Error as exc:
            raise PlatformError(
                ErrorCode.OBJECT_STORE_UNAVAILABLE,
                f"下载对象失败: {object_key}",
                retryable=True,
            ) from exc

    async def upload(
        self, source: Path, object_key: str, content_type: str = "application/octet-stream"
    ) -> str:
        try:
            result = await asyncio.to_thread(
                self.client.fput_object,
                self.bucket,
                object_key,
                str(source),
                content_type,
            )
            return result.etag
        except S3Error as exc:
            raise PlatformError(
                ErrorCode.OBJECT_STORE_UNAVAILABLE,
                f"上传对象失败: {object_key}",
                retryable=True,
            ) from exc

    async def get_json(self, object_key: str) -> dict[str, Any]:
        response = None
        try:
            response = await asyncio.to_thread(self.client.get_object, self.bucket, object_key)
            raw = await asyncio.to_thread(response.read)
            return json.loads(raw)
        except (S3Error, json.JSONDecodeError) as exc:
            raise PlatformError(
                ErrorCode.OBJECT_STORE_UNAVAILABLE,
                f"读取 JSON 对象失败: {object_key}",
                retryable=isinstance(exc, S3Error),
            ) from exc
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    async def put_json(self, value: dict[str, Any], object_key: str) -> str:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        try:
            result = await asyncio.to_thread(
                self.client.put_object,
                self.bucket,
                object_key,
                io.BytesIO(raw),
                len(raw),
                content_type="application/json",
            )
            return result.etag
        except S3Error as exc:
            raise PlatformError(
                ErrorCode.OBJECT_STORE_UNAVAILABLE,
                f"写入 JSON 对象失败: {object_key}",
                retryable=True,
            ) from exc

    async def stat(self, object_key: str) -> dict[str, Any]:
        try:
            value = await asyncio.to_thread(self.client.stat_object, self.bucket, object_key)
            return {
                "etag": value.etag,
                "size": value.size,
                "contentType": value.content_type,
                "lastModified": value.last_modified.isoformat() if value.last_modified else None,
            }
        except S3Error as exc:
            raise PlatformError(
                ErrorCode.OBJECT_STORE_UNAVAILABLE,
                f"对象不存在或不可访问: {object_key}",
                retryable=True,
            ) from exc

    async def promote(self, temporary_key: str, final_key: str) -> str:
        try:
            result = await asyncio.to_thread(
                self.client.copy_object,
                self.bucket,
                final_key,
                CopySource(self.bucket, temporary_key),
            )
            await asyncio.to_thread(self.client.remove_object, self.bucket, temporary_key)
            return result.etag
        except S3Error as exc:
            raise PlatformError(
                ErrorCode.OBJECT_STORE_UNAVAILABLE,
                f"原子发布对象失败: {final_key}",
                retryable=True,
            ) from exc

    async def health(self) -> bool:
        try:
            return await asyncio.to_thread(self.client.bucket_exists, self.bucket)
        except Exception:
            return False
