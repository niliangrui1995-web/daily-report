from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import smtplib
import ssl
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


def _record(checks: list[dict[str, Any]], name: str, operation: Callable[[], Any]) -> None:
    try:
        detail = operation()
    except Exception as exc:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        detail = type(exc).__name__
        if isinstance(status_code, int):
            detail = f"{detail} status={status_code}"
        checks.append({"name": name, "ok": False, "detail": detail})
        return
    checks.append({"name": name, "ok": True, "detail": detail})


def _write_probe(directory: Path, prefix: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f".{prefix}-{os.getpid()}.tmp"
    path.write_text("ok", encoding="ascii")
    if path.read_text(encoding="ascii") != "ok":
        raise RuntimeError("probe content mismatch")
    path.unlink()
    return str(directory)


def _expect_write_denied(directory: Path) -> str:
    path = directory / f".vcp-deny-probe-{os.getpid()}.tmp"
    try:
        path.write_text("must-not-write", encoding="ascii")
    except PermissionError:
        return str(directory)
    else:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"write unexpectedly succeeded in {directory}")


def _response_texts(payload: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        texts.append(output_text.strip())
    output = payload.get("output")
    if not isinstance(output, list):
        return texts
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip():
                texts.append(part["text"].strip())
    return texts


def _post_inference_with_retry(
    session: Any,
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> Any:
    response = None
    for attempt in range(3):
        response = session.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=(5.0, 90.0),
        )
        if response.status_code not in {429, 502, 503, 504} or attempt == 2:
            return response
        retry_after = response.headers.get("Retry-After", "")
        try:
            delay = min(10.0, max(0.0, float(retry_after)))
        except ValueError:
            delay = float(2**attempt)
        time.sleep(delay)
    raise RuntimeError("proxy inference retry loop ended without a response")


def _probe_proxy(base_url: str, api_key: str, inference_model: str = "") -> str:
    if not base_url or not api_key:
        raise RuntimeError("proxy URL or API key is missing")

    import requests

    endpoint = f"{base_url.rstrip('/')}/models"
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=(3.0, 8.0),
        )
        response.raise_for_status()
        payload = response.json()
        models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(models, list) or not models:
            raise RuntimeError("proxy returned no models")
        if inference_model:
            nonce = f"VCP_CANARY_{secrets.token_hex(8)}"
            inference = _post_inference_with_retry(
                session,
                f"{base_url.rstrip('/')}/responses",
                {"Authorization": f"Bearer {api_key}"},
                {
                    "model": inference_model,
                    "input": f"Reply with exactly {nonce}.",
                    "reasoning": {"effort": "low"},
                    "max_output_tokens": 128,
                },
            )
            inference.raise_for_status()
            inference_payload = inference.json()
            response_status = inference_payload.get("status") if isinstance(inference_payload, dict) else None
            if (
                not isinstance(inference_payload, dict)
                or not inference_payload.get("id")
                or response_status not in {None, "completed"}
                or nonce not in _response_texts(inference_payload)
            ):
                raise RuntimeError("proxy inference returned an invalid payload")
    finally:
        session.close()

    suffix = f" inference_model={inference_model}" if inference_model else ""
    return f"authenticated models={len(models)}{suffix}"


def _probe_external_auth(search_api_key: str, grok_api_key: str) -> str:
    if not search_api_key or not grok_api_key:
        raise RuntimeError("Gemini or xAI API key is missing")

    import requests

    session = requests.Session()
    session.trust_env = False
    try:
        google = session.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": search_api_key},
            timeout=(5.0, 15.0),
        )
        google.raise_for_status()
        xai = session.get(
            "https://api.x.ai/v1/models",
            headers={"Authorization": f"Bearer {grok_api_key}"},
            timeout=(5.0, 15.0),
        )
        xai.raise_for_status()
    finally:
        session.close()
    return f"gemini={google.status_code} xai={xai.status_code}"


def _probe_smtp_login(address: str, password: str) -> str:
    if not address or not password:
        raise RuntimeError("SMTP credentials are missing")
    with smtplib.SMTP_SSL(
        "smtp.qq.com",
        465,
        timeout=30,
        context=ssl.create_default_context(),
    ) as server:
        server.login(address, password)
    return "TLS login succeeded; no message sent"


