from __future__ import annotations

import sys
from datetime import datetime

import pytest


@pytest.fixture(autouse=True)
def isolate_runtime_artifacts(tmp_path, monkeypatch):
    """Keep every test away from the operator's real daily run state."""
    import artifact_store
    import settings

    output_root = tmp_path / "runtime_outputs"
    day_dir = output_root / datetime.now().strftime("%Y%m%d")
    day_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(settings, "OUTPUT_DIR", str(output_root))
    monkeypatch.setattr(settings, "get_today_output_dir", lambda: str(day_dir))
    monkeypatch.setattr(artifact_store, "OUTPUT_DIR", str(output_root))
    monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(day_dir))
    monkeypatch.delenv(artifact_store.RUN_TIMESTAMP_ENV, raising=False)

    # These modules import the path helpers by value, so update their already
    # loaded aliases as well. Individual tests can still override the fixture.
    checkpoint = sys.modules.get("checkpoint")
    if checkpoint is not None:
        monkeypatch.setattr(checkpoint, "get_today_output_dir", lambda: str(day_dir))

    pipeline_cli = sys.modules.get("pipeline_cli")
    if pipeline_cli is not None:
        monkeypatch.setattr(pipeline_cli, "OUTPUT_DIR", str(output_root))
        monkeypatch.setattr(pipeline_cli, "settings_today_output_dir", lambda: str(day_dir))

    deep_research = sys.modules.get("p7_deep_research")
    if deep_research is not None and hasattr(deep_research, "OUTPUT_DIR"):
        monkeypatch.setattr(deep_research, "OUTPUT_DIR", str(output_root))
