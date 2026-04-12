from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from artifact_store import (
    get_error_log_path,
    get_manifest_path,
    get_output_root_dir,
    get_pipeline_log_path,
    get_today_output_dir,
    load_run_manifest,
)
from pipeline_lock import PipelineLockError, acquire_run_lock, is_lock_stale, read_run_lock
from pipeline_maintenance import clean_workspace, prune_outputs
from settings import OUTPUT_DIR, SEARCH_API_KEY_POOL, ANTIGRAVITY_API_KEY, get_today_output_dir as settings_today_output_dir, require_prompt
from trading_calendar import is_trading_day


def _configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def configure_run_logging() -> None:
    _configure_stdio()
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    pipeline_handler = RotatingFileHandler(
        get_pipeline_log_path(),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    pipeline_handler.setLevel(logging.INFO)
    pipeline_handler.setFormatter(formatter)
    root_logger.addHandler(pipeline_handler)

    error_handler = RotatingFileHandler(
        get_error_log_path(),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    root_logger.addHandler(error_handler)

    for noisy_logger in ("httpx", "httpcore", "urllib3", "google"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


def _run_orchestrator(flags: list[str], *, command_name: str) -> int:
    from orchestrator import orchestrator_main

    configure_run_logging()
    try:
        with acquire_run_lock(command=command_name):
            orchestrator_main(flags)
    except PipelineLockError as exc:
        info = exc.info or {}
        logging.error(
            "pipeline lock active: pid=%s command=%s started_at=%s",
            info.get("pid", "unknown"),
            info.get("command", "unknown"),
            info.get("started_at", "unknown"),
        )
        return 2
    return 0


def cmd_run(_: argparse.Namespace) -> int:
    return _run_orchestrator([], command_name="run")


def cmd_resume(_: argparse.Namespace) -> int:
    return _run_orchestrator([], command_name="resume")


def cmd_force_run(_: argparse.Namespace) -> int:
    return _run_orchestrator(["--force-run"], command_name="force-run")


def cmd_restart(_: argparse.Namespace) -> int:
    return _run_orchestrator(["--force-restart"], command_name="restart")


def cmd_status(_: argparse.Namespace) -> int:
    today_dir = Path(get_today_output_dir())
    manifest_path = Path(get_manifest_path())
    manifest = load_run_manifest()
    lock_info = read_run_lock()

    print(f"output_root: {get_output_root_dir()}")
    print(f"today_dir: {today_dir}")
    print(f"manifest: {manifest_path}")
    print(f"trading_day: {'yes' if is_trading_day(__import__('datetime').date.today()) else 'no'}")

    if lock_info:
        stale = is_lock_stale(lock_info, stale_after_seconds=6 * 60 * 60)
        print(
            "lock: active"
            f" pid={lock_info.get('pid', 'unknown')}"
            f" command={lock_info.get('command', 'unknown')}"
            f" started_at={lock_info.get('started_at', 'unknown')}"
            f" stale={'yes' if stale else 'no'}"
        )
    else:
        print("lock: idle")

    stages = manifest.get("stages", {})
    if not stages:
        print("stages: none")
        return 0

    print(f"run_timestamp: {manifest.get('run_timestamp', '')}")
    for stage_name in sorted(stages):
        payload = stages.get(stage_name, {})
        status = payload.get("status", "unknown")
        updated_at = payload.get("updated_at", "")
        print(f"{stage_name}: {status} {updated_at}".rstrip())
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    checks: list[tuple[str, bool, str]] = []
    today_dir = settings_today_output_dir()
    checks.append(("output_root", os.path.isdir(OUTPUT_DIR), OUTPUT_DIR))
    checks.append(("today_output", os.path.isdir(today_dir), today_dir))
    checks.append(("search_keys", bool(SEARCH_API_KEY_POOL), f"count={len(SEARCH_API_KEY_POOL)}"))
    checks.append(("antigravity_key", bool(ANTIGRAVITY_API_KEY), "configured" if ANTIGRAVITY_API_KEY else "missing"))

    for prompt_name in ("audit.txt", "p2_system.txt", "p2_user.txt", "p7a_audit.txt", "p9_report.txt"):
        try:
            require_prompt(prompt_name)
            checks.append((f"prompt:{prompt_name}", True, "ok"))
        except Exception as exc:
            checks.append((f"prompt:{prompt_name}", False, str(exc)))

    lock_info = read_run_lock()
    if lock_info:
        stale = is_lock_stale(lock_info, stale_after_seconds=6 * 60 * 60)
        checks.append(("run_lock", not stale, f"{lock_info}"))
    else:
        checks.append(("run_lock", True, "idle"))

    failed = False
    for name, ok, detail in checks:
        status = "OK" if ok else "FAIL"
        print(f"[{status}] {name}: {detail}")
        failed = failed or not ok
    return 1 if failed else 0


def cmd_clean(args: argparse.Namespace) -> int:
    result = clean_workspace(args.project_root)
    print(f"removed_dirs={result['removed_dirs']} removed_files={result['removed_files']}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    result = prune_outputs(keep_days=args.keep_days)
    print(f"removed_dirs={result['removed_dirs']} keep_days={args.keep_days}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VCP Hunter pipeline control plane")
    subparsers = parser.add_subparsers(dest="command", required=True)

    command_map = {
        "run": cmd_run,
        "resume": cmd_resume,
        "force-run": cmd_force_run,
        "restart": cmd_restart,
        "status": cmd_status,
        "doctor": cmd_doctor,
    }
    for name, handler in command_map.items():
        sub = subparsers.add_parser(name)
        sub.set_defaults(handler=handler)

    clean_parser = subparsers.add_parser("clean")
    clean_parser.add_argument("--project-root", default=str(Path(__file__).resolve().parent.parent))
    clean_parser.set_defaults(handler=cmd_clean)

    prune_parser = subparsers.add_parser("prune")
    prune_parser.add_argument("--keep-days", type=int, default=7)
    prune_parser.set_defaults(handler=cmd_prune)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
