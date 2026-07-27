from __future__ import annotations

from pathlib import Path

from hongying_ai.application.compiler import compile_timeline
from hongying_ai.application.preflight import render_preflight
from hongying_ai.domain.models import InputManifest, Timeline


def test_preflight_estimates_resources(timeline: Timeline, manifest: InputManifest) -> None:
    result = render_preflight(timeline, manifest)
    assert result.accepted is True
    assert result.estimated_seconds > 0
    assert result.estimated_disk_bytes > sum(item.size_bytes for item in manifest.assets)


def test_compiler_uses_argument_array_and_filter_script(
    tmp_path: Path, timeline: Timeline, manifest: InputManifest
) -> None:
    local_assets = {}
    for asset in manifest.assets:
        path = tmp_path / f"{asset.asset_id}.mp4"
        path.touch()
        local_assets[asset.asset_id] = path
    output = tmp_path / "output.mp4"
    compiled = compile_timeline(timeline, manifest, local_assets, tmp_path, output)
    assert "-filter_complex_script" in compiled.args
    assert "shell=True" not in " ".join(compiled.args)
    assert "concat=n=2:v=1:a=0" in compiled.filter_graph
    assert "anullsrc" not in compiled.filter_graph
    assert "[0:a]atrim" in compiled.filter_graph
    assert compiled.output_path == output
