from __future__ import annotations

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


def _pick_existing_file(path: str) -> str:
    return path if path and Path(path).exists() else ""


def _load_files_from_manifest(ckpt: dict, manifest: dict) -> None:
    stages = manifest.get("stages", {})

    def _store(stage_name: str, file_key: str, *preferred_keys: str) -> None:
        payload = stages.get(stage_name, {})
        files = payload.get("files", {})
        for preferred in preferred_keys:
            existing = _pick_existing_file(files.get(preferred, ""))
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
    _store("p9", "war_report", "md")


def _merge_files_from_fallback(ckpt: dict, day_dir: Path) -> None:
    for file_key, patterns in FALLBACK_PATTERNS.items():
        if file_key in ckpt["files"]:
            continue
        matched_path = ""
        for pattern in patterns:
            matches = sorted(day_dir.rglob(pattern), reverse=True)
            if matches:
                matched_path = str(matches[0])
                break
        if matched_path:
            ckpt["files"][file_key] = matched_path


def _derive_stage(files: dict[str, str]) -> str:
    if files.get("war_report"):
        return "p9_done"
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
        "run_timestamp": "",
    }

    manifest = load_run_manifest()
    if manifest:
        ckpt["run_timestamp"] = manifest.get("run_timestamp", "")
        _load_files_from_manifest(ckpt, manifest)
        ckpt["stage"] = _derive_stage(ckpt["files"])
        if ckpt["stage"] == "p9_done":
            return ckpt

    _merge_files_from_fallback(ckpt, day_dir)
    ckpt["stage"] = _derive_stage(ckpt["files"])

    if not ckpt["run_timestamp"]:
        for candidate in ckpt["files"].values():
            ckpt["run_timestamp"] = extract_timestamp_from_name(candidate)
            if ckpt["run_timestamp"]:
                break

    return ckpt
