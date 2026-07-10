from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
import requests

from ops import credential_rotation_check, maintenance_lock, scheduled_task_canary


class _FakeResponse:
    def __init__(
        self,
        payload: dict[str, object],
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class _FakeSession:
    def __init__(self, inference_payload: dict[str, object]) -> None:
        self.trust_env = True
        self.inference_payload = inference_payload
        self.posts: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def get(self, *_args: object, **_kwargs: object) -> _FakeResponse:
        return _FakeResponse({"data": [{"id": "model"}]})

    def post(self, url: str, **kwargs: object) -> _FakeResponse:
        self.posts.append((url, kwargs["json"]))
        return _FakeResponse(self.inference_payload)

    def close(self) -> None:
        self.closed = True


def test_proxy_canary_requires_real_inference_text(monkeypatch: pytest.MonkeyPatch) -> None:
    nonce = "VCP_CANARY_0123456789abcdef"
    session = _FakeSession(
        {
            "id": "response-id",
            "status": "completed",
            "output": [{"content": [{"type": "output_text", "text": nonce}]}],
        }
    )
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setattr(scheduled_task_canary.secrets, "token_hex", lambda _length: "0123456789abcdef")

    detail = scheduled_task_canary._probe_proxy(
        "http://127.0.0.1:8317/v1",
        "local-test-key",
        "gpt-5.6-sol",
    )

    assert "inference_model=gpt-5.6-sol" in detail
    assert session.trust_env is False
    assert session.closed is True
    assert session.posts[0][0].endswith("/responses")
    assert session.posts[0][1]["model"] == "gpt-5.6-sol"
    assert session.posts[0][1]["reasoning"] == {"effort": "low"}
    assert session.posts[0][1]["max_output_tokens"] == 128


def test_proxy_canary_rejects_empty_inference(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession({"id": "response-id", "output": []})
    monkeypatch.setattr(requests, "Session", lambda: session)

    with pytest.raises(RuntimeError, match="invalid payload"):
        scheduled_task_canary._probe_proxy(
            "http://127.0.0.1:8317/v1",
            "local-test-key",
            "gpt-5.6-sol",
        )

    assert session.closed is True


def test_proxy_canary_rejects_failed_inference_status(monkeypatch: pytest.MonkeyPatch) -> None:
    nonce = "VCP_CANARY_0123456789abcdef"
    session = _FakeSession({"id": "response-id", "status": "failed", "output_text": nonce})
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setattr(scheduled_task_canary.secrets, "token_hex", lambda _length: "0123456789abcdef")

    with pytest.raises(RuntimeError, match="invalid payload"):
        scheduled_task_canary._probe_proxy(
            "http://127.0.0.1:8317/v1",
            "local-test-key",
            "gpt-5.6-sol",
        )


def test_inference_probe_retries_transient_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        _FakeResponse({}, status_code=429, headers={"Retry-After": "0"}),
        _FakeResponse({"id": "response-id", "output_text": "OK"}),
    ]

    class RetrySession:
        def post(self, *_args: object, **_kwargs: object) -> _FakeResponse:
            return responses.pop(0)

    response = scheduled_task_canary._post_inference_with_retry(
        RetrySession(),
        "http://127.0.0.1:8317/v1/responses",
        {"Authorization": "Bearer local-test-key"},
        {"model": "gpt-5.6-sol", "input": "OK"},
    )

    assert response.status_code == 200
    assert responses == []


def test_canary_record_never_persists_exception_message(tmp_path: Path) -> None:
    sentinel = "CANARY_SECRET_SENTINEL"
    checks: list[dict[str, object]] = []

    def fail() -> None:
        raise ValueError(f"invalid header contains {sentinel}")

    scheduled_task_canary._record(checks, "secret_probe", fail)
    result_path = tmp_path / "canary.json"
    scheduled_task_canary._write_result(result_path, {"ok": False, "checks": checks})
    serialized = result_path.read_text(encoding="utf-8")

    assert sentinel not in serialized
    assert checks == [{"name": "secret_probe", "ok": False, "detail": "ValueError"}]


def test_rotation_check_scans_reachable_historical_blobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    secret = ("rotation-test-value-" + "1234567890").encode()
    tracked = tmp_path / "legacy.txt"
    tracked.write_bytes(b"historical=" + secret)
    subprocess.run(["git", "add", "legacy.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "legacy"], cwd=tmp_path, check=True)
    tracked.unlink()
    subprocess.run(["git", "add", "-u"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "remove"], cwd=tmp_path, check=True)
    original_root = subprocess.check_output(
        ["git", "rev-parse", "HEAD~1"],
        cwd=tmp_path,
        text=True,
    ).strip()
    clean_tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=tmp_path,
        text=True,
    ).strip()
    replacement = subprocess.check_output(
        ["git", "commit-tree", clean_tree],
        cwd=tmp_path,
        input="replacement\n",
        text=True,
    ).strip()
    subprocess.run(["git", "replace", original_root, replacement], cwd=tmp_path, check=True)
    clean_repo = tmp_path / "redirected-clean-repo"
    subprocess.run(["git", "init", "-q", clean_repo], check=True)
    monkeypatch.setenv("GIT_DIR", str(clean_repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(clean_repo))

    matches = credential_rotation_check._find_historical_matches(
        tmp_path,
        {secret: {"EMAIL_PASSWORD"}},
    )

    assert matches == {secret}


def test_current_secret_parser_deduplicates_values(tmp_path: Path) -> None:
    shared = "rotation-test-value-" + "1234567890"
    second = "rotation-test-value-" + "abcdefghij"
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            (
                f"EMAIL_PASSWORD={shared}",
                f"PRO_OPUS_API_KEY={shared}",
                f"GEMINI_SEARCH_API_KEYS={shared},{second},your-placeholder",
            )
        ),
        encoding="utf-8",
    )

    secrets, weak_names = credential_rotation_check._current_secrets(env_path)

    assert set(secrets) == {shared.encode(), second.encode()}
    assert weak_names == set()
    assert secrets[shared.encode()] == {
        "EMAIL_PASSWORD",
        "GEMINI_SEARCH_API_KEYS",
        "PRO_OPUS_API_KEY",
    }


def test_current_secret_parser_blocks_short_configured_secret(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("ANTIGRAVITY_API_KEY=abc\n", encoding="utf-8")

    secrets, weak_names = credential_rotation_check._current_secrets(env_path)

    assert secrets == {}
    assert weak_names == {"ANTIGRAVITY_API_KEY"}


def test_reachable_blob_scan_fails_closed_on_missing_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_id = "a" * 40
    (tmp_path / ".git" / "info").mkdir(parents=True)

    def fake_check_output(args: list[str], **_kwargs: object) -> str:
        command = " ".join(args)
        if "--show-toplevel" in command:
            return str(tmp_path)
        if "--is-shallow-repository" in command:
            return "false\n"
        if "--git-dir" in command:
            return ".git\n"
        if "rev-list" in command:
            return f"{object_id}\n"
        raise AssertionError(command)

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[1] == "config":
            return subprocess.CompletedProcess(args, 1, stdout="")
        if args[1] == "cat-file":
            return subprocess.CompletedProcess(args, 0, stdout=f"{object_id} missing\n")
        raise AssertionError(args)

    monkeypatch.setattr(credential_rotation_check.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(credential_rotation_check.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="missing or invalid"):
        credential_rotation_check._reachable_blob_ids(tmp_path)


def test_maintenance_ready_marker_is_removed_on_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pipeline_lock

    ready = tmp_path / "maintenance.ready"
    release = tmp_path / "maintenance.release"
    release.write_text("release", encoding="ascii")

    @contextmanager
    def fake_lock(**_kwargs: object):
        yield {"pid": 1, "command": "maintenance"}

    monkeypatch.setattr(pipeline_lock, "acquire_run_lock", fake_lock)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "maintenance_lock.py",
            "--project-root",
            str(tmp_path),
            "--ready-file",
            str(ready),
            "--release-file",
            str(release),
            "--timeout-seconds",
            "30",
        ],
    )

    assert maintenance_lock.main() == 0
    assert ready.exists() is False
