from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
from datetime import date
from logging.handlers import RotatingFileHandler
from pathlib import Path

from artifact_store import (
    get_error_log_path,
    get_manifest_path,
    get_output_root_dir,
    get_pipeline_log_path,
    get_today_logs_dir,
    get_today_output_dir,
    load_run_manifest,
    set_preflight_summary,
    update_run_manifest,
)
from agent_assistant import explain_preflight
from pipeline_lock import PipelineLockError, acquire_run_lock, is_lock_stale, read_run_lock
from pipeline_maintenance import clean_workspace, prune_outputs
from settings import (
    OUTPUT_DIR,
    ANTIGRAVITY_API_KEY,
    GROK_API_KEY,
    INSECURE_TLS_ALLOWLIST,
    SEARCH_API_KEY_POOL,
    get_today_output_dir as settings_today_output_dir,
    require_prompt,
)
from trading_calendar import get_calendar_status, is_trading_day

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_LOCK_ACTIVE = 2

REQUIRED_PROMPTS = (
    "audit.txt",
    "p2_system.txt",
    "p2_user.txt",
    "p7a_audit.txt",
    "p9_report.txt",
)
RUNTIME_IMPORTS = (
    ("requests", "requests", "error"),
    ("feedparser", "feedparser", "error"),
    ("pydantic", "pydantic", "error"),
    ("anthropic", "anthropic", "error"),
    ("google.genai", "google-genai", "error"),
    ("openai", "openai", "error"),
    ("scrapling", "scrapling", "warning"),
)


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

    preflight_result = _collect_preflight(strict=False)
    set_preflight_summary(preflight_result["summary"])
    try:
        explain_preflight_and_record(preflight_result)
    except Exception:
        pass
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


def _format_check_line(name: str, ok: bool, detail: str, severity: str) -> str:
    if ok:
        status = "OK"
    elif severity == "lock":
        status = "LOCK"
    elif severity == "warning":
        status = "WARN"
    else:
        status = "FAIL"
    return f"[{status}] {name}: {detail}"


