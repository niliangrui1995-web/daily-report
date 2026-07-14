from __future__ import annotations

from datetime import datetime, timedelta

import pipeline_maintenance


def test_clean_workspace_only_removes_known_caches_and_legacy_logs(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    output_root = tmp_path / "output"
    cache_dir = project_root / "nested" / "__pycache__"
    source_dir = project_root / "每日战报"
    cache_dir.mkdir(parents=True)
    source_dir.mkdir(parents=True)
    (cache_dir / "cached.pyc").write_bytes(b"cache")
    source_file = source_dir / "keep.py"
    source_file.write_text("print('keep')\n", encoding="utf-8")
    output_root.mkdir()
    (output_root / "pipeline.log").write_text("legacy", encoding="utf-8")
    keep_file = output_root / "keep.md"
    keep_file.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(pipeline_maintenance, "get_output_root_dir", lambda: str(output_root))

    result = pipeline_maintenance.clean_workspace(str(project_root))

    assert result == {"removed_dirs": 1, "removed_files": 1}
    assert cache_dir.exists() is False
    assert source_file.exists() is True
    assert keep_file.exists() is True


def test_prune_outputs_removes_only_old_transient_directories(tmp_path):
    old_day = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
    recent_day = datetime.now().strftime("%Y%m%d")
    old_dir = tmp_path / old_day
    recent_dir = tmp_path / recent_day
    invalid_dir = tmp_path / "20260231"
    named_dir = tmp_path / "archive"
    for path in (old_dir, recent_dir, invalid_dir, named_dir):
        path.mkdir()
        (path / "logs").mkdir()
    permanent = old_dir / "report.md"
    permanent.write_text("keep", encoding="utf-8")

    result = pipeline_maintenance.prune_outputs(str(tmp_path), keep_days=7)

    assert result == {"removed_dirs": 1}
    assert (old_dir / "logs").exists() is False
    assert permanent.exists() is True
    assert (recent_dir / "logs").exists() is True
    assert (invalid_dir / "logs").exists() is True
    assert (named_dir / "logs").exists() is True


def test_prune_outputs_is_noop_when_root_is_missing(tmp_path):
    assert pipeline_maintenance.prune_outputs(str(tmp_path / "missing")) == {"removed_dirs": 0}
