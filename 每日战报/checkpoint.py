from __future__ import annotations

import hashlib
from pathlib import Path

from artifact_store import extract_timestamp_from_name, load_run_manifest
from settings import get_today_output_dir

FALLBACK_PATTERNS: dict[str, tuple[str, ...]] = {
    "war_report": ("战报_*.md",),
    "p8": ("P8_技术面_*.md",),
    "p7a": ("P7a_深研审计_*.md",),
    "p7": ("P7_深度研究_*.md",),
    "p75": ("P7.5_补充审计_*.md",),
    "p6": ("P6_映射_*.md",),
    "p5": ("P5_审计_*.md",),
    "p4": ("P4_核心行情_*.md", "P4_海外行情_*.md"),
    "p3": ("P3_机动情报_*.md",),
    "p2": ("P2_X平台情报_*.md",),
    "p1": ("P1_*原始数据_*.json",),
    "p1_md": ("P1_*原始数据_*.md",),
}

P9_DELIVERY_SUCCESS_STATUSES = frozenset({"completed_email_sent", "completed_no_email"})
STAGE_SUCCESS_STATUSES = frozenset(
    {
        "completed",
        "completed_recovered",
        "completed_structured_degraded",
    }
)
ROOT_SUCCESS_STATUSES = frozenset(
    {
        "completed",
        "completed_existing_report",
        "completed_with_stage_degradation",
    }
)
DELIVERY_RECOVERABLE_STATUSES = frozenset(
    {
        "completed_email_failed",
        "failed_delivery",
        "report_save_in_progress",
        "report_saved_pending_delivery",
    }
)
P6_HASHED_RESEARCH_STAGES = ("p7", "p7a", "p75")
INTEL_STAGES = ("p1", "p2", "p3")


def _pick_existing_file(path: str, *, require_nonempty: bool = False) -> str:
    if not path:
        return ""
    candidate = Path(path)
    try:
        if not candidate.is_file():
            return ""
        if require_nonempty and candidate.stat().st_size <= 0:
            return ""
    except OSError:
        return ""
    return path


def get_file_mtime(files: dict[str, str], file_key: str) -> float:
    path = files.get(file_key, "")
    if not path:
        return 0.0
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def compute_file_sha256(path: str) -> str:
    if not path:
        return ""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def is_older_than_any(files: dict[str, str], file_key: str, upstream_keys: tuple[str, ...]) -> bool:
    target_mtime = get_file_mtime(files, file_key)
    if not target_mtime:
        return False
    return any(get_file_mtime(files, upstream_key) > target_mtime for upstream_key in upstream_keys)


def _manifest_p6_input_hashes(manifest: dict) -> dict[str, str]:
    hashes = {}
    for stage_name in P6_HASHED_RESEARCH_STAGES:
        payload = _current_stage_payload(manifest, stage_name)
        metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
        value = str(metrics.get("p6_input_sha256") or "").strip().lower()
        if value:
            hashes[stage_name] = value
    return hashes


def _p6_input_hash_is_stale(
    files: dict[str, str],
    p6_input_hashes: dict[str, str],
    stage_name: str,
) -> bool:
    return not p6_input_hash_matches(files, p6_input_hashes, stage_name)


def p6_input_hash_matches(
    files: dict[str, str],
    p6_input_hashes: dict[str, str],
    stage_name: str,
) -> bool:
    if not files.get(stage_name):
        return True
    expected = str(p6_input_hashes.get(stage_name) or "").strip().lower()
    actual = compute_file_sha256(files.get("p6", ""))
    return bool(expected and actual and expected == actual)


def _drop_stale_downstream_files(
    files: dict[str, str],
    p6_input_hashes: dict[str, str],
) -> None:
    p7_hash_stale = _p6_input_hash_is_stale(files, p6_input_hashes, "p7")
    p75_hash_stale = _p6_input_hash_is_stale(files, p6_input_hashes, "p75")
    p7a_hash_stale = _p6_input_hash_is_stale(files, p6_input_hashes, "p7a")
    p7_stale = bool(files.get("p7")) and p7_hash_stale
    p75_stale = bool(files.get("p75")) and p75_hash_stale
    p7a_stale = bool(files.get("p7a")) and (
        not files.get("p7")
        or p7_stale
        or p7a_hash_stale
        or is_older_than_any(files, "p7a", ("p7",))
    )
    p6_dependency_stale = p7_hash_stale or p75_hash_stale or p7a_hash_stale

    for file_key, stale in (("p7", p7_stale), ("p75", p75_stale), ("p7a", p7a_stale)):
        if stale:
            files.pop(file_key, None)

    if p6_dependency_stale or (files.get("p8") and is_older_than_any(files, "p8", ("p6",))):
        files.pop("p8", None)

    if p6_dependency_stale or (
        files.get("war_report")
        and is_older_than_any(
            files,
            "war_report",
            ("p8", "p75", "p7a", "p7", "p6", "p5", "p4", "p3", "p2"),
        )
    ):
        files.pop("war_report", None)


