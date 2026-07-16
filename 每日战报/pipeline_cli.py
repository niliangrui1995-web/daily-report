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
from urllib.parse import urlparse

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
    set_preflight_summary,
    summarize_stage_degradations,
    update_manifest_context,
)
from native_llm_adapter import validate_model_base_url
from pipeline_lock import (
    DEFAULT_STALE_AFTER_SECONDS,
    PipelineLockError,
    acquire_run_lock,
    get_lock_stale_reason,
    get_run_lock_state,
    read_run_lock,
)
from pipeline_maintenance import clean_workspace, prune_outputs
from settings import (
    OUTPUT_DIR,
    ANTIGRAVITY_API_KEY,
    EMAIL_ADDRESS,
    EMAIL_PASSWORD,
    RECEIVER_EMAIL,
    GROK_API_KEY,
    INSECURE_TLS_ALLOWLIST,
    PRO_OPUS_API_KEY,
    PRO_OPUS_BASE_URL,
    SEARCH_API_KEY_POOL,
    get_today_output_dir as settings_today_output_dir,
    require_prompt,
)
from trading_calendar import get_calendar_status, is_trading_day

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_LOCK_ACTIVE = 2
RUN_LOCK_STALE_AFTER_SECONDS = DEFAULT_STALE_AFTER_SECONDS

REQUIRED_PROMPTS = (
    "audit.txt",
    "p2_system.txt",
    "p2_user.txt",
    "p7a_audit.txt",
    "p9_report.txt",
)
LOCAL_ONLY_PREFLIGHT_RESUME_STAGES = frozenset({"roi_resume", "p9_done"})
DELIVERY_PREFLIGHT_RESUME_STAGES = frozenset({"delivery_resume"})
RUNTIME_IMPORTS = (
    ("requests", "requests", "error"),
    ("feedparser", "feedparser", "error"),
    ("pydantic", "pydantic", "error"),
    ("anthropic", "anthropic", "error"),
    ("google.genai", "google-genai", "error"),
    ("openai", "openai", "error"),
    ("scrapling", "scrapling", "warning"),
)


def _probe_local_model_endpoint(base_url: str, api_key: str) -> tuple[bool, str]:
    try:
        endpoint_ok, is_loopback, endpoint_error = validate_model_base_url(base_url)
        if not endpoint_ok:
            return False, endpoint_error
        hostname = (urlparse(base_url).hostname or "").strip().lower()
        if not api_key:
            return False, "model endpoint API key is missing"
        if not is_loopback:
            return True, f"external endpoint configured host={hostname}; live probe skipped"

        import requests

        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get(
                f"{base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=(3.0, 8.0),
            )
            status_code = int(response.status_code)
            if status_code != 200:
                return False, f"authenticated probe returned HTTP {status_code}"
            payload = response.json()
        finally:
            session.close()
        models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(models, list) or not models:
            return False, "authenticated probe returned no models"
        return True, f"authenticated status=200 models={len(models)} host={hostname}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


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
    try:
        with acquire_run_lock(command=command_name) as lock_payload:
            resume_stage = _resolve_preflight_resume_stage(flags)
            preflight_result = _collect_preflight(
                strict=True,
                resume_stage=resume_stage,
                expected_lock_owner_token=str(lock_payload.get("owner_token") or ""),
            )
            preflight_summary = preflight_result["summary"]
            preflight_exit_code = int(preflight_summary.get("exit_code", EXIT_CHECK_FAILED))
            if preflight_exit_code != EXIT_OK:
                _print_preflight(preflight_result)
                return preflight_exit_code

            set_preflight_summary(preflight_summary)
            configure_run_logging()
            update_manifest_context(
                runtime_context={"preflight_exit_code": preflight_exit_code},
                preflight=preflight_summary,
            )
            from orchestrator import orchestrator_main

            orchestrator_main(flags)
    except PipelineLockError as exc:
        info = exc.info or {}
        logging.error(
            "pipeline lock active: pid=%s command=%s started_at=%s",
            info.get("pid", "unknown"),
            info.get("command", "unknown"),
            info.get("started_at", "unknown"),
        )
        return EXIT_LOCK_ACTIVE
    return 0


