from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from hongying_ai.config import Settings
from hongying_ai.domain.errors import ErrorCode, PlatformError


class BaiduTtsClient:
    def __init__(self, settings: Settings) -> None:
        self.api_key = settings.baidu_tts_api_key
        self.url = settings.baidu_tts_short_url
        self.voice = settings.baidu_tts_voice
        self.speed = settings.baidu_tts_speed
        self.pitch = settings.baidu_tts_pitch
        self.volume = settings.baidu_tts_volume
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10))

    async def synthesize(
        self,
        text: str,
        output: Path,
        *,
        cuid: str,
        voice: int | None = None,
        speed: int | None = None,
        pitch: int | None = None,
        volume: int | None = None,
    ) -> None:
        if not self.api_key:
            raise PlatformError(ErrorCode.MODEL_UNAVAILABLE, "未配置百度语音合成 API Key")
        normalized = " ".join(text.split())[:480]
        if not normalized:
            raise PlatformError(ErrorCode.INVALID_COMMAND, "配音文本为空")
        response = await self.client.post(
            self.url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "tex": normalized,
                "cuid": cuid,
                "ctp": "1",
                "lan": "zh",
                "per": str(voice if voice is not None else self.voice),
                "spd": str(speed if speed is not None else self.speed),
                "pit": str(pitch if pitch is not None else self.pitch),
                "vol": str(volume if volume is not None else self.volume),
                "aue": "3",
            },
        )
        content_type = response.headers.get("content-type", "")
        if response.status_code >= 400 or "audio" not in content_type:
            detail = ""
            try:
                body = response.json()
                detail = str(body.get("err_detail") or body.get("err_msg") or body.get("message") or "")
            except ValueError:
                detail = response.text[:200]
            suffix = f": {detail[:200]}" if detail else ""
            raise PlatformError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"百度语音合成失败{suffix}",
                retryable=True,
            )
        if not response.content:
            raise PlatformError(
                ErrorCode.MODEL_OUTPUT_INVALID,
                "百度语音合成返回了空音频",
                retryable=True,
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(output.write_bytes, response.content)

    async def close(self) -> None:
        await self.client.aclose()