def _current_stage_payload(manifest: dict, stage_name: str) -> dict:
    stages = manifest.get("stages", {})
    if not isinstance(stages, dict):
        return {}
    payload = stages.get(stage_name, {})
    if not isinstance(payload, dict):
        return {}

    manifest_run_ts = str(manifest.get("run_timestamp") or "").strip()
    stage_run_ts = str(payload.get("run_timestamp") or "").strip()
    if manifest_run_ts and stage_run_ts and manifest_run_ts != stage_run_ts:
        return {}
    return payload


def _stage_status(manifest: dict, stage_name: str) -> str:
    return str(_current_stage_payload(manifest, stage_name).get("status", ""))


def _has_terminal_success(manifest: dict, files: dict[str, str]) -> bool:
    return bool(
        files.get("war_report")
        and _stage_status(manifest, "p9") in P9_DELIVERY_SUCCESS_STATUSES
        and _stage_status(manifest, "report") in STAGE_SUCCESS_STATUSES
        and _stage_status(manifest, "roi") in STAGE_SUCCESS_STATUSES
        and str(manifest.get("status", "")) in ROOT_SUCCESS_STATUSES
    )


def _needs_delivery_resume(manifest: dict, files: dict[str, str]) -> bool:
    if not files.get("war_report"):
        return False
    p9 = _current_stage_payload(manifest, "p9")
    status = str(p9.get("status", ""))
    metrics = p9.get("metrics", {})
    return status in DELIVERY_RECOVERABLE_STATUSES or (
        metrics.get("email_configured") is True and metrics.get("email_sent") is False
    )


def _needs_roi_resume(manifest: dict, files: dict[str, str]) -> bool:
    return bool(
        files.get("war_report")
        and _stage_status(manifest, "p9") in P9_DELIVERY_SUCCESS_STATUSES
        and not _has_terminal_success(manifest, files)
    )


def _missing_intel_resume_stages(manifest: dict, files: dict[str, str]) -> list[str]:
    current_statuses = {stage: _stage_status(manifest, stage).strip().lower() for stage in INTEL_STAGES}
    has_resume_anchor = bool(
        any(files.get(key) for key in (*INTEL_STAGES, "p5"))
        or any(current_statuses.values())
        or _stage_status(manifest, "collect_intel")
    )
    if not has_resume_anchor:
        return []

    missing: list[str] = []
    for stage_name in INTEL_STAGES:
        if _pick_existing_file(files.get(stage_name, ""), require_nonempty=True):
            continue

        status = current_statuses[stage_name]
        if stage_name == "p2":
            retryable = (
                not status
                or status in {"pending", "running", "retrying"}
                or status.startswith("failed")
                or status.startswith("auth_failed_")
            )
        else:
            retryable = status not in {"skipped_no_results", "completed_no_results"}

        if retryable:
            missing.append(stage_name)
    return missing


def _needs_p6_resume(manifest: dict, files: dict[str, str]) -> bool:
    has_recoverable_upstream = any(
        _pick_existing_file(files.get(stage_name, ""), require_nonempty=True)
        for stage_name in ("p2", "p3", "p4", "p5")
    )
    if not has_recoverable_upstream:
        return False

    p6_payload = _current_stage_payload(manifest, "p6")
    p6_status = str(p6_payload.get("status", "")).strip().lower()
    market_mapping_status = _stage_status(manifest, "market_mapping").strip().lower()
    p6_was_attempted = bool(p6_status) or market_mapping_status in {
        "running",
        "retrying",
        "completed",
    } or market_mapping_status.startswith("failed")
    if not p6_was_attempted:
        return False

    p6_files = p6_payload.get("files", {}) if isinstance(p6_payload, dict) else {}
    current_markdown = _pick_existing_file(
        str(p6_files.get("md", "")),
        require_nonempty=True,
    )
    return not (p6_status in STAGE_SUCCESS_STATUSES and current_markdown)


def _load_files_from_manifest(ckpt: dict, manifest: dict) -> None:
    manifest_run_ts = str(manifest.get("run_timestamp") or "").strip()

    def _store(
        stage_name: str,
        file_key: str,
        *preferred_keys: str,
        require_nonempty: bool = False,
    ) -> None:
        payload = _current_stage_payload(manifest, stage_name)
        files = payload.get("files", {})
        for preferred in preferred_keys:
            existing = _pick_existing_file(files.get(preferred, ""), require_nonempty=require_nonempty)
            artifact_run_ts = extract_timestamp_from_name(existing)
            if existing and not (
                manifest_run_ts
                and artifact_run_ts
                and artifact_run_ts != manifest_run_ts
            ):
                ckpt["files"][file_key] = existing
                return

    _store("p1", "p1", "json", "md")
    _store("p1", "p1_md", "md")
    _store("p2", "p2", "md", "json")
    _store("p3", "p3", "md", "json")
    _store("p4", "p4", "md", "json")
    _store("p5", "p5", "md", "json")
    _store("p6", "p6", "md", "json")
    _store("p7", "p7", "md", "json")
    _store("p7a", "p7a", "md", "json")
    _store("p75", "p75", "md", "json")
    _store("p8", "p8", "md", "json")
    _store("p9", "war_report", "md", require_nonempty=True)