def _resolve_preflight_resume_stage(flags: list[str]) -> str:
    if "--force" in flags or "--force-restart" in flags:
        return "none"

    from checkpoint import detect_checkpoint

    checkpoint = detect_checkpoint()
    return str(checkpoint.get("stage") or "none").strip() or "none"


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
    stages = manifest.get("stages", {})
    if not isinstance(stages, dict):
        stages = {}
    orchestrator_metrics = {}
    orchestrator_payload = stages.get("orchestrator", {})
    if isinstance(orchestrator_payload, dict):
        metrics = orchestrator_payload.get("metrics", {})
        if isinstance(metrics, dict):
            orchestrator_metrics = metrics

    status = str(manifest.get("status") or runtime_context.get("run_state") or "").strip()
    root_cause = str(manifest.get("root_cause") or runtime_context.get("root_cause") or "").strip()
    reason = str(manifest.get("status_reason") or runtime_context.get("run_state_reason") or "").strip()
    main_flow_entered = manifest.get("main_flow_entered", runtime_context.get("main_flow_entered"))
    resume_from = str(runtime_context.get("resume_from") or orchestrator_metrics.get("resume_from") or "").strip()
    resume_start_stage = str(
        runtime_context.get("resume_start_stage") or orchestrator_metrics.get("resume_start_stage") or ""
    ).strip()
    completed_after_resume = runtime_context.get("completed_after_resume")
    if completed_after_resume is None:
        completed_after_resume = orchestrator_metrics.get("completed_after_resume")
    if completed_after_resume is None:
        completed_after_resume = bool(
            status.startswith("completed") and resume_from and resume_from not in {"none", "p9_done"}
        )

    if not status:
        preflight = manifest.get("preflight", {})
        if not isinstance(preflight, dict):
            preflight = {}
        preflight_exit_code = preflight.get("exit_code", runtime_context.get("preflight_exit_code"))
        preflight_only_stages = not stages
        if preflight_exit_code == EXIT_OK and preflight_only_stages:
            status = "preflight_passed_no_run"
            root_cause = "preflight_only"
            reason = "standalone preflight passed; main flow was not started"
            main_flow_entered = False
        elif isinstance(stages, dict) and stages and "orchestrator" not in stages:
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
        "degraded_stages": summarize_stage_degradations(manifest),
        "completed_after_resume": bool(completed_after_resume),
        "resume_from": resume_from,
        "resume_start_stage": resume_start_stage,
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


def _lock_matches_target_day(lock_info: dict[str, Any] | None, target_day: date) -> bool:
    if not lock_info:
        return False
    run_day = str(lock_info.get("run_day") or "").strip()
    if not run_day:
        run_day = str(lock_info.get("run_timestamp") or "").strip()[:8]
    if run_day:
        return run_day == target_day.strftime("%Y%m%d")
    return target_day == date.today()


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
    global_lock_info = read_run_lock()
    lock_is_related = _lock_matches_target_day(global_lock_info, target_day)
    unrelated_lock_info = global_lock_info if global_lock_info and not lock_is_related else None
    lock_info = global_lock_info if lock_is_related else None
    lock_state = get_run_lock_state(lock_info)
    lock_stale_reason = (
        get_lock_stale_reason(lock_info, stale_after_seconds=RUN_LOCK_STALE_AFTER_SECONDS)
        if lock_state != "missing"
        else ""
    )
    target_is_trading_day = is_trading_day(target_day)
    run_state = _manifest_run_state(manifest)
    control_plane_inconsistency = run_state["status"] == "running" and lock_state in {"missing", "invalid"}
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
    run_state_line = (
        f"run_state: {run_state['status']} "
        f"root_cause={run_state['root_cause']} "
        f"main_flow_entered={_bool_label(run_state['main_flow_entered'])}"
    )
    if run_state["completed_after_resume"]:
        run_state_line += " completed_after_resume=yes"
        if run_state["resume_from"]:
            run_state_line += f" resume_from={run_state['resume_from']}"
        if run_state["resume_start_stage"]:
            run_state_line += f" resume_start_stage={run_state['resume_start_stage']}"
    print(run_state_line)
    if run_state["reason"]:
        print(f"run_reason: {run_state['reason']}")
    degraded_stages = run_state["degraded_stages"]
    if degraded_stages:
        stage_text = ", ".join(
            f"{item['stage']}={item['status']}"
            + (f" reason={item['reason']}" if item.get("reason") else "")
            for item in degraded_stages
        )
        print(f"degraded_stages: {stage_text}")
    schedule = run_state["schedule"]
    if schedule:
        print(
            "schedule_decision:"
            f" is_trading_day={_bool_label(schedule.get('is_trading_day'))}"
            f" should_run={_bool_label(schedule.get('should_run'))}"
            f" reason={schedule.get('reason', '')}"
        )

    if unrelated_lock_info:
        print(
            "lock: unrelated"
            f" run_day={unrelated_lock_info.get('run_day', 'unknown')}"
            f" target_day={target_day.strftime('%Y%m%d')}"
        )
    elif lock_state == "valid":
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
    elif lock_state == "invalid":
        stale = bool(lock_stale_reason)
        print(
            "lock: invalid"
            f" stale={'yes' if stale else 'no'}"
            f" stale_reason={lock_stale_reason or 'none'}"
            f" error={lock_info.get('error', 'unknown')}"
        )
    else:
        print("lock: missing")
    if control_plane_inconsistency:
        print(f"control_plane_inconsistency: manifest=running lock={get_run_lock_state(lock_info)}")

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


