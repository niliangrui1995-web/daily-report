from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from artifact_store import (
    UTF8_FILE_ENCODING,
    configure_utf8_environment,
    get_error_log_path,
    get_manifest_path,
    get_output_root_dir,
    get_pipeline_log_path,
    get_today_logs_dir,
    get_today_output_dir,
    load_run_manifest,
    record_run_state,
    set_run_timestamp,
    set_preflight_summary,
    update_run_manifest,
)
from agent_assistant import explain_preflight
from pipeline_lock import PipelineLockError, acquire_run_lock, get_lock_stale_reason, read_run_lock
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
RUN_LOCK_STALE_AFTER_SECONDS = 6 * 60 * 60

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
    configure_utf8_environment()


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
        encoding=UTF8_FILE_ENCODING,
    )
    pipeline_handler.setLevel(logging.INFO)
    pipeline_handler.setFormatter(formatter)
    root_logger.addHandler(pipeline_handler)

    error_handler = RotatingFileHandler(
        get_error_log_path(),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding=UTF8_FILE_ENCODING,
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    root_logger.addHandler(error_handler)

    for noisy_logger in ("httpx", "httpcore", "urllib3", "google"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


def _run_orchestrator(flags: list[str], *, command_name: str) -> int:
    preflight_result = _collect_preflight(strict=True)
    set_preflight_summary(preflight_result["summary"])
    try:
        explain_preflight_and_record(preflight_result)
    except Exception:
        pass

    preflight_exit_code = int(preflight_result.get("summary", {}).get("exit_code", EXIT_CHECK_FAILED))
    if preflight_exit_code != EXIT_OK:
        record_run_state(
            status="not_entered_main_flow",
            root_cause="preflight_failed",
            reason=f"preflight exit_code={preflight_exit_code}",
            source=f"pipeline_cli:{command_name}",
            main_flow_entered=False,
            runtime_context={"preflight_exit_code": preflight_exit_code},
        )
        _print_preflight(preflight_result)
        return preflight_exit_code

    configure_run_logging()
    from orchestrator import orchestrator_main

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
        record_run_state(
            status="not_entered_main_flow",
            root_cause="run_lock_active",
            reason="pipeline lock active",
            source=f"pipeline_cli:{command_name}",
            main_flow_entered=False,
            runtime_context={"lock_info": info},
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


def _bool_label(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def _manifest_schedule(manifest: dict[str, Any], runtime_context: dict[str, Any]) -> dict[str, Any]:
    schedule = manifest.get("schedule_decision")
    if isinstance(schedule, dict):
        return schedule
    schedule = runtime_context.get("schedule_decision")
    return schedule if isinstance(schedule, dict) else {}


def _manifest_run_state(manifest: dict[str, Any]) -> dict[str, Any]:
    runtime_context = manifest.get("runtime_context")
    if not isinstance(runtime_context, dict):
        runtime_context = {}

    status = str(manifest.get("status") or runtime_context.get("run_state") or "").strip()
    root_cause = str(manifest.get("root_cause") or runtime_context.get("root_cause") or "").strip()
    reason = str(manifest.get("status_reason") or runtime_context.get("run_state_reason") or "").strip()
    main_flow_entered = manifest.get("main_flow_entered", runtime_context.get("main_flow_entered"))

    if not status:
        stages = manifest.get("stages", {})
        if isinstance(stages, dict) and stages and "orchestrator" not in stages:
            status = "not_entered_main_flow"
            root_cause = "missing_orchestrator_state"
            reason = "manifest has stages but no root run_state or orchestrator stage"
            main_flow_entered = False
        elif isinstance(stages, dict) and not stages:
            status = "no_run_activity"
            root_cause = "empty_manifest"
            reason = "manifest has no stages and no root run_state"

    return {
        "status": status or "unknown",
        "root_cause": root_cause or "unknown",
        "reason": reason,
        "main_flow_entered": main_flow_entered,
        "schedule": _manifest_schedule(manifest, runtime_context),
    }


def _resolve_status_target(args: argparse.Namespace) -> tuple[date, Path]:
    raw_day = str(getattr(args, "day", "") or "").strip()
    if not raw_day:
        return date.today(), Path(get_today_output_dir())

    try:
        target_day = datetime.strptime(raw_day, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError("--day must use YYYYMMDD, for example 20260623") from exc
    return target_day, Path(get_output_root_dir()) / raw_day


def _maybe_close_stale_running_manifest(
    *,
    manifest: dict[str, Any],
    run_state: dict[str, Any],
    lock_info: dict[str, Any] | None,
    lock_stale_reason: str,
    day_dir: Path,
) -> bool:
    if run_state["status"] != "running":
        return False
    if not lock_info or not lock_stale_reason:
        return False

    record_run_state(
        status="failed_stale_run",
        root_cause="stale_run_lock",
        reason=f"manifest was still running but run lock is stale: {lock_stale_reason}",
        source="pipeline_cli:status",
        main_flow_entered=True,
        timestamp=str(manifest.get("run_timestamp") or "").strip() or None,
        day_dir=str(day_dir),
        runtime_context={
            "previous_run_state": run_state["status"],
            "lock_info": lock_info,
            "lock_stale_reason": lock_stale_reason,
        },
    )
    return True


def cmd_status(args: argparse.Namespace) -> int:
    try:
        target_day, day_dir = _resolve_status_target(args)
    except ValueError as exc:
        print(f"error: {exc}")
        return EXIT_CHECK_FAILED

    manifest_path = Path(get_manifest_path(day_dir=str(day_dir)))
    manifest = load_run_manifest(day_dir=str(day_dir))
    lock_info = read_run_lock()
    lock_stale_reason = (
        get_lock_stale_reason(lock_info, stale_after_seconds=RUN_LOCK_STALE_AFTER_SECONDS) if lock_info else ""
    )
    target_is_trading_day = is_trading_day(target_day)
    run_state = _manifest_run_state(manifest)
    if _maybe_close_stale_running_manifest(
        manifest=manifest,
        run_state=run_state,
        lock_info=lock_info,
        lock_stale_reason=lock_stale_reason,
        day_dir=day_dir,
    ):
        manifest = load_run_manifest(day_dir=str(day_dir))
        run_state = _manifest_run_state(manifest)

    print(f"output_root: {get_output_root_dir()}")
    print(f"day_dir: {day_dir}")
    print(f"manifest: {manifest_path}")
    print(f"trading_day: {'yes' if target_is_trading_day else 'no'}")
    print(
        f"run_state: {run_state['status']} "
        f"root_cause={run_state['root_cause']} "
        f"main_flow_entered={_bool_label(run_state['main_flow_entered'])}"
    )
    if run_state["reason"]:
        print(f"run_reason: {run_state['reason']}")
    schedule = run_state["schedule"]
    if schedule:
        print(
            "schedule_decision:"
            f" is_trading_day={_bool_label(schedule.get('is_trading_day'))}"
            f" should_run={_bool_label(schedule.get('should_run'))}"
            f" reason={schedule.get('reason', '')}"
        )

    if lock_info:
        stale = bool(lock_stale_reason)
        lock_state = "stale" if stale else "active"
        print(
            f"lock: {lock_state}"
            f" pid={lock_info.get('pid', 'unknown')}"
            f" command={lock_info.get('command', 'unknown')}"
            f" started_at={lock_info.get('started_at', 'unknown')}"
            f" stale={'yes' if stale else 'no'}"
            f" stale_reason={lock_stale_reason or 'none'}"
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
        stale_reason = get_lock_stale_reason(lock_info, stale_after_seconds=RUN_LOCK_STALE_AFTER_SECONDS)
        if stale_reason:
            add_check("run_lock", True, f"stale_reason={stale_reason} stale={lock_info}")
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

    summary = result.get("summary", {})
    status = "PASS" if summary["exit_code"] == EXIT_OK else "FAIL"
    strict_label = "strict" if result.get("strict", False) else "normal"
    print(
        f"[SUMMARY] status={status} mode={strict_label} total={summary.get('total', 0)} "
        f"failed={summary.get('failed', 0)} warnings={summary.get('warnings', 0)} "
        f"exit_code={summary.get('exit_code', EXIT_CHECK_FAILED)}"
    )
    return int(summary.get("exit_code", EXIT_CHECK_FAILED))


def explain_preflight_and_record(result: dict) -> dict:
    manifest = load_run_manifest()
    existing_run_ts = str(manifest.get("run_timestamp", "")).strip()
    if existing_run_ts and manifest.get("stages"):
        set_run_timestamp(existing_run_ts)
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
    update_run_manifest(
        "agent_preflight",
        status="completed" if agent_result.get("ok") else "skipped",
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
        if name == "status":
            sub.add_argument("--day", help="output day in YYYYMMDD format")
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
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
