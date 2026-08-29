# -*- coding: utf-8 -*-
"""Close the remaining behavioral branches in the LLM adapter and settings."""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import native_llm_adapter as adapter_module
import settings as settings_module


BASE_URL = "http://127.0.0.1:8317"
API_KEY = "test-key"  # pragma: allowlist secret
ORIGINAL_GET_TODAY_OUTPUT_DIR = settings_module.get_today_output_dir


def _make_adapter(model: str = "local-model", **kwargs: Any):
    return adapter_module.NativeLLMAdapter(
        model=model,
        base_url=kwargs.pop("base_url", BASE_URL),
        api_key=kwargs.pop("api_key", API_KEY),
        **kwargs,
    )


def _load_settings_variant(monkeypatch, name: str):
    spec = importlib.util.spec_from_file_location(name, settings_module.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_settings_import_without_dotenv_preserves_first_proxy_and_warns_for_missing_keys(
    monkeypatch,
    caplog,
):
    monkeypatch.setitem(sys.modules, "dotenv", None)
    monkeypatch.setenv("NO_PROXY", "example.test")
    monkeypatch.setenv("no_proxy", "example.test")
    monkeypatch.setenv("HTTP_PROXY", "http://first.proxy.test:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://second.proxy.test:8443")
    for name in (
        "ANTIGRAVITY_API_KEY",
        "GEMINI_SEARCH_API_KEYS",
        "GEMINI_SEARCH_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    with caplog.at_level("WARNING"):
        variant = _load_settings_variant(monkeypatch, "settings_without_dotenv_final")

    assert variant.SYSTEM_PROXY == "http://first.proxy.test:8080"
    assert variant.SEARCH_API_KEY_POOL == []
    assert {"127.0.0.1", "localhost", "::1"}.issubset(
        set(variant.MERGED_NO_PROXY.split(","))
    )
    assert "ANTIGRAVITY_API_KEY" in caplog.text
    assert "GEMINI_SEARCH_API_KEYS" in caplog.text


def test_settings_import_accepts_single_search_key(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "dotenv",
        SimpleNamespace(load_dotenv=lambda _path: False),
    )
    monkeypatch.delenv("GEMINI_SEARCH_API_KEYS", raising=False)
    monkeypatch.setenv("GEMINI_SEARCH_API_KEY", "single-search-key")  # pragma: allowlist secret

    variant = _load_settings_variant(monkeypatch, "settings_single_search_key_final")

    assert variant.SEARCH_API_KEY_POOL == ["single-search-key"]  # pragma: allowlist secret


def test_settings_tls_helpers_cover_duplicate_empty_and_parse_failure(monkeypatch):
    assert settings_module._parse_insecure_tls_allowlist(
        " Example.COM,example.com, ,api.example.com "
    ) == ("example.com", "api.example.com")

    monkeypatch.setattr(settings_module, "_PUBLIC_SUFFIX_LIST", None)
    assert settings_module._parse_insecure_tls_allowlist("example.com") == ()

    monkeypatch.setattr(
        settings_module,
        "urlparse",
        lambda _url: (_ for _ in ()).throw(ValueError("invalid URL")),
    )
    assert settings_module.is_insecure_tls_allowed("not-used") is False


def test_settings_tls_summary_covers_empty_and_populated_allowlist(monkeypatch):
    monkeypatch.setattr(settings_module, "INSECURE_TLS_ALLOWLIST", ())
    assert settings_module.get_tls_policy_summary() == "strict_default; allowlist=none"

    monkeypatch.setattr(
        settings_module,
        "INSECURE_TLS_ALLOWLIST",
        ("legacy.example.test", "old-api.example.test"),
    )
    assert settings_module.get_tls_policy_summary() == (
        "strict_default; allowlist=legacy.example.test,old-api.example.test"
    )


def test_settings_today_output_directory_is_created(monkeypatch, tmp_path: Path):
    class FixedDatetime:
        @staticmethod
        def now():
            return SimpleNamespace(strftime=lambda _format: "20260715")

    monkeypatch.setattr(settings_module, "datetime", FixedDatetime)
    monkeypatch.setattr(settings_module, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        settings_module,
        "get_today_output_dir",
        ORIGINAL_GET_TODAY_OUTPUT_DIR,
    )

    result = Path(settings_module.get_today_output_dir())

    assert result == tmp_path / "20260715"
    assert result.is_dir()


def test_settings_load_prompt_reads_once_then_uses_cache(monkeypatch):
    opened_paths: list[tuple[str, str, str]] = []

    def fake_open(path, mode, encoding):
        opened_paths.append((path, mode, encoding))
        return io.StringIO("prompt body")

    monkeypatch.setattr(settings_module, "open", fake_open, raising=False)
    monkeypatch.setattr(settings_module, "_prompt_cache", {})

    assert settings_module.load_prompt("covered-prompt.txt") == "prompt body"
    assert settings_module.load_prompt("covered-prompt.txt", required=True) == "prompt body"
    assert len(opened_paths) == 1
    assert opened_paths[0][1:] == ("r", "utf-8")
    assert Path(opened_paths[0][0]).parts[-2:] == ("prompts", "covered-prompt.txt")


@pytest.mark.parametrize(
    ("gemini_key", "search_keys", "expected_key", "expects_warning"),
    [
        ("dedicated-key", ["pooled-key"], "dedicated-key", False),  # pragma: allowlist secret
        ("", ["pooled-key"], "pooled-key", False),  # pragma: allowlist secret
        ("", [], "proxy-key", True),  # pragma: allowlist secret
    ],
)
def test_settings_gemini35_audit_key_priority(
    monkeypatch,
    caplog,
    gemini_key,
    search_keys,
    expected_key,
    expects_warning,
):
    captured: list[dict[str, Any]] = []

    class RecordingAdapter:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(adapter_module, "NativeLLMAdapter", RecordingAdapter)
    monkeypatch.setattr(settings_module, "_pro_adapter", None)
    monkeypatch.setattr(settings_module, "AUDIT_MODEL", "gemini-3.5-pro-preview")
    monkeypatch.setattr(settings_module, "GEMINI_PRO_API_KEY", gemini_key)
    monkeypatch.setattr(settings_module, "SEARCH_API_KEY_POOL", search_keys)
    monkeypatch.setattr(settings_module, "PRO_OPUS_API_KEY", "proxy-key")

    with caplog.at_level("WARNING"):
        adapter = settings_module._get_pro_adapter()

    assert adapter is settings_module._pro_adapter
    assert captured[0]["model"] == "gemini-3.5-pro-preview"
    assert captured[0]["api_key"] == expected_key
    assert ("GEMINI_PRO_API_KEY" in caplog.text) is expects_warning


def test_settings_audit_fallback_disabled_then_cached(monkeypatch):
    captured: list[dict[str, Any]] = []

    class RecordingAdapter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            captured.append(kwargs)

    monkeypatch.setattr(adapter_module, "NativeLLMAdapter", RecordingAdapter)
    monkeypatch.setattr(settings_module, "_audit_fallback_adapter", None)
    monkeypatch.setattr(settings_module, "AUDIT_FALLBACK_BASE_URL", "")
    monkeypatch.setattr(settings_module, "AUDIT_FALLBACK_API_KEY", "")
    assert settings_module.get_audit_fallback_adapter() is None

    monkeypatch.setattr(
        settings_module,
        "AUDIT_FALLBACK_BASE_URL",
        "https://backup.example.test/v1/messages/",
    )
    monkeypatch.setattr(settings_module, "AUDIT_FALLBACK_API_KEY", "backup-key")

    first = settings_module.get_audit_fallback_adapter()
    second = settings_module.get_audit_fallback_adapter()

    assert first is second
    assert len(captured) == 1
    assert captured[0]["base_url"] == "https://backup.example.test"
    assert captured[0]["api_key"] == "backup-key"  # pragma: allowlist secret
    assert captured[0]["max_retries"] == 2


def test_settings_normalizer_and_lazy_adapter_forwarding():
    assert settings_module._normalize_anthropic_base_url(
        "https://backup.example.test/api"
    ) == "https://backup.example.test/api"

    class CallableTarget:
        label = "ready"

        def __repr__(self):
            return "CallableTarget(ready)"

        def __call__(self, value, *, scale=1):
            return value * scale

    target = CallableTarget()
    lazy = settings_module._LazyAdapter(lambda: target)

    assert lazy.label == "ready"
    assert repr(lazy) == "CallableTarget(ready)"
    assert lazy(4, scale=3) == 12


def test_http_error_detail_handles_scalar_text_fallback_and_empty_payload():
    scalar_error = RuntimeError("request failed")
    scalar_error.response = SimpleNamespace(json=lambda: {"error": "plain provider detail"})
    scalar_result = adapter_module._with_http_error_detail(scalar_error)
    assert scalar_result is not scalar_error
    assert str(scalar_result).endswith("response_error=plain provider detail")

    text_error = RuntimeError("request failed")
    text_error.response = SimpleNamespace(
        json=lambda: (_ for _ in ()).throw(ValueError("not JSON")),
        text="fallback response text",
    )
    text_result = adapter_module._with_http_error_detail(text_error)
    assert str(text_result).endswith("response_error=fallback response text")

    empty_error = RuntimeError("request failed")
    empty_error.response = SimpleNamespace(json=lambda: {}, text="")
    assert adapter_module._with_http_error_detail(empty_error) is empty_error

    empty_scalar_error = RuntimeError("request failed")
    empty_scalar_error.response = SimpleNamespace(json=lambda: {"error": ""}, text="")
    assert (
        adapter_module._with_http_error_detail(empty_scalar_error)
        is empty_scalar_error
    )


def test_noneligible_primary_error_does_not_activate_model_fallback(monkeypatch):
    adapter = _make_adapter("gpt-5.6-sol", max_retries=1)

    def fail_with_oversized_input(self, *_args):
        self._set_last_error("input_too_large", "request payload rejected")
        return "", ""

    monkeypatch.setattr(
        adapter_module.NativeLLMAdapter,
        "_call_openai_compat",
        fail_with_oversized_input,
    )

    result = adapter.invoke("question")
    provenance = result.response_metadata["model_provenance"]

    assert result.content == ""
    assert provenance["fallback_used"] is False
    assert provenance["attempted_models"] == ["gpt-5.6-sol"]
    assert provenance["final_error_category"] == "input_too_large"


def test_external_fallback_handles_invalid_timeout_and_empty_response(monkeypatch):
    original_adapter_class = adapter_module.NativeLLMAdapter
    primary = _make_adapter(
        "gpt-5.6-sol",
        max_retries=1,
        timeout=37,
        fallback_model="claude-opus-4-6-thinking",
        fallback_base_url="https://backup.example.test",
        fallback_api_key="backup-key",  # pragma: allowlist secret
        fallback_request_timeout_seconds="not-a-number",
    )
    constructed: list[dict[str, Any]] = []
    received_messages: list[Any] = []

    def fail_primary(self, *_args):
        self._set_last_error("server_error", "HTTP 503")
        return "", ""

    class EmptyFallbackAdapter:
        provider = "anthropic"
        last_response_model = ""
        last_error_category = "server_error"
        last_error_message = "backup provider returned no text"

        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def invoke(self, messages):
            received_messages.append(messages)
            return SimpleNamespace(content="", thinking_content="")

    monkeypatch.setattr(original_adapter_class, "_call_openai_compat", fail_primary)
    monkeypatch.setattr(adapter_module, "NativeLLMAdapter", EmptyFallbackAdapter)

    result = primary.invoke(
        [("system", "system instruction"), ("human", "question")]
    )
    provenance = result.response_metadata["model_provenance"]

    assert constructed == [
        {
            "model": "claude-opus-4-6-thinking",
            "base_url": "https://backup.example.test",
            "api_key": "backup-key",  # pragma: allowlist secret
            "max_retries": 1,
            "timeout": 37.0,
        }
    ]
    assert received_messages == [
        [("system", "system instruction"), ("human", "question")]
    ]
    assert result.content == ""
    assert provenance["fallback_used"] is True
    assert provenance["fallback_endpoint_independent"] is True
    assert provenance["final_error_category"] == "server_error"


def test_deadline_helpers_and_string_failover_categories(monkeypatch):
    zero_budget = _make_adapter(total_timeout_seconds=0)
    assert zero_budget._new_call_deadline() is None

    monkeypatch.setattr(adapter_module.time, "monotonic", lambda: 10.0)
    assert zero_budget._bounded_request_timeout(9.0) == 0.0

    string_failover = _make_adapter(
        max_retries=2,
        failover_categories=" timeout, server_error ",
    )
    monkeypatch.setattr(
        adapter_module.time,
        "sleep",
        lambda _seconds: pytest.fail("immediate failover must not sleep"),
    )
    assert (
        string_failover._handle_provider_error(
            "provider",
            RuntimeError("request timeout"),
            0,
        )
        is False
    )
    assert string_failover.last_error_category == "timeout"


@pytest.mark.parametrize(
    ("provider", "expected_result"),
    [
        ("anthropic", ("", "")),
        ("gemini", ""),
        ("openai", ("", "")),
        ("responses", ("", "")),
    ],
)
@pytest.mark.parametrize("prior_error", [False, True])
def test_each_provider_stops_before_request_when_deadline_is_exhausted(
    monkeypatch,
    provider,
    expected_result,
    prior_error,
):
    unexpected_calls: list[str] = []

    if provider == "anthropic":
        adapter = _make_adapter("claude-basic", max_retries=1)
        object.__setattr__(
            adapter,
            "_anthropic_client",
            SimpleNamespace(
                messages=SimpleNamespace(
                    create=lambda **_kwargs: unexpected_calls.append("anthropic")
                )
            ),
        )
        invoke = lambda: adapter._call_anthropic("", "question", deadline=0.0)
    elif provider == "gemini":
        adapter = _make_adapter("gemini-basic", max_retries=1)
        object.__setattr__(
            adapter,
            "_gemini_client",
            SimpleNamespace(
                models=SimpleNamespace(
                    generate_content=lambda **_kwargs: unexpected_calls.append("gemini")
                )
            ),
        )
        invoke = lambda: adapter._call_gemini("question", deadline=0.0)
    elif provider == "openai":
        adapter = _make_adapter("local-model", max_retries=1)
        object.__setattr__(
            adapter,
            "_openai_client",
            SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(
                        create=lambda **_kwargs: unexpected_calls.append("openai")
                    )
                )
            ),
        )
        invoke = lambda: adapter._call_openai_compat("", "question", deadline=0.0)
    else:
        adapter = _make_adapter("local-model", max_retries=1)
        import requests

        monkeypatch.setattr(
            requests,
            "post",
            lambda *_args, **_kwargs: unexpected_calls.append("responses"),
        )
        invoke = lambda: adapter._call_openai_responses("", "question", deadline=0.0)

    if prior_error:
        adapter._set_last_error("prior_error", "keep existing error")

    assert invoke() == expected_result
    assert unexpected_calls == []
    if prior_error:
        assert adapter.last_error_category == "prior_error"
    else:
        assert adapter.last_error_category == "timeout_budget_exhausted"