def _collect_preflight(strict: bool) -> dict:
    checks: list[dict] = []

    def add_check(name: str, ok: bool, detail: str, severity: str = "error") -> None:
        checks.append(
            {
                "name": name,
                "ok": bool(ok),
                "detail": detail,
                "severity": severity,
            }
        )

    today_dir = Path(settings_today_output_dir())
    logs_dir = Path(get_today_logs_dir())
    prompt_dir = Path(__file__).resolve().parent / "prompts"

    add_check("output_root", os.path.isdir(OUTPUT_DIR), OUTPUT_DIR)
    add_check("today_output", today_dir.is_dir(), str(today_dir))
    add_check("logs_dir", logs_dir.is_dir(), str(logs_dir))
    add_check("prompts_dir", prompt_dir.is_dir(), str(prompt_dir))

    add_check("search_keys", bool(SEARCH_API_KEY_POOL), f"count={len(SEARCH_API_KEY_POOL)}")
    add_check("antigravity_key", bool(ANTIGRAVITY_API_KEY), "configured" if ANTIGRAVITY_API_KEY else "missing")
    add_check("grok_key", bool(GROK_API_KEY), "configured" if GROK_API_KEY else "missing", severity="warning")
    add_check(
        "tls_allowlist",
        True,
        f"count={len(INSECURE_TLS_ALLOWLIST)} values={','.join(INSECURE_TLS_ALLOWLIST) if INSECURE_TLS_ALLOWLIST else 'none'}",
    )

    for prompt_name in REQUIRED_PROMPTS:
        try:
            require_prompt(prompt_name)
            add_check(f"prompt:{prompt_name}", True, "ok")
        except Exception as exc:
            add_check(f"prompt:{prompt_name}", False, str(exc))

    for module_name, package_name, default_severity in RUNTIME_IMPORTS:
        severity = "error" if strict and default_severity == "warning" else default_severity
        try:
            importlib.import_module(module_name)
            add_check(f"import:{module_name}", True, package_name)
        except Exception as exc:
            add_check(f"import:{module_name}", False, f"{package_name}: {exc}", severity=severity)

    calendar_status = get_calendar_status()
    calendar_mode = calendar_status.get("mode", "unknown")
    calendar_package = calendar_status.get("package_name", "unknown")
    calendar_import = calendar_status.get("import_name", "unknown")
    calendar_version = calendar_status.get("package_version") or "unknown"
    add_check(
        "calendar_mode",
        calendar_mode == "chinese_calendar" or not strict,
        f"{calendar_mode} package={calendar_package} import={calendar_import} version={calendar_version}",
        severity="warning",
    )
    try:
        is_today_trading = is_trading_day(date.today())
        add_check("calendar_probe", True, f"today_trading={is_today_trading}")
    except Exception as exc:
        add_check("calendar_probe", False, str(exc))

    lock_info = read_run_lock()
    if lock_info:
        stale = is_lock_stale(lock_info, stale_after_seconds=6 * 60 * 60)
        if stale:
            add_check("run_lock", False, f"stale={lock_info}", severity="warning")
        else:
            add_check("run_lock", False, f"active={lock_info}", severity="lock")
    else:
        add_check("run_lock", True, "idle")

    failures = [item for item in checks if not item["ok"] and item["severity"] in {"error", "lock"}]
    warnings_only = [item for item in checks if not item["ok"] and item["severity"] == "warning"]

    if any(item["severity"] == "lock" for item in failures):
        exit_code = EXIT_LOCK_ACTIVE
    elif failures or (strict and warnings_only):
        exit_code = EXIT_CHECK_FAILED
    else:
        exit_code = EXIT_OK

    return {
        "strict": strict,
        "checks": checks,
        "summary": {
            "total": len(checks),
            "failed": len(failures),
            "warnings": len(warnings_only),
            "exit_code": exit_code,
            "calendar_mode": calendar_mode,
            "search_keys_count": len(SEARCH_API_KEY_POOL),
        },
    }


def _print_preflight(result: dict) -> int:
    for item in result["checks"]:
        print(_format_check_line(item["name"], item["ok"], item["detail"], item["severity"]))

    summary = result["summary"]
    status = "PASS" if summary["exit_code"] == EXIT_OK else "FAIL"
    strict_label = "strict" if result["strict"] else "normal"
    print(
        f"[SUMMARY] status={status} mode={strict_label} total={summary['total']} "
        f"failed={summary['failed']} warnings={summary['warnings']} exit_code={summary['exit_code']}"
    )
    return int(summary["exit_code"])


def explain_preflight_and_record(result: dict) -> dict:
    agent_result = explain_preflight(result)
    status = "completed" if agent_result.get("ok") else "skipped"
    update_run_manifest(
        "agent_preflight",
        status=status,
        files={
            key: value
            for key, value in {
                "json": agent_result.get("json_path", ""),
                "md": agent_result.get("md_path", ""),
            }.items()
            if value
        } or None,
        metrics={
            "ok": bool(agent_result.get("ok")),
            "reason": agent_result.get("reason", ""),
        },
    )
    return agent_result


def _run_preflight_with_agent(strict: bool) -> int:
    result = _collect_preflight(strict=strict)
    exit_code = _print_preflight(result)
    agent_result = explain_preflight_and_record(result)
    if agent_result.get("json_path"):
        print(f"Agent preflight artifact: {agent_result['json_path']}")
    return exit_code


def cmd_doctor(args: argparse.Namespace) -> int:
    return _run_preflight_with_agent(strict=bool(getattr(args, "strict", False)))


def cmd_preflight(_: argparse.Namespace) -> int:
    return _run_preflight_with_agent(strict=True)


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
        "preflight": cmd_preflight,
    }
    for name, handler in command_map.items():
        sub = subparsers.add_parser(name)
        if name == "doctor":
            sub.add_argument("--strict", action="store_true")
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
