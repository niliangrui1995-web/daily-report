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
DELIVERY_RECOVERABLE_STATUSES = frozenset({"completed_email_failed", "failed_delivery"})
P6_HASHED_RESEARCH_STAGES = ("p7", "p7a", "p75")


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


def _file_mtime(files: dict[str, str], file_key: str) -> float:
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


def _is_older_than_any(files: dict[str, str], file_key: str, upstream_keys: tuple[str, ...]) -> bool:
    target_mtime = _file_mtime(files, file_key)
    if not target_mtime:
        return False
    return any(_file_mtime(files, upstream_key) > target_mtime for upstream_key in upstream_keys)


def _manifest_p6_input_hashes(manifest: dict) -> dict[str, str]:
    stages = manifest.get("stages", {})
    if not isinstance(stages, dict):
        return {}
    hashes = {}
    for stage_name in P6_HASHED_RESEARCH_STAGES:
        payload = stages.get(stage_name, {})
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
    if not files.get(stage_name):
        return False
    expected = str(p6_input_hashes.get(stage_name) or "").strip().lower()
    actual = compute_file_sha256(files.get("p6", ""))
    return not expected or not actual or expected != actual


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
        or _is_older_than_any(files, "p7a", ("p7",))
    )
    p6_dependency_stale = p7_hash_stale or p75_hash_stale or p7a_hash_stale

    for file_key, stale in (("p7", p7_stale), ("p75", p75_stale), ("p7a", p7a_stale)):
        if stale:
            files.pop(file_key, None)

    if p6_dependency_stale or (files.get("p8") and _is_older_than_any(files, "p8", ("p6",))):
        files.pop("p8", None)

    if p6_dependency_stale or (
        files.get("war_report")
        and _is_older_than_any(
            files,
            "war_report",
            ("p8", "p75", "p7a", "p7", "p6", "p5", "p4", "p3", "p2"),
        )
    ):
        files.pop("war_report", None)


def _stage_status(manifest: dict, stage_name: str) -> str:
    return str(manifest.get("stages", {}).get(stage_name, {}).get("status", ""))


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
    p9 = manifest.get("stages", {}).get("p9", {})
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


def _load_files_from_manifest(ckpt: dict, manifest: dict) -> None:
    stages = manifest.get("stages", {})

    def _store(
        stage_name: str,
        file_key: str,
        *preferred_keys: str,
        require_nonempty: bool = False,
    ) -> None:
        payload = stages.get(stage_name, {})
        files = payload.get("files", {})
        for preferred in preferred_keys:
            existing = _pick_existing_file(files.get(preferred, ""), require_nonempty=require_nonempty)
            if existing:
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


def _merge_files_from_fallback(ckpt: dict, day_dir: Path) -> None:
    for file_key, patterns in FALLBACK_PATTERNS.items():
        if file_key in ckpt["files"]:
            continue
        matched_path = ""
        for pattern in patterns:
            matches = sorted(day_dir.rglob(pattern), reverse=True)
            for match in matches:
                candidate = _pick_existing_file(str(match), require_nonempty=file_key == "war_report")
                if candidate:
                    matched_path = candidate
                    break
            if matched_path:
                break
        if matched_path:
            ckpt["files"][file_key] = matched_path


def _derive_stage(files: dict[str, str], manifest: dict) -> str:
    _drop_stale_downstream_files(files, _manifest_p6_input_hashes(manifest))
    if _has_terminal_success(manifest, files):
        return "p9_done"
    if _needs_delivery_resume(manifest, files):
        return "delivery_resume"
    if _needs_roi_resume(manifest, files):
        return "roi_resume"
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
        "run_timestamp": "",
    }

    manifest = load_run_manifest()
    if manifest:
        ckpt["run_timestamp"] = manifest.get("run_timestamp", "")
        ckpt["p6_input_sha256"] = _manifest_p6_input_hashes(manifest)
        _load_files_from_manifest(ckpt, manifest)
        ckpt["stage"] = _derive_stage(ckpt["files"], manifest)
        if ckpt["stage"] == "p9_done":
            return ckpt

    _merge_files_from_fallback(ckpt, day_dir)
    ckpt["stage"] = _derive_stage(ckpt["files"], manifest)

    if not ckpt["run_timestamp"]:
        for candidate in ckpt["files"].values():
            ckpt["run_timestamp"] = extract_timestamp_from_name(candidate)
            if ckpt["run_timestamp"]:
                break

    return ckpt
