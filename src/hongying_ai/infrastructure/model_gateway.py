from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import httpx

from hongying_ai.config import Settings
from hongying_ai.domain.errors import ErrorCode, PlatformError


class DeepSeekModelClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.deepseek_base_url.rstrip("/")
        self.api_key = settings.deepseek_api_key
        self.model = settings.deepseek_model
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10))

    async def structured_output(
        self,
        *,
        prompt_id: str,
        prompt_version: str,
        system_prompt: str,
        user_data: dict[str, Any],
        json_schema: dict[str, Any],
        max_tokens: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.api_key:
            raise PlatformError(ErrorCode.MODEL_UNAVAILABLE, "未配置模型密钥")
        schema_text = json.dumps(json_schema, ensure_ascii=False, separators=(",", ":"))
        data_text = json.dumps(user_data, ensure_ascii=False, separators=(",", ":"))
        prompt_hash = hashlib.sha256(
            f"{prompt_id}:{prompt_version}:{system_prompt}".encode()
        ).hexdigest()
        started = time.perf_counter()
        try:
            response = await self.client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "response_format": {"type": "json_object"},
                    "temperature": 0.3,
                    "max_tokens": max_tokens,
                    "messages": [
                        {
                            "role": "system",
                            "content": f"{system_prompt}\n必须符合以下 JSON Schema：{schema_text}",
                        },
                        {"role": "user", "content": f"<untrusted_data>{data_text}</untrusted_data>"},
                    ],
                },
            )
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            value = json.loads(content)
            usage = body.get("usage", {})
            return value, {
                "provider": "deepseek",
                "model": self.model,
                "promptId": prompt_id,
                "promptVersion": prompt_version,
                "promptHash": prompt_hash,
                "latencyMs": round((time.perf_counter() - started) * 1000),
                "inputTokens": usage.get("prompt_tokens"),
                "outputTokens": usage.get("completion_tokens"),
                "fallback": False,
            }
        except (httpx.HTTPError, KeyError, json.JSONDecodeError) as exc:
            raise PlatformError(
                ErrorCode.MODEL_OUTPUT_INVALID
                if isinstance(exc, json.JSONDecodeError)
                else ErrorCode.MODEL_UNAVAILABLE,
                "模型调用或结构化输出失败",
                retryable=True,
            ) from exc

    async def close(self) -> None:
        await self.client.aclose()

