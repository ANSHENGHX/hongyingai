from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from hongying_ai.domain.errors import ErrorCode, PlatformError

PROGRESS_PATTERN = re.compile(r"out_time_ms=(\d+)")
BLACK_PATTERN = re.compile(r"black_duration:(?P<duration>[0-9.]+)")
SILENCE_PATTERN = re.compile(r"silence_duration: (?P<duration>[0-9.]+)")
FREEZE_PATTERN = re.compile(r"freeze_duration: (?P<duration>[0-9.]+)")
SCENE_PATTERN = re.compile(r"pts_time:(?P<seconds>[0-9.]+)")


class FfmpegRunner:
    def __init__(self, ffmpeg_path: str = "ffmpeg", ffprobe_path: str = "ffprobe") -> None:
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path

    async def probe(self, path: Path) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            self.ffprobe_path,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise PlatformError(
                ErrorCode.MEDIA_UNSUPPORTED,
                f"ffprobe 失败: {stderr.decode('utf-8', errors='replace')[-1000:]}",
            )
        return json.loads(stdout)

    async def scan_quality(self, path: Path) -> dict[str, Any]:
        probe = await self.probe(path)
        has_audio = any(
            stream.get("codec_type") == "audio" for stream in probe.get("streams", [])
        )
        filter_graph = "[0:v]blackdetect=d=0.5:pix_th=0.10,freezedetect=n=-60dB:d=2[v]"
        maps = ["-map", "[v]"]
        if has_audio:
            filter_graph += ";[0:a]silencedetect=n=-50dB:d=1[a]"
            maps.extend(["-map", "[a]"])
        args = [
            self.ffmpeg_path,
            "-nostdin",
            "-hide_banner",
            "-v",
            "info",
            "-i",
            str(path),
            "-filter_complex",
            filter_graph,
            *maps,
            "-f",
            "null",
            "-",
        ]
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        text = stderr.decode("utf-8", errors="replace")
        return {
            "returnCode": process.returncode,
            "blackSeconds": sum(float(m.group("duration")) for m in BLACK_PATTERN.finditer(text)),
            "silenceSeconds": sum(float(m.group("duration")) for m in SILENCE_PATTERN.finditer(text)),
            "freezeSeconds": sum(float(m.group("duration")) for m in FREEZE_PATTERN.finditer(text)),
        }

    async def detect_scenes(self, source: Path, duration_ms: int) -> list[int]:
        process = await asyncio.create_subprocess_exec(
            self.ffmpeg_path,
            "-nostdin",
            "-hide_banner",
            "-v",
            "info",
            "-i",
            str(source),
            "-vf",
            "select='gt(scene,0.32)',showinfo",
            "-an",
            "-f",
            "null",
            "-",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            return [0, duration_ms]
        boundaries = {0, duration_ms}
        for match in SCENE_PATTERN.finditer(stderr.decode("utf-8", errors="replace")):
            milliseconds = round(float(match.group("seconds")) * 1000)
            if 300 <= milliseconds <= duration_ms - 300:
                boundaries.add(milliseconds)
        return sorted(boundaries)

    async def create_thumbnail(
        self, source: Path, destination: Path, at_seconds: float
    ) -> None:
        await self._run_simple(
            [
                self.ffmpeg_path,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{max(0, at_seconds):.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale='min(640,iw)':-2",
                "-q:v",
                "3",
                str(destination),
            ],
            ErrorCode.MEDIA_UNSUPPORTED,
            "生成缩略图失败",
        )

    async def create_proxy(self, source: Path, destination: Path) -> None:
        await self._run_simple(
            [
                self.ffmpeg_path,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vf",
                "scale=-2:'min(480,ih)',fps=30,format=yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "28",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                str(destination),
            ],
            ErrorCode.MEDIA_UNSUPPORTED,
            "生成分析代理失败",
        )

    async def _run_simple(
        self, args: list[str], code: ErrorCode, message: str
    ) -> None:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace")[-2000:]
            raise PlatformError(code, f"{message}: {detail}")

    async def render(
        self,
        args: list[str],
        *,
        timeout_seconds: int,
        on_progress: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        process = await asyncio.create_subprocess_exec(
            self.ffmpeg_path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        async def consume_progress() -> None:
            if not process.stdout:
                return
            last = 0
            while line := await process.stdout.readline():
                match = PROGRESS_PATTERN.search(line.decode("utf-8", errors="ignore"))
                if match and on_progress:
                    current = int(match.group(1))
                    if current > last:
                        last = current
                        await on_progress(float(current))

        async def read_stderr() -> bytes:
            if not process.stderr:
                return b""
            return await process.stderr.read()

        progress_task = asyncio.create_task(consume_progress())
        stderr_task = asyncio.create_task(read_stderr())
        wait_task = asyncio.create_task(process.wait())
        try:
            done, _ = await asyncio.wait(
                {progress_task, wait_task},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_EXCEPTION,
            )
            if not done:
                raise TimeoutError
            if progress_task.done() and progress_task.exception():
                raise progress_task.exception()  # type: ignore[misc]
            await asyncio.wait_for(wait_task, timeout=timeout_seconds)
            await progress_task
            stderr = await stderr_task
        except TimeoutError as exc:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
            raise PlatformError(ErrorCode.TIMEOUT, "FFmpeg 渲染超时") from exc
        except asyncio.CancelledError:
            process.terminate()
            await process.wait()
            raise
        except Exception:
            process.terminate()
            await process.wait()
            raise
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace")[-4000:]
            raise PlatformError(ErrorCode.RENDER_FAILED, f"FFmpeg 执行失败: {detail}", retryable=True)
