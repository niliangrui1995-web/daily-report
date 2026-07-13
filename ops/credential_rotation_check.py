from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
from pathlib import Path
from typing import BinaryIO

from dotenv import dotenv_values


_SECRET_NAMES = (
    "AUDIT_FALLBACK_API_KEY",
    "ANTIGRAVITY_API_KEY",
    "EMAIL_PASSWORD",
    "GEMINI_PRO_API_KEY",
    "GEMINI_SEARCH_API_KEYS",
    "GEMINI_SEARCH_API_KEY",
    "GROK_API_KEY",
    "PRO_OPUS_API_KEY",
    "TAVILY_API_KEY",
)
_GIT_TIMEOUT_SECONDS = 60
_MAX_HISTORY_BLOB_BYTES = 512 * 1024 * 1024
_GIT_REDIRECT_ENV = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
}


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in list(env):
        if name in _GIT_REDIRECT_ENV or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(name, None)
    env.update(
        {
            "GCM_INTERACTIVE": "Never",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _assert_complete_history_view(repo_root: Path) -> None:
    top_level = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo_root,
        env=_git_env(),
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        timeout=15,
    ).strip()
    if Path(top_level).resolve() != repo_root.resolve():
        raise RuntimeError("credential rotation check resolved a different Git repository")
    shallow = subprocess.check_output(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=repo_root,
        env=_git_env(),
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="ascii",
        timeout=15,
    ).strip()
    if shallow != "false":
        raise RuntimeError("credential rotation check requires a non-shallow repository")
    git_dir_raw = subprocess.check_output(
        ["git", "rev-parse", "--git-dir"],
        cwd=repo_root,
        env=_git_env(),
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        timeout=15,
    ).strip()
    git_dir = Path(git_dir_raw)
    if not git_dir.is_absolute():
        git_dir = repo_root / git_dir
    grafts = git_dir / "info" / "grafts"
    if grafts.is_file() and grafts.stat().st_size:
        raise RuntimeError("credential rotation check refuses repositories with grafts")
    promisor = subprocess.run(
        ["git", "config", "--local", "--get-regexp", r"^(extensions\.partialclone|remote\..*\.promisor)$"],
        cwd=repo_root,
        env=_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )
    if promisor.returncode not in {0, 1}:
        raise RuntimeError("credential rotation check could not inspect partial-clone settings")
    if promisor.stdout.strip():
        raise RuntimeError("credential rotation check refuses partial/promisor repositories")


def _current_secrets(env_path: Path) -> tuple[dict[bytes, set[str]], set[str]]:
    values = dotenv_values(env_path)
    secrets: dict[bytes, set[str]] = {}
    weak_names: set[str] = set()
    for name in _SECRET_NAMES:
        raw = str(values.get(name) or "").strip()
        candidates = raw.split(",") if name == "GEMINI_SEARCH_API_KEYS" else [raw]
        for candidate in candidates:
            value = candidate.strip()
            if not value or value.casefold().startswith("your-"):
                continue
            if len(value) < 8:
                weak_names.add(name)
                continue
            secrets.setdefault(value.encode("utf-8"), set()).add(name)
    return secrets, weak_names


def _reachable_blob_ids(repo_root: Path) -> list[str]:
    _assert_complete_history_view(repo_root)
    objects = subprocess.check_output(
        ["git", "rev-list", "--objects", "--all"],
        cwd=repo_root,
        env=_git_env(),
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    object_ids = list(dict.fromkeys(line.split(" ", 1)[0] for line in objects.splitlines() if line))
    if not object_ids:
        raise RuntimeError("credential rotation check found no reachable Git objects")
    checked = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
        cwd=repo_root,
        env=_git_env(),
        input="".join(f"{object_id}\n" for object_id in object_ids),
        text=True,
        encoding="ascii",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    ).stdout
    lines = checked.splitlines()
    if len(lines) != len(object_ids):
        raise RuntimeError("git cat-file batch-check returned an incomplete object list")
    blob_ids: list[str] = []
    total_blob_bytes = 0
    for expected_id, line in zip(object_ids, lines, strict=True):
        parts = line.split()
        if len(parts) != 3 or parts[0] != expected_id or parts[1] == "missing":
            raise RuntimeError("git cat-file reported a missing or invalid reachable object")
        try:
            size = int(parts[2])
        except ValueError:
            raise RuntimeError("git cat-file returned an invalid object size") from None
        if size < 0:
            raise RuntimeError("git cat-file returned a negative object size")
        if parts[1] == "blob":
            blob_ids.append(expected_id)
            total_blob_bytes += size
            if total_blob_bytes > _MAX_HISTORY_BLOB_BYTES:
                raise RuntimeError("reachable Git blob history exceeds the safe scan limit")
    return blob_ids


def _read_batch_blob(stdout: BinaryIO, expected_object_id: str) -> bytes:
    header = stdout.readline().decode("ascii", errors="strict").strip().split()
    if len(header) != 3 or header[0] != expected_object_id or header[1] != "blob":
        raise RuntimeError("git cat-file returned an invalid blob header")
    size = int(header[2])
    data = stdout.read(size)
    if len(data) != size or stdout.read(1) != b"\n":
        raise RuntimeError("git cat-file returned a truncated blob")
    return data


def _find_historical_matches(repo_root: Path, secrets: dict[bytes, set[str]]) -> set[bytes]:
    matches: set[bytes] = set()
    blob_ids = _reachable_blob_ids(repo_root)
    try:
        result = subprocess.run(
            ["git", "cat-file", "--batch"],
            cwd=repo_root,
            env=_git_env(),
            input="".join(f"{object_id}\n" for object_id in blob_ids).encode("ascii"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("git cat-file history scan timed out") from None
    stream = io.BytesIO(result.stdout)
    for object_id in blob_ids:
        data = _read_batch_blob(stream, object_id)
        for secret in secrets.keys() - matches:
            if secret in data:
                matches.add(secret)
    if stream.read(1):
        raise RuntimeError("git cat-file returned unexpected trailing output")
    return matches


def main() -> int:
    parser = argparse.ArgumentParser(description="Check that current runtime secrets differ from reachable Git history.")
    parser.add_argument("--project-root", type=Path, required=True)
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    secrets, weak_names = _current_secrets(project_root / "每日战报" / ".env")
    try:
        matches = _find_historical_matches(project_root, secrets) if secrets else set()
        check_error = ""
    except Exception as exc:
        matches = set()
        check_error = type(exc).__name__
    matched_names = sorted({name for secret in matches for name in secrets[secret]})
    rotated = bool(secrets) and not weak_names and not matches and not check_error
    result = {
        "checked_secret_count": len(secrets),
        "historical_match_count": len(matches),
        "historical_match_names": matched_names,
        "weak_configured_secret_count": len(weak_names),
        "weak_configured_secret_names": sorted(weak_names),
        "rotation_check_error": check_error,
        "all_current_secrets_rotated": rotated,
        "secrets_printed": False,
    }
    print(json.dumps(result))
    return 0 if rotated else 2


if __name__ == "__main__":
    raise SystemExit(main())
