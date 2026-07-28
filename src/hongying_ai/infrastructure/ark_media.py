from __future__ import annotations

import asyncio
import base64
import mimetypes
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from hongying_ai.config import Settings
from hongying_ai.domain.errors import ErrorCode, PlatformError


class ArkMediaClient:
    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.ark_media_enabled
        self.base_url = settings.ark_base_url.rstrip("/")
        self.api_key = settings.ark_api_key
        self.image_model = settings.ark_image_model
        self.video_model = settings.ark_video_model
        self.timeout_seconds = settings.ark_timeout_seconds
        self.poll_interval = max(1.0, settings.ark_poll_interval_seconds)
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=15),
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

    async def generate_images(
        self,
        prompts: tuple[str, ...],
        output_dir: Path,
        *,
        width: int,
        height: int,
        on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> list[Path]:
        self._require(self.image_model, "图片")
        await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)
        generated: list[Path] = []
        for index, prompt in enumerate(prompts, start=1):
            response = await self.client.post(
                f"{self.base_url}/images/generations",
                json={
                    "model": self.image_model,
                    "prompt": prompt,
                    "size": f"{width}x{height}",
                    "response_format": "url",
                    "watermark": False,
                    "sequential_image_generation": "disabled",
                },
            )
            self._raise_for_status(response, "图片生成")
            body = response.json()
            url = _first_url(body.get("data"))
            if not url:
                raise PlatformError(
                    ErrorCode.MODEL_OUTPUT_INVALID,
                    "方舟图片生成完成但未返回图片地址",
                    retryable=True,
                )
            destination = output_dir / f"scene-{index:02d}.jpg"
            await self._download(url, destination)
            generated.append(destination)
            if on_progress:
                await on_progress(index, len(prompts))
        return generated

    async def generate_videos(
        self,
        prompts: tuple[str, ...],
        output_dir: Path,
        *,
        ratio: str,
        duration_seconds: int,
        reference_images: tuple[Path | None, ...] = (),
        on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> list[Path]:
        self._require(self.video_model, "视频")
        await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)
        generated: list[Path] = []
        for index, prompt in enumerate(prompts, start=1):
            content: list[dict[str, Any]] = [
                {
                    "type": "text",
                    "text": (f"{prompt} --ratio {ratio} --dur {max(4, min(12, duration_seconds))}"),
                }
            ]
            reference = reference_images[index - 1] if index - 1 < len(reference_images) else None
            if reference:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": _data_url(reference)},
                    }
                )
            response = await self.client.post(
                f"{self.base_url}/contents/generations/tasks",
                json={
                    "model": self.video_model,
                    "content": content,
                    "return_last_frame": False,
                },
            )
            self._raise_for_status(response, "视频任务提交")
            task_id = str(response.json().get("id") or "")
            if not task_id:
                raise PlatformError(
                    ErrorCode.MODEL_OUTPUT_INVALID,
                    "方舟视频任务未返回任务 ID",
                    retryable=True,
                )
            video_url = await self._wait_video(task_id)
            destination = output_dir / f"scene-{index:02d}.mp4"
            await self._download(video_url, destination)
            generated.append(destination)
            if on_progress:
                await on_progress(index, len(prompts))
        return generated

    async def _wait_video(self, task_id: str) -> str:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            response = await self.client.get(f"{self.base_url}/contents/generations/tasks/{task_id}")
            self._raise_for_status(response, "视频任务查询")
            body = response.json()
            status = str(body.get("status") or "").lower()
            if status == "succeeded":
                url = str((body.get("content") or {}).get("video_url") or "")
                if url:
                    return url
                raise PlatformError(
                    ErrorCode.MODEL_OUTPUT_INVALID,
                    "方舟视频任务成功但未返回视频地址",
                    retryable=True,
                )
            if status in {"failed", "cancelled"}:
                message = str(body.get("error") or body.get("message") or status)
                raise PlatformError(
                    ErrorCode.MODEL_UNAVAILABLE,
                    f"方舟视频生成失败: {message[:500]}",
                    retryable=True,
                )
            await asyncio.sleep(self.poll_interval)
        raise PlatformError(ErrorCode.TIMEOUT, "方舟视频生成超时", retryable=True)

    async def _download(self, url: str, destination: Path) -> None:
        async with self.client.stream("GET", url) as response:
            self._raise_for_status(response, "生成结果下载")
            with destination.open("wb") as stream:
                async for chunk in response.aiter_bytes():
                    stream.write(chunk)
        size = await asyncio.to_thread(lambda: destination.stat().st_size if destination.is_file() else 0)
        if size == 0:
            raise PlatformError(
                ErrorCode.MODEL_OUTPUT_INVALID,
                "生成结果文件为空",
                retryable=True,
            )

    def _require(self, model: str, capability: str) -> None:
        if not self.enabled or not self.api_key or not model:
            raise PlatformError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"未配置方舟{capability}生成模型",
            )

    @staticmethod
    def _raise_for_status(response: httpx.Response, action: str) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text[-800:]
            raise PlatformError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"方舟{action}失败: HTTP {response.status_code} {detail}",
                retryable=response.status_code >= 429,
            ) from exc

    async def close(self) -> None:
        await self.client.aclose()


def _first_url(data: Any) -> str:
    if not isinstance(data, list):
        return ""
    for item in data:
        if isinstance(item, dict) and item.get("url"):
            return str(item["url"])
    return ""


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"
