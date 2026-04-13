from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

from artifact_store import get_output_root_dir

CACHE_DIR_NAMES = {"__pycache__", ".pytest_cache", ".ruff_cache"}
TRANSIENT_OUTPUT_DIRS = {"raw", "audit", "logs", "system", "agent"}
LEGACY_LOG_FILES = {"pipeline.log", "error.log"}


def clean_workspace(project_root: str) -> dict[str, int]:
    root = Path(project_root)
    removed_dirs = 0
    removed_files = 0

    for path in root.rglob("*"):
        if path.is_dir() and path.name in CACHE_DIR_NAMES:
            shutil.rmtree(path, ignore_errors=True)
            removed_dirs += 1

    output_root = Path(get_output_root_dir())
    for name in LEGACY_LOG_FILES:
        legacy_file = output_root / name
        if legacy_file.exists():
            legacy_file.unlink()
            removed_files += 1

    return {"removed_dirs": removed_dirs, "removed_files": removed_files}


def prune_outputs(output_root: str | None = None, *, keep_days: int = 7) -> dict[str, int]:
    root = Path(output_root or get_output_root_dir())
    cutoff = datetime.now() - timedelta(days=max(keep_days, 0))
    removed_dirs = 0

    if not root.exists():
        return {"removed_dirs": 0}

    for day_dir in root.iterdir():
        if not day_dir.is_dir():
            continue
        if not day_dir.name.isdigit() or len(day_dir.name) != 8:
            continue
        try:
            day_value = datetime.strptime(day_dir.name, "%Y%m%d")
        except ValueError:
            continue
        if day_value >= cutoff:
            continue

        for transient in TRANSIENT_OUTPUT_DIRS:
            target = day_dir / transient
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
                removed_dirs += 1

    return {"removed_dirs": removed_dirs}