def _write_result(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the scheduled-task service account runtime.")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--expect-base-prefix", type=Path, required=True)
    parser.add_argument("--expected-user", default="")
    parser.add_argument("--result-path", type=Path, required=True)
    parser.add_argument("--tdx-root", type=Path, required=True)
    parser.add_argument("--probe-model", default="")
    parser.add_argument("--probe-external-auth", action="store_true")
    parser.add_argument("--probe-smtp-login", action="store_true")
    parser.add_argument("--allow-maintenance-lock", action="store_true")
    parser.add_argument("--expect-tdx-readonly", action="store_true")
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    app_dir = project_root / "每日战报"
    output_dir = project_root / "每日热点输出"
    sys.path.insert(0, str(app_dir))

    checks: list[dict[str, Any]] = []
    current_user = getpass.getuser()
    _record(
        checks,
        "identity",
        lambda: current_user
        if not args.expected_user or current_user.casefold() == args.expected_user.casefold()
        else (_ for _ in ()).throw(
            RuntimeError(f"expected {args.expected_user}, got {current_user}")
        ),
    )
    _record(
        checks,
        "base_python",
        lambda: sys.base_prefix
        if Path(sys.base_prefix).resolve() == args.expect_base_prefix.resolve()
        else (_ for _ in ()).throw(
            RuntimeError(f"expected {args.expect_base_prefix}, got {sys.base_prefix}")
        ),
    )
    _record(
        checks,
        "runtime_files",
        lambda: str(app_dir)
        if all(
            path.is_file()
            for path in (
                project_root / "run_daily.bat",
                app_dir / ".env",
                app_dir / "pipeline_cli.py",
            )
        )
        else (_ for _ in ()).throw(RuntimeError("required runtime file is unavailable")),
    )
    _record(checks, "temp_write", lambda: _write_probe(Path(tempfile.gettempdir()), "vcp-temp"))
    _record(checks, "output_write", lambda: _write_probe(output_dir, "vcp-output"))

    def tdx_probe() -> str:
        from tdx_stock_universe import load_tdx_stock_code_map

        stock_map = load_tdx_stock_code_map(args.tdx_root)
        if not stock_map:
            raise RuntimeError("TDX stock universe is empty")
        return f"symbols={len(stock_map)}"

    _record(checks, "tdx_read", tdx_probe)
    if args.expect_tdx_readonly:
        _record(checks, "tdx_write_denied", lambda: _expect_write_denied(args.tdx_root.resolve()))

    def preflight_probe() -> str:
        import pipeline_cli

        result = pipeline_cli._collect_preflight(
            strict=True,
            allow_maintenance_lock=args.allow_maintenance_lock,
        )
        summary = result.get("summary") if isinstance(result, dict) else None
        exit_code = int(summary.get("exit_code", 1)) if isinstance(summary, dict) else 1
        if not isinstance(summary, dict) or exit_code != 0:
            raise RuntimeError(f"strict preflight failed: {summary}")
        return f"checks={summary.get('total', 0)}"

    _record(checks, "strict_preflight", preflight_probe)

    def config_probe() -> str:
        from settings import (
            EMAIL_ADDRESS,
            EMAIL_PASSWORD,
            PRO_OPUS_API_KEY,
            PRO_OPUS_BASE_URL,
        )

        if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
            raise RuntimeError("SMTP credentials are missing")
        return _probe_proxy(PRO_OPUS_BASE_URL, PRO_OPUS_API_KEY, args.probe_model)

    _record(checks, "authenticated_proxy", config_probe)

    if args.probe_external_auth:
        def external_auth_probe() -> str:
            from settings import GROK_API_KEY, SEARCH_API_KEY_POOL

            return _probe_external_auth(SEARCH_API_KEY_POOL[0] if SEARCH_API_KEY_POOL else "", GROK_API_KEY)

        _record(checks, "external_api_auth", external_auth_probe)

    if args.probe_smtp_login:
        def smtp_probe() -> str:
            from settings import EMAIL_ADDRESS, EMAIL_PASSWORD

            return _probe_smtp_login(EMAIL_ADDRESS, EMAIL_PASSWORD)

        _record(checks, "smtp_login_no_send", smtp_probe)

    result = {
        "ok": all(item["ok"] for item in checks),
        "user": current_user,
        "python": sys.executable,
        "base_prefix": sys.base_prefix,
        "checks": checks,
    }
    _write_result(args.result_path, result)
    print(json.dumps({"ok": result["ok"], "checks": len(checks)}, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
