from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from hongying_ai.domain.models import InputManifest, Timeline, TrackType


@dataclass(frozen=True, slots=True)
class CompiledRender:
    args: tuple[str, ...]
    filter_graph: str
    input_asset_ids: tuple[str, ...]
    output_path: Path


def _even(value: int) -> int:
    return value if value % 2 == 0 else value - 1


def _ass_time(milliseconds: int) -> str:
    total_centiseconds = milliseconds // 10
    hours, remainder = divmod(total_centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6000)
    seconds, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def write_ass(timeline: Timeline, path: Path) -> None:
    width = timeline.canvas.width
    height = timeline.canvas.height
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
        "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,"
        "BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        "Style: Default,Noto Sans CJK SC,48,&H00FFFFFF,&H0000FFFF,&H00000000,"
        "&H80000000,-1,0,0,0,100,100,0,0,1,2,0,2,40,40,120,1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for cue in timeline.subtitles:
        color = cue.style.color.lstrip("#")
        bgr = f"{color[4:6]}{color[2:4]}{color[0:2]}"
        position_y = round(height * cue.style.position_y)
        override = (
            f"{{\\fn{_ass_escape(cue.style.font)}\\fs{cue.style.font_size}"
            f"\\1c&H{bgr}&\\bord{cue.style.outline_width}\\pos({width // 2},{position_y})}}"
        )
        text = override + _ass_escape(cue.text)
        if cue.translation:
            text += r"\N" + _ass_escape(cue.translation)
        lines.append(
            f"Dialogue: 0,{_ass_time(cue.start_ms)},{_ass_time(cue.end_ms)},"
            f"Default,,0,0,0,,{text}"
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def compile_timeline(
    timeline: Timeline,
    manifest: InputManifest,
    local_assets: dict[str, Path],
    work_dir: Path,
    output_path: Path,
) -> CompiledRender:
    manifest_assets = manifest.by_id()
    input_asset_ids: list[str] = []
    input_index: dict[str, int] = {}
    args: list[str] = ["-nostdin", "-hide_banner", "-y"]

    for track in timeline.tracks:
        for clip in track.clips:
            if clip.asset_id not in input_index:
                if clip.asset_id not in manifest_assets or clip.asset_id not in local_assets:
                    raise ValueError(f"缺少本地素材: {clip.asset_id}")
                input_index[clip.asset_id] = len(input_asset_ids)
                input_asset_ids.append(clip.asset_id)
                args.extend(["-i", str(local_assets[clip.asset_id])])

    width = _even(timeline.canvas.width)
    height = _even(timeline.canvas.height)
    filters: list[str] = []
    primary = next((track for track in timeline.tracks if track.type == TrackType.VIDEO), None)
    if not primary or not primary.clips:
        raise ValueError("Timeline 必须包含至少一个视频主轨片段")

    clip_labels: list[str] = []
    for clip_no, clip in enumerate(primary.clips):
        index = input_index[clip.asset_id]
        transform = clip.transform
        chain = [
            f"trim=start={clip.source_in_ms / 1000:.3f}:end={clip.source_out_ms / 1000:.3f}",
            f"setpts=(PTS-STARTPTS)/{transform.speed:.6f}",
        ]
        if transform.rotation == 90:
            chain.append("transpose=1")
        elif transform.rotation == 180:
            chain.extend(["transpose=1", "transpose=1"])
        elif transform.rotation == 270:
            chain.append("transpose=2")
        if transform.crop:
            crop = transform.crop
            chain.append(
                "crop="
                f"iw*{crop.width:.6f}:ih*{crop.height:.6f}:"
                f"iw*{crop.x:.6f}:ih*{crop.y:.6f}"
            )
        if transform.scale_mode in {"fill", "blur"}:
            chain.append(
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}"
            )
        elif transform.scale_mode == "stretch":
            chain.append(f"scale={width}:{height}")
        else:
            chain.append(
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={timeline.canvas.background}"
            )
        chain.append(
            f"eq=brightness={transform.brightness:.3f}:"
            f"contrast={transform.contrast:.3f}:saturation={transform.saturation:.3f}"
        )
        chain.extend(["fps=" + str(timeline.canvas.fps), "format=yuv420p"])
        label = f"v{clip_no}"
        filters.append(f"[{index}:v]" + ",".join(chain) + f"[{label}]")
        clip_labels.append(label)

    transition_by_pair = {
        (item.from_clip_id, item.to_clip_id): item for item in timeline.transitions if item.type != "cut"
    }
    current = clip_labels[0]
    accumulated_seconds = primary.clips[0].duration_ms / 1000
    for index in range(1, len(primary.clips)):
        previous = primary.clips[index - 1]
        clip = primary.clips[index]
        transition = transition_by_pair.get((previous.id, clip.id))
        output_label = f"vx{index}"
        if transition:
            duration = transition.duration_ms / 1000
            offset = max(0, accumulated_seconds - duration)
            transition_name = {
                "crossfade": "fade",
                "fade": "fadeblack",
                "slide": "slideleft",
                "zoom": "zoomin",
            }[transition.type]
            filters.append(
                f"[{current}][{clip_labels[index]}]"
                f"xfade=transition={transition_name}:duration={duration:.3f}:offset={offset:.3f}"
                f"[{output_label}]"
            )
            accumulated_seconds += clip.duration_ms / 1000 - duration
        else:
            filters.append(
                f"[{current}][{clip_labels[index]}]concat=n=2:v=1:a=0[{output_label}]"
            )
            accumulated_seconds += clip.duration_ms / 1000
        current = output_label

    for overlay_no, track in enumerate(
        track for track in timeline.tracks if track.type == TrackType.OVERLAY
    ):
        for clip_no, clip in enumerate(track.clips):
            index = input_index[clip.asset_id]
            overlay_label = f"ov{overlay_no}_{clip_no}"
            composed_label = f"voc{overlay_no}_{clip_no}"
            alpha = clip.transform.opacity
            filters.append(
                f"[{index}:v]trim=start={clip.source_in_ms / 1000:.3f}:"
                f"end={clip.source_out_ms / 1000:.3f},setpts=PTS-STARTPTS,"
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"format=rgba,colorchannelmixer=aa={alpha:.3f}[{overlay_label}]"
            )
            start = clip.timeline_start_ms / 1000
            end = (clip.timeline_start_ms + clip.duration_ms) / 1000
            filters.append(
                f"[{current}][{overlay_label}]overlay=(W-w)/2:(H-h)/2:"
                f"enable='between(t,{start:.3f},{end:.3f})'[{composed_label}]"
            )
            current = composed_label

    if timeline.subtitles:
        ass_path = work_dir / "subtitles.ass"
        write_ass(timeline, ass_path)
        escaped = re.sub(r"([\\':,])", r"\\\1", str(ass_path))
        filters.append(f"[{current}]subtitles='{escaped}',format=yuv420p[vout]")
    else:
        filters.append(f"[{current}]format=yuv420p[vout]")

    audio_labels: list[str] = []
    audio_number = 0
    for track in (track for track in timeline.tracks if track.type == TrackType.AUDIO and not track.muted):
        for clip in track.clips:
            index = input_index[clip.asset_id]
            label = f"a{audio_number}"
            chain = [
                f"atrim=start={clip.source_in_ms / 1000:.3f}:end={clip.source_out_ms / 1000:.3f}",
                "asetpts=PTS-STARTPTS",
            ]
            speed = clip.transform.speed
            if speed != 1:
                chain.append(f"atempo={speed:.6f}")
            chain.append(f"volume={clip.volume:.3f}")
            if clip.fade_in_ms:
                chain.append(f"afade=t=in:st=0:d={clip.fade_in_ms / 1000:.3f}")
            if clip.fade_out_ms:
                fade_start = max(0, clip.duration_ms - clip.fade_out_ms) / 1000
                chain.append(f"afade=t=out:st={fade_start:.3f}:d={clip.fade_out_ms / 1000:.3f}")
            chain.append(f"adelay={clip.timeline_start_ms}|{clip.timeline_start_ms}")
            filters.append(f"[{index}:a]" + ",".join(chain) + f"[{label}]")
            audio_labels.append(label)
            audio_number += 1
    if audio_labels:
        joined = "".join(f"[{label}]" for label in audio_labels)
        filters.append(
            f"{joined}amix=inputs={len(audio_labels)}:duration=longest:normalize=0,"
            "loudnorm=I=-16:TP=-1.5:LRA=11[aout]"
        )
    else:
        duration = timeline.duration_ms / 1000
        filters.append(
            f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration={duration:.3f}[aout]"
        )

    filter_graph = ";\n".join(filters)
    graph_path = work_dir / "compiled-filter.txt"
    graph_path.write_text(filter_graph, encoding="utf-8")
    codec = "libx264" if timeline.output.video_codec == "h264" else "libx265"
    args.extend(
        [
            "-filter_complex_script",
            str(graph_path),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-c:v",
            codec,
            "-preset",
            "veryfast" if timeline.output.preview else "medium",
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            f"{timeline.output.video_bitrate_kbps}k",
            "-c:a",
            "aac",
            "-b:a",
            f"{timeline.output.audio_bitrate_kbps}k",
            "-movflags",
            "+faststart",
            "-t",
            f"{timeline.duration_ms / 1000:.3f}",
            "-progress",
            "pipe:1",
            "-nostats",
            str(output_path),
        ]
    )
    return CompiledRender(
        args=tuple(args),
        filter_graph=filter_graph,
        input_asset_ids=tuple(input_asset_ids),
        output_path=output_path,
    )
