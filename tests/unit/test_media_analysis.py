from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

from hongying_ai.application.media import analyze_image, image_profile


def test_image_analysis_returns_quality_subject_and_histogram(tmp_path: Path) -> None:
    path = tmp_path / "dish.jpg"
    image = Image.new("RGB", (640, 480), "#f6efe5")
    for x in range(180, 460):
        for y in range(100, 400):
            image.putpixel((x, y), (190, 48, 32))
    image.save(path, quality=92)

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    profile = image_profile(
        path,
        object_key="prod/10001/material/dish.jpg",
        sha256=digest,
        size_bytes=path.stat().st_size,
        asset_id="dish",
    )
    analysis = analyze_image(path)

    assert profile.media_type == "image"
    assert (profile.width, profile.height) == (640, 480)
    assert 0 <= analysis["brightness"] <= 1
    assert 0 <= analysis["qualityScore"] <= 100
    assert analysis["subjectBox"]["width"] > 0
    assert len(analysis["colorHistogram"]["red"]) == 16
