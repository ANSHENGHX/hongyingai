from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

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


def _atempo_filters(speed: float) -> list[str]:
    values: list[float] = []
    remaining = speed
    while remaining < 0.5:
        values.append(0.5)
        remaining /= 0.5
    while remaining > 2:
        values.append(2)
        remaining /= 2
    if abs(remaining - 1) > 0.000001:
        values.append(remaining)
    return [f"atempo={value:.6f}" for value in values]


def _overlay_position(position: str, margin: int) -> tuple[str, str]:
    values = {
        "center": ("(W-w)/2", "(H-h)/2"),
        "top_left": (str(margin), str(margin)),
        "top_right": (f"W-w-{margin}", str(margin)),
        "bottom_left": (str(margin), f"H-h-{margin}"),
        "bottom_right": (f"W-w-{margin}", f"H-h-{margin}"),
    }
    return values[position]


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


def _font_path(requested: str) -> str | None:
    candidates = (
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/STHeiti Light.ttc"),
        Path("/Library/Fonts/Arial Unicode.ttf"),
    )
    direct = Path(requested)
    if direct.is_file():
        return str(direct)
    return next((str(path) for path in candidates if path.is_file()), None)


def _subtitle_image(timeline: Timeline, cue_no: int, path: Path) -> None:
    cue = timeline.subtitles[cue_no]
    width, height = timeline.canvas.width, timeline.canvas.height
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    font_path = _font_path(cue.style.font)
    font = (
        ImageFont.truetype(font_path, cue.style.font_size)
        if font_path
        else ImageFont.load_default(size=cue.style.font_size)
    )
    text = cue.text + (f"\n{cue.translation}" if cue.translation else "")
    max_characters = max(8, round(width / max(1, cue.style.font_size) * 1.55))
    wrapped: list[str] = []
    for raw_line in text.splitlines():
        wrapped.extend(
            raw_line[index : index + max_characters]
            for index in range(0, len(raw_line), max_characters)
        )
    rendered = "\n".join(wrapped[: cue.style.max_lines])
    bbox = draw.multiline_textbbox(
        (0, 0),
        rendered,
        font=font,
        stroke_width=cue.style.outline_width,
        spacing=round(cue.style.font_size * 0.25),
        align="center",
    )
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = (width - text_width) // 2
    y = round(height * cue.style.position_y - text_height / 2)
    draw.multiline_text(
        (x, y),
        rendered,
        font=font,
        fill=cue.style.color,
        stroke_width=cue.style.outline_width,
        stroke_fill=cue.style.outline_color,
        spacing=round(cue.style.font_size * 0.25),
        align="center",
    )
    image.save(path, "PNG", optimize=True)


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
                if manifest_assets[clip.asset_id].media_type == "image":
                    args.extend(["-loop", "1", "-framerate", str(timeline.canvas.fps)])
                args.extend(["-i", str(local_assets[clip.asset_id])])

    width = _even(timeline.canvas.width)
    height = _even(timeline.canvas.height)
    filters: list[str] = []
    primary = next((track for track in timeline.tracks if track.type == TrackType.VIDEO), None)
    if not primary or not primary.clips:
        raise ValueError("Timeline 必须包含至少一个视频主轨片段")
    primary_clips = tuple(sorted(primary.clips, key=lambda item: item.timeline_start_ms))

    clip_labels: list[str] = []
    for clip_no, clip in enumerate(primary_clips):
        index = input_index[clip.asset_id]
        transform = clip.transform
        asset = manifest_assets[clip.asset_id]
        if asset.media_type == "image":
            chain = [
                f"trim=duration={clip.duration_ms / 1000:.3f}",
                "setpts=PTS-STARTPTS",
            ]
        else:
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
        if transform.scale_mode == "blur":
            prepared = f"vp{clip_no}"
            background = f"vbg{clip_no}"
            foreground = f"vfg{clip_no}"
            composed = f"vbc{clip_no}"
            filters.append(f"[{index}:v]" + ",".join(chain) + f",split=2[{prepared}][{foreground}]")
            filters.append(
                f"[{prepared}]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},boxblur=24:12[{background}]"
            )
            filters.append(
                f"[{foreground}]scale={width}:{height}:force_original_aspect_ratio=decrease"
                f"[vfgs{clip_no}]"
            )
            filters.append(
                f"[{background}][vfgs{clip_no}]overlay=(W-w)/2:(H-h)/2[{composed}]"
            )
            label = f"v{clip_no}"
            filters.append(
                f"[{composed}]eq=brightness={transform.brightness:.3f}:"
                f"contrast={transform.contrast:.3f}:saturation={transform.saturation:.3f},"
                f"fps={timeline.canvas.fps},format=yuv420p[{label}]"
            )
            clip_labels.append(label)
            continue
        if transform.scale_mode == "fill":
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
    accumulated_seconds = primary_clips[0].duration_ms / 1000
    for index in range(1, len(primary_clips)):
        previous = primary_clips[index - 1]
        clip = primary_clips[index]
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
            asset = manifest_assets[clip.asset_id]
            trim = (
                f"trim=duration={clip.duration_ms / 1000:.3f}"
                if asset.media_type == "image"
                else (
                    f"trim=start={clip.source_in_ms / 1000:.3f}:"
                    f"end={clip.source_out_ms / 1000:.3f}"
                )
            )
            scale_width = max(2, _even(round(width * clip.transform.overlay_scale)))
            filters.append(
                f"[{index}:v]{trim},"
                f"setpts=PTS-STARTPTS+{clip.timeline_start_ms / 1000:.3f}/TB,"
                f"scale={scale_width}:-2:force_original_aspect_ratio=decrease,"
                f"format=rgba,colorchannelmixer=aa={alpha:.3f}[{overlay_label}]"
            )
            start = clip.timeline_start_ms / 1000
            end = (clip.timeline_start_ms + clip.duration_ms) / 1000
            position_x, position_y = _overlay_position(
                clip.transform.position, clip.transform.margin
            )
            filters.append(
                f"[{current}][{overlay_label}]overlay={position_x}:{position_y}:"
                f"enable='between(t,{start:.3f},{end:.3f})'[{composed_label}]"
            )
            current = composed_label

    for cue_no, cue in enumerate(timeline.subtitles):
        subtitle_path = work_dir / f"subtitle-{cue_no + 1:03d}.png"
        _subtitle_image(timeline, cue_no, subtitle_path)
        subtitle_index = len(input_asset_ids) + cue_no
        args.extend(
            ["-loop", "1", "-framerate", str(timeline.canvas.fps), "-i", str(subtitle_path)]
        )
        subtitle_label = f"sub{cue_no}"
        composed_label = f"vsub{cue_no}"
        start = cue.start_ms / 1000
        end = cue.end_ms / 1000
        filters.append(
            f"[{subtitle_index}:v]trim=duration={(cue.end_ms - cue.start_ms) / 1000:.3f},"
            f"setpts=PTS-STARTPTS+{start:.3f}/TB,format=rgba[{subtitle_label}]"
        )
        filters.append(
            f"[{current}][{subtitle_label}]overlay=0:0:"
            f"enable='between(t,{start:.3f},{end:.3f})'[{composed_label}]"
        )
        current = composed_label
    filters.append(f"[{current}]format=yuv420p[vout]")

    audio_labels: list[str] = []
    audio_number = 0
    audio_tracks = tuple(
        track for track in timeline.tracks if track.type == TrackType.AUDIO and not track.muted
    )
    audio_clips = (
        tuple(clip for track in audio_tracks for clip in track.clips)
        if audio_tracks
        else tuple(
            clip
            for clip in primary_clips
            if manifest_assets[clip.asset_id].media_type == "video"
            and manifest_assets[clip.asset_id].has_audio
        )
    )
    for clip in audio_clips:
        index = input_index[clip.asset_id]
        label = f"a{audio_number}"
        chain = [
            f"atrim=start={clip.source_in_ms / 1000:.3f}:end={clip.source_out_ms / 1000:.3f}",
            "asetpts=PTS-STARTPTS",
        ]
        speed = clip.transform.speed
        if speed != 1:
            chain.extend(_atempo_filters(speed))
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