def _merge_files_from_fallback(
    ckpt: dict,
    day_dir: Path,
    *,
    run_timestamp: str = "",
) -> None:
    candidates_by_key: dict[str, list[Path]] = {}
    timestamped_candidates: list[str] = []

    for file_key, patterns in FALLBACK_PATTERNS.items():
        candidates: list[Path] = []
        seen_paths: set[str] = set()
        for pattern in patterns:
            for match in sorted(day_dir.rglob(pattern), reverse=True):
                normalized = str(match)
                if normalized in seen_paths:
                    continue
                seen_paths.add(normalized)
                candidates.append(match)
                timestamp = extract_timestamp_from_name(normalized)
                if timestamp:
                    timestamped_candidates.append(timestamp)
        candidates_by_key[file_key] = candidates

    target_run_ts = str(run_timestamp or "").strip()
    if not target_run_ts and timestamped_candidates:
        target_run_ts = max(timestamped_candidates)
        ckpt["run_timestamp"] = target_run_ts

    for file_key, candidates in candidates_by_key.items():
        if file_key in ckpt["files"]:
            continue
        matched_path = ""
        for match in candidates:
            candidate_run_ts = extract_timestamp_from_name(str(match))
            if target_run_ts and candidate_run_ts != target_run_ts:
                continue
            if not target_run_ts and candidate_run_ts:
                continue
            candidate = _pick_existing_file(str(match), require_nonempty=file_key == "war_report")
            if candidate:
                matched_path = candidate
                break
        if matched_path:
            ckpt["files"][file_key] = matched_path


def _manifest_has_recovery_state(manifest: dict) -> bool:
    run_timestamp = str(manifest.get("run_timestamp") or "").strip()
    if (
        manifest.get("corrupt_manifest")
        or not run_timestamp
        or extract_timestamp_from_name(run_timestamp) != run_timestamp
    ):
        return False

    stages = manifest.get("stages", {})
    return bool(
        (isinstance(stages, dict) and stages)
        or str(manifest.get("status") or "").strip()
        or str(manifest.get("updated_at") or "").strip()
    )


def _derive_stage(files: dict[str, str], manifest: dict) -> str:
    _drop_stale_downstream_files(files, _manifest_p6_input_hashes(manifest))
    intel_resume_stages = _missing_intel_resume_stages(manifest, files)
    if intel_resume_stages == ["p2"]:
        return "p2_resume"
    if _has_terminal_success(manifest, files):
        return "p9_done"
    if _needs_delivery_resume(manifest, files):
        return "delivery_resume"
    if _needs_roi_resume(manifest, files):
        return "roi_resume"
    if intel_resume_stages:
        return "intel_resume"
    if _needs_p6_resume(manifest, files):
        return "p6_resume"
    if files.get("p8"):
        return "p9_resume"
    if files.get("p7") or files.get("p7a"):
        return "p8_resume"
    if files.get("p6"):
        return "p6"
    if files.get("p5"):
        return "p5"
    if files.get("p1"):
        return "p1"
    return "none"


def detect_checkpoint() -> dict:
    day_dir = Path(get_today_output_dir())
    ckpt = {
        "stage": "none",
        "files": {},
        "p6_input_sha256": {},
        "p6_metrics": {},
        "p9_metrics": {},
        "intel_resume_stages": [],
        "run_timestamp": "",
    }

    manifest = load_run_manifest()
    manifest_is_authoritative = _manifest_has_recovery_state(manifest)
    recovery_manifest = manifest if manifest_is_authoritative else {}
    if manifest_is_authoritative:
        ckpt["run_timestamp"] = manifest.get("run_timestamp", "")
        ckpt["p6_input_sha256"] = _manifest_p6_input_hashes(manifest)
        p6_metrics = _current_stage_payload(manifest, "p6").get("metrics", {})
        ckpt["p6_metrics"] = dict(p6_metrics) if isinstance(p6_metrics, dict) else {}
        p9_metrics = _current_stage_payload(manifest, "p9").get("metrics", {})
        ckpt["p9_metrics"] = dict(p9_metrics) if isinstance(p9_metrics, dict) else {}
        _load_files_from_manifest(ckpt, manifest)
        ckpt["intel_resume_stages"] = _missing_intel_resume_stages(manifest, ckpt["files"])
        ckpt["stage"] = _derive_stage(ckpt["files"], manifest)
        if ckpt["stage"] == "p9_done":
            return ckpt

    _merge_files_from_fallback(
        ckpt,
        day_dir,
        run_timestamp=ckpt["run_timestamp"] if manifest_is_authoritative else "",
    )
    ckpt["intel_resume_stages"] = _missing_intel_resume_stages(
        recovery_manifest,
        ckpt["files"],
    )
    ckpt["stage"] = _derive_stage(ckpt["files"], recovery_manifest)

    if not ckpt["run_timestamp"]:
        for candidate in ckpt["files"].values():
            ckpt["run_timestamp"] = extract_timestamp_from_name(candidate)
            if ckpt["run_timestamp"]:
                break

    return ckpt
