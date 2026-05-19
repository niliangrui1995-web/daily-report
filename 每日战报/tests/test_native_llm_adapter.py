# -*- coding: utf-8 -*-
"""Tests for provider error classification and audit failure telemetry."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestProviderErrorClassification:
    def test_permission_denied_is_not_misclassified_as_risk_control(self):
        from native_llm_adapter import _classify_provider_error

        info = _classify_provider_error(
            RuntimeError(
                '403 PERMISSION_DENIED: Cloud Code Private API has not been used in project automatic-ethos-1nkgc before or it is disabled.'
            )
        )

        assert info["category"] == "permission_denied"
        assert info["retryable"] is False

    def test_input_too_large_is_non_retryable(self):
        from native_llm_adapter import _classify_provider_error

        info = _classify_provider_error(
            RuntimeError("400 Request too large: maximum context length exceeded for this model")
        )

        assert info["category"] == "input_too_large"
        assert info["retryable"] is False

    def test_plain_403_still_maps_to_risk_control(self):
        from native_llm_adapter import _classify_provider_error

        info = _classify_provider_error(RuntimeError("HTTP 403 Forbidden"))

        assert info["category"] == "risk_control"
        assert info["retryable"] is True

    def test_proxy_eof_500_maps_to_network_error(self):
        from native_llm_adapter import _classify_provider_error

        info = _classify_provider_error(
            RuntimeError(
                """Error code: 500 - {'error': {'message': 'Post "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:generateContent": EOF', 'type': 'server_error', 'code': 'internal_server_error'}}"""
            )
        )

        assert info["category"] == "network_error"
        assert info["retryable"] is True

    def test_antigravity_v1_routes_gemini_31_pro_to_openai_compat(self):
        from native_llm_adapter import NativeLLMAdapter

        adapter = NativeLLMAdapter(
            model="gemini-3.1-pro-high",
            base_url="http://127.0.0.1:7861/antigravity/v1",
            api_key="pwd",
        )

        assert adapter.provider == "openai_compat"

    def test_antigravity_v1_routes_opus_46_to_openai_compat(self):
        from native_llm_adapter import NativeLLMAdapter

        adapter = NativeLLMAdapter(
            model="claude-opus-4-6-thinking",
            base_url="http://127.0.0.1:7861/antigravity/v1",
            api_key="pwd",
        )

        assert adapter.provider == "openai_compat"

    def test_antigravity_v1_routes_sonnet_46_to_openai_compat(self):
        from native_llm_adapter import NativeLLMAdapter

        adapter = NativeLLMAdapter(
            model="claude-sonnet-4-6",
            base_url="http://127.0.0.1:7861/antigravity/v1",
            api_key="pwd",
        )

        assert adapter.provider == "openai_compat"

    def test_standard_v1_routes_gemini_31_pro_to_openai_compat(self):
        from native_llm_adapter import NativeLLMAdapter

        adapter = NativeLLMAdapter(
            model="gemini-3.1-pro-high",
            base_url="http://127.0.0.1:8317/v1",
            api_key="pwd",
        )

        assert adapter.provider == "openai_compat"

    def test_standard_v1_routes_opus_46_to_openai_compat(self):
        from native_llm_adapter import NativeLLMAdapter

        adapter = NativeLLMAdapter(
            model="claude-opus-4-6-thinking",
            base_url="http://127.0.0.1:8317/v1",
            api_key="pwd",
        )

        assert adapter.provider == "openai_compat"

    def test_standard_v1_routes_sonnet_46_to_openai_compat(self):
        from native_llm_adapter import NativeLLMAdapter

        adapter = NativeLLMAdapter(
            model="claude-sonnet-4-6",
            base_url="http://127.0.0.1:8317/v1",
            api_key="pwd",
        )

        assert adapter.provider == "openai_compat"

    def test_standard_v1_does_not_reroute_gemini_25_flash(self):
        from native_llm_adapter import NativeLLMAdapter

        adapter = NativeLLMAdapter(
            model="gemini-2.5-flash",
            base_url="http://127.0.0.1:8317/v1",
            api_key="pwd",
        )

        assert adapter.provider == "google"

    def test_standard_v1_does_not_reroute_gemini_35_flash(self):
        from native_llm_adapter import NativeLLMAdapter

        adapter = NativeLLMAdapter(
            model="gemini-3.5-flash",
            base_url="http://127.0.0.1:8317/v1",
            api_key="pwd",
        )

        assert adapter.provider == "google"

    def test_gemini_35_flash_uses_official_client(self, monkeypatch):
        import types

        from native_llm_adapter import NativeLLMAdapter

        client_kwargs = {}
        generate_kwargs = {}

        class FakeConfig:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeHttpOptions:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeModels:
            def generate_content(self, **kwargs):
                generate_kwargs.update(kwargs)
                return SimpleNamespace(text="OK")

        class FakeClient:
            def __init__(self, **kwargs):
                client_kwargs.update(kwargs)
                self.models = FakeModels()

        fake_types = SimpleNamespace(GenerateContentConfig=FakeConfig, HttpOptions=FakeHttpOptions)
        fake_genai = SimpleNamespace(Client=FakeClient, types=fake_types)
        monkeypatch.setitem(sys.modules, "google", SimpleNamespace(genai=fake_genai))
        monkeypatch.setitem(sys.modules, "google.genai", fake_genai)

        adapter = NativeLLMAdapter(
            model="gemini-3.5-flash",
            base_url="http://127.0.0.1:8317/v1",
            api_key="gemini-key",
            max_retries=1,
        )

        assert adapter._call_gemini("ping") == "OK"
        assert client_kwargs == {"api_key": "gemini-key"}
        assert generate_kwargs["model"] == "gemini-3.5-flash"

    def test_openai_compat_preserves_reasoning_content(self, monkeypatch):
        import types

        from native_llm_adapter import NativeLLMAdapter

        fake_message = SimpleNamespace(content="OK", reasoning_content="trace")
        fake_response = SimpleNamespace(choices=[SimpleNamespace(message=fake_message)])
        client_kwargs = {}
        create_kwargs = {}

        class FakeOpenAI:
            def __init__(self, *args, **kwargs):
                client_kwargs.update(kwargs)
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(create=lambda **kwargs: create_kwargs.update(kwargs) or fake_response)
                )

        monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))

        adapter = NativeLLMAdapter(
            model="claude-opus-4-6-thinking",
            base_url="http://127.0.0.1:7861/antigravity/v1",
            api_key="pwd",
            max_retries=1,
        )

        result = adapter.invoke("只回复OK")

        assert result.content == "OK"
        assert result.thinking_content == "trace"
        assert client_kwargs["max_retries"] == 0
        assert "max_tokens" not in create_kwargs

    def test_openai_compat_omits_default_max_tokens_for_non_thinking(self, monkeypatch):
        import types

        from native_llm_adapter import NativeLLMAdapter

        monkeypatch.delenv("OPENAI_COMPAT_MAX_TOKENS", raising=False)
        fake_message = SimpleNamespace(content="OK", reasoning_content="")
        fake_response = SimpleNamespace(choices=[SimpleNamespace(message=fake_message)])
        create_kwargs = {}

        class FakeOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(create=lambda **kwargs: create_kwargs.update(kwargs) or fake_response)
                )

        monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))

        adapter = NativeLLMAdapter(
            model="claude-sonnet-4-6",
            base_url="http://127.0.0.1:8317/v1",
            api_key="pwd",
            max_retries=1,
        )

        result = adapter.invoke("只返回OK")

        assert result.content == "OK"
        assert "max_tokens" not in create_kwargs

    def test_openai_compat_honors_max_tokens_override(self, monkeypatch):
        import types

        from native_llm_adapter import NativeLLMAdapter

        monkeypatch.setenv("OPENAI_COMPAT_MAX_TOKENS", "32000")
        fake_message = SimpleNamespace(content="OK", reasoning_content="")
        fake_response = SimpleNamespace(choices=[SimpleNamespace(message=fake_message)])
        create_kwargs = {}

        class FakeOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(create=lambda **kwargs: create_kwargs.update(kwargs) or fake_response)
                )

        monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))

        adapter = NativeLLMAdapter(
            model="claude-sonnet-4-6",
            base_url="http://127.0.0.1:8317/v1",
            api_key="pwd",
            max_retries=1,
        )

        result = adapter.invoke("只返回OK")

        assert result.content == "OK"
        assert create_kwargs["max_tokens"] == 32000

    def test_grok_native_parses_responses_message_output_text(self, monkeypatch):
        import requests

        from native_llm_adapter import NativeLLMAdapter

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "Grok answer"},
                            ],
                        }
                    ]
                }

        monkeypatch.delenv("GROK_NATIVE_ENABLE_X_SEARCH", raising=False)
        monkeypatch.delenv("GROK_NATIVE_REASONING_EFFORT", raising=False)
        monkeypatch.delenv("GROK_NATIVE_MAX_OUTPUT_TOKENS", raising=False)
        monkeypatch.delenv("GROK_REASONING_EFFORT", raising=False)
        calls = []

        def fake_post(*args, **kwargs):
            calls.append(kwargs)
            return FakeResponse()

        monkeypatch.setattr(requests, "post", fake_post)

        adapter = NativeLLMAdapter(model="grok-4.3", api_key="secret", max_retries=1)

        assert adapter._call_grok_native("system", "user") == "Grok answer"
        assert calls[0]["json"]["model"] == "grok-4.3"
        assert calls[0]["json"]["reasoning"] == {"effort": "none"}
        assert calls[0]["json"]["max_output_tokens"] == 4096
        assert "tools" not in calls[0]["json"]

    def test_grok_native_x_search_is_opt_in(self, monkeypatch):
        import requests

        from native_llm_adapter import NativeLLMAdapter

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"output": ["ok"]}

        calls = []
        monkeypatch.setenv("GROK_NATIVE_ENABLE_X_SEARCH", "true")
        monkeypatch.setenv("GROK_NATIVE_REASONING_EFFORT", "off")
        monkeypatch.setenv("GROK_NATIVE_MAX_OUTPUT_TOKENS", "1024")
        monkeypatch.setattr(requests, "post", lambda *args, **kwargs: calls.append(kwargs) or FakeResponse())

        adapter = NativeLLMAdapter(model="grok-4.3", api_key="secret", max_retries=1)

        assert adapter._call_grok_native("", "user") == "ok"
        assert calls[0]["json"]["tools"] == [{"type": "x_search"}]
        assert calls[0]["json"]["max_output_tokens"] == 1024
        assert "reasoning" not in calls[0]["json"]

    def test_grok_native_permission_denied_stops_without_retry(self, monkeypatch):
        import requests

        import native_llm_adapter
        from native_llm_adapter import NativeLLMAdapter

        class PermissionDeniedResponse:
            def raise_for_status(self):
                raise requests.HTTPError(
                    "403 PERMISSION_DENIED: Cloud Code Private API has not been used in project test before or it is disabled."
                )

        calls = []
        sleeps = []
        monkeypatch.setattr(requests, "post", lambda *args, **kwargs: calls.append(kwargs) or PermissionDeniedResponse())
        monkeypatch.setattr(native_llm_adapter.time, "sleep", lambda seconds: sleeps.append(seconds))

        adapter = NativeLLMAdapter(model="grok-4.3", api_key="secret", max_retries=3)

        assert adapter._call_grok_native("", "user") == ""
        assert len(calls) == 1
        assert adapter.last_error_category == "permission_denied"
        assert sleeps == []


class TestAuditFailureTelemetry:
    def test_p5_empty_output_includes_last_error_details(self, monkeypatch):
        import audit_engine

        manifest_calls = []

        class DummyAdapter:
            last_error_category = "permission_denied"
            last_error_message = "Cloud Code Private API has not been used in project automatic-ethos-1nkgc before or it is disabled."

            def invoke(self, prompt):
                return SimpleNamespace(content="")

        monkeypatch.setattr(audit_engine, "require_prompt", lambda _: "{layer_name}\n{raw_data}")
        monkeypatch.setattr(audit_engine, "pro_adapter", DummyAdapter())
        monkeypatch.setattr(
            audit_engine,
            "update_run_manifest",
            lambda stage, **kwargs: manifest_calls.append((stage, kwargs)),
        )

        result = audit_engine.gemini_pro_audit("测试输入", audit_type="p5", backtrack="24h")

        assert result == ""
        assert manifest_calls
        stage_name, payload = manifest_calls[-1]
        assert stage_name == "p5"
        assert payload["status"] == "failed_empty_output"
        assert payload["metrics"]["failure_category"] == "permission_denied"
        assert "Cloud Code Private API" in payload["metrics"]["error"]