def _collect_preflight(
    strict: bool,
    *,
    allow_maintenance_lock: bool = False,
    resume_stage: str = "",
    expected_lock_owner_token: str = "",
) -> dict:
    checks: list[dict] = []
    normalized_resume_stage = str(resume_stage or "").strip().lower()
    local_only = normalized_resume_stage in LOCAL_ONLY_PREFLIGHT_RESUME_STAGES
    delivery_only = normalized_resume_stage in DELIVERY_PREFLIGHT_RESUME_STAGES
    full_preflight = not (local_only or delivery_only)

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

    add_check("output_root", os.path.isdir(OUTPUT_DIR), OUTPUT_DIR)
    add_check("today_output", today_dir.is_dir(), str(today_dir))
    add_check("logs_dir", logs_dir.is_dir(), str(logs_dir))
    calendar_mode = "not_checked"

    if full_preflight:
        prompt_dir = Path(__file__).resolve().parent / "prompts"
        add_check("prompts_dir", prompt_dir.is_dir(), str(prompt_dir))
        add_check("search_keys", bool(SEARCH_API_KEY_POOL), f"count={len(SEARCH_API_KEY_POOL)}")
        add_check(
            "antigravity_key",
            bool(ANTIGRAVITY_API_KEY),
            "configured" if ANTIGRAVITY_API_KEY else "missing",
        )
        endpoint_ok, endpoint_detail = _probe_local_model_endpoint(PRO_OPUS_BASE_URL, PRO_OPUS_API_KEY)
        add_check("model_endpoint", endpoint_ok, endpoint_detail)
        fallback_host = (urlparse(PRO_OPUS_BASE_URL).hostname or "").strip().lower()
        fallback_detail = (
            f"same endpoint model failover host={fallback_host}"
            if endpoint_ok
            else f"shared model endpoint unavailable: {endpoint_detail}"
        )
        add_check("opus_fallback", endpoint_ok, fallback_detail)

    if full_preflight or delivery_only:
        delivery_requires_email = True
        if delivery_only:
            try:
                manifest = load_run_manifest()
                p9_payload = manifest.get("stages", {}).get("p9", {})
                p9_metrics = p9_payload.get("metrics", {}) if isinstance(p9_payload, dict) else {}
                delivery_requires_email = not (
                    isinstance(p9_metrics, dict)
                    and p9_metrics.get("email_configured") is False
                )
            except Exception:
                delivery_requires_email = True

        email_configured = bool(EMAIL_ADDRESS and EMAIL_PASSWORD and RECEIVER_EMAIL)
        if delivery_only and not delivery_requires_email:
            add_check(
                "email_credentials",
                True,
                "not required by saved local-only report",
            )
        else:
            add_check(
                "email_credentials",
                email_configured,
                (
                    "configured"
                    if email_configured
                    else "missing sender, receiver, or SMTP authorization code"
                ),
            )

    if full_preflight:
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
    lock_state = get_run_lock_state(lock_info)
    if lock_state == "valid":
        current_owner_token = str(lock_info.get("owner_token") or "")
        lock_is_owned = bool(
            expected_lock_owner_token
            and current_owner_token
            and current_owner_token == expected_lock_owner_token
        )
        stale_reason = ""
        if not lock_is_owned:
            stale_reason = get_lock_stale_reason(lock_info, stale_after_seconds=RUN_LOCK_STALE_AFTER_SECONDS)
        if lock_is_owned:
            add_check("run_lock", True, f"owned pid={lock_info.get('pid')}")
        elif stale_reason:
            add_check("run_lock", True, f"stale_reason={stale_reason} stale={lock_info}")
        elif allow_maintenance_lock and lock_info.get("command") == "maintenance":
            add_check("run_lock", True, f"maintenance_allowed pid={lock_info.get('pid')}")
        else:
            add_check("run_lock", False, f"active={lock_info}", severity="lock")
    elif lock_state == "invalid":
        stale_reason = get_lock_stale_reason(lock_info, stale_after_seconds=RUN_LOCK_STALE_AFTER_SECONDS)
        if stale_reason:
            add_check("run_lock", True, f"invalid_stale_reason={stale_reason} recoverable={lock_info}")
        else:
            add_check("run_lock", False, f"invalid_active={lock_info}", severity="lock")
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


def _run_preflight(strict: bool, *, allow_maintenance_lock: bool = False) -> int:
    if allow_maintenance_lock:
        result = _collect_preflight(strict=strict, allow_maintenance_lock=True)
    else:
        result = _collect_preflight(strict=strict)
    exit_code = _print_preflight(result)
    update_manifest_context(
        runtime_context={"preflight_exit_code": exit_code},
        preflight=result.get("summary", {}),
    )
    return exit_code


def cmd_doctor(args: argparse.Namespace) -> int:
    return _run_preflight(strict=bool(getattr(args, "strict", False)))


def cmd_preflight(_: argparse.Namespace) -> int:
    allow_maintenance_lock = os.environ.get("VCP_PREFLIGHT_ALLOW_MAINTENANCE_LOCK", "") == "1"
    return _run_preflight(
        strict=True,
        allow_maintenance_lock=allow_maintenance_lock,
    )


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
