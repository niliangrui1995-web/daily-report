from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Hold the pipeline lock during scheduled-task migration.")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--release-file", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()

    app_root = args.project_root.resolve() / "每日战报"
    sys.path.insert(0, str(app_root))
    from pipeline_lock import acquire_run_lock

    args.ready_file.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(30, args.timeout_seconds)
    try:
        with acquire_run_lock(command="maintenance", heartbeat_interval_seconds=10):
            temp_ready = args.ready_file.with_name(f".{args.ready_file.name}.{os.getpid()}.tmp")
            temp_ready.write_text(str(os.getpid()), encoding="ascii")
            os.replace(temp_ready, args.ready_file)
            while not args.release_file.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("maintenance lock release timed out")
                time.sleep(0.25)
    finally:
        args.ready_file.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
