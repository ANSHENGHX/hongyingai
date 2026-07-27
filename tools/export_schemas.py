from __future__ import annotations

import json
from pathlib import Path

from hongying_ai.contracts.events import (
    AssetAnalyzeCommand,
    CancelCommand,
    EventEnvelope,
    PlanGenerateCommand,
    QualityCommand,
    RenderCommand,
)
from hongying_ai.contracts.studio import StudioGenerateRequest
from hongying_ai.domain.models import InputManifest, QualityReport, TaskSnapshot, Timeline


def main() -> None:
    output = Path("schemas/generated")
    output.mkdir(parents=True, exist_ok=True)
    models = {
        "timeline-v1": Timeline,
        "input-manifest-v1": InputManifest,
        "task-snapshot-v1": TaskSnapshot,
        "quality-report-v1": QualityReport,
        "event-envelope-v1": EventEnvelope,
        "asset-analyze-command-v1": AssetAnalyzeCommand,
        "plan-generate-command-v1": PlanGenerateCommand,
        "render-command-v1": RenderCommand,
        "quality-command-v1": QualityCommand,
        "cancel-command-v1": CancelCommand,
        "studio-generate-request-v1": StudioGenerateRequest,
    }
    for filename, model in models.items():
        path = output / f"{filename}.schema.json"
        path.write_text(
            json.dumps(model.model_json_schema(by_alias=True), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