def test_openai_compat_records_empty_http200_response():
    adapter = _make_adapter("local-model", max_retries=1)
    response = SimpleNamespace(
        model="served-model",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="", reasoning_content="")
            )
        ],
    )
    object.__setattr__(
        adapter,
        "_openai_client",
        SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_kwargs: response)
            )
        ),
    )

    assert adapter._call_openai_compat("", "question") == ("", "")
    assert adapter.last_response_model == "served-model"
    assert adapter.last_error_category == "empty_response"


class _ResponsesResult:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_incomplete_response_stops_when_retry_delay_exceeds_budget(monkeypatch):
    import requests

    calls: list[dict[str, Any]] = []
    payload = {
        "model": "served-model",
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output_text": "partial",
    }
    monkeypatch.setattr(
        requests,
        "post",
        lambda *_args, **kwargs: calls.append(kwargs) or _ResponsesResult(payload),
    )
    ticks = iter([100.0, 105.0])
    monkeypatch.setattr(adapter_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(adapter_module.random, "uniform", lambda *_args: 0.0)
    monkeypatch.setattr(
        adapter_module.time,
        "sleep",
        lambda _seconds: pytest.fail("insufficient budget must not sleep"),
    )
    adapter = _make_adapter("gpt-5.6-sol", max_retries=2)

    assert adapter._call_openai_responses("", "question", deadline=110.0) == (
        "",
        "",
    )
    assert len(calls) == 1
    assert calls[0]["timeout"] == 10.0
    assert adapter.last_error_category == "timeout_budget_exhausted"
    assert "incomplete response" in adapter.last_error_message


def test_incomplete_response_retries_when_budget_is_sufficient(monkeypatch):
    import requests

    payloads = iter(
        [
            {
                "model": "served-model",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output_text": "partial",
            },
            {
                "model": "served-model",
                "status": "completed",
                "output_text": "complete answer",
            },
        ]
    )
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        requests,
        "post",
        lambda *_args, **kwargs: calls.append(kwargs)
        or _ResponsesResult(next(payloads)),
    )
    ticks = iter([100.0, 101.0, 102.0])
    monkeypatch.setattr(adapter_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(adapter_module.random, "uniform", lambda *_args: 0.0)
    sleeps: list[float] = []
    monkeypatch.setattr(adapter_module.time, "sleep", sleeps.append)
    adapter = _make_adapter("gpt-5.6-sol", max_retries=2)

    assert adapter._call_openai_responses("", "question", deadline=120.0) == (
        "complete answer",
        "",
    )
    assert [call["timeout"] for call in calls] == [20.0, 18.0]
    assert sleeps == [15.0]
    assert adapter.last_error_category == ""
