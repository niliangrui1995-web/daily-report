# -*- coding: utf-8 -*-
"""Focused behavioral coverage for ``native_llm_adapter``.

The fakes in this module keep every provider call local while asserting the
request and response contracts that the adapter exposes to the pipeline.
"""

from __future__ import annotations

import importlib.util
import sys
from types import SimpleNamespace
from typing import Any

import pytest

import native_llm_adapter as adapter_module
from native_llm_adapter import FakeAIMessage, NativeLLMAdapter


BASE_URL = "http://127.0.0.1:8317"
API_KEY = "test-key"  # pragma: allowlist secret


def make_adapter(model: str = "local-model", **kwargs: Any) -> NativeLLMAdapter:
    return NativeLLMAdapter(
        model=model,
        base_url=kwargs.pop("base_url", BASE_URL),
        api_key=API_KEY,
        **kwargs,
    )


class FakeHTTPResponse:
    def __init__(self, payload: dict[str, Any], error: Exception | None = None):
        self.payload = payload
        self.error = error

    def raise_for_status(self) -> None:
        if self.error:
            raise self.error

    def json(self) -> dict[str, Any]:
        return self.payload


def test_url_validation_covers_parse_failure_and_endpoint_classes(monkeypatch):
    monkeypatch.setattr(adapter_module, "urlparse", lambda _value: (_ for _ in ()).throw(ValueError("bad")))
    assert adapter_module.validate_model_base_url("not-used") == (
        False,
        False,
        "invalid model endpoint URL",
    )

    monkeypatch.undo()
    assert adapter_module.validate_model_base_url("ftp://localhost/model") == (
        False,
        False,
        "invalid model endpoint URL",
    )
    assert adapter_module.validate_model_base_url("http://localhost:8317") == (True, True, "")
    assert adapter_module.validate_model_base_url("https://models.example.test") == (True, False, "")


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("503 RESOURCE_EXHAUSTED", "rate_limit"),
        ("request deadline exceeded", "timeout"),
        ("an unrecognised provider problem", "unknown_error"),
    ],
)
def test_error_classification_remaining_categories(message, category):
    result = adapter_module._classify_provider_error(RuntimeError(message))
    assert result == {"category": category, "retryable": True}


def test_model_routing_helpers_cover_negative_and_official_variants():
    assert not adapter_module._should_use_openai_compat_proxy("ordinary-model", BASE_URL + "/v1")
    assert adapter_module._should_use_openai_compat_proxy(
        "CLAUDE-OPUS-4-6", BASE_URL + "/ANTIGRAVITY/V1/"
    )
    assert not adapter_module._is_official_gemini_direct_model("claude-sonnet")
    assert not adapter_module._is_official_gemini_direct_model("gemini-3.1-pro-high")
    assert adapter_module._is_official_gemini_direct_model("gemini-custom-preview")
    assert adapter_module._is_official_gemini_direct_model("gemini-custom-latest")


@pytest.mark.parametrize(("value", "expected"), [("bad", None), ("0", None), ("-2", None), ("33", 33)])
def test_openai_max_tokens_environment_validation(monkeypatch, value, expected):
    monkeypatch.setenv("OPENAI_COMPAT_MAX_TOKENS", value)
    assert adapter_module._openai_compat_max_tokens("any-model") == expected


def test_environment_parsers_cover_defaults_invalid_and_precedence(monkeypatch):
    monkeypatch.delenv("FEATURE_FLAG", raising=False)
    assert adapter_module._env_flag("FEATURE_FLAG", default=True) is True
    monkeypatch.setenv("FEATURE_FLAG", "YES")
    assert adapter_module._env_flag("FEATURE_FLAG") is True
    monkeypatch.setenv("FEATURE_FLAG", "no")
    assert adapter_module._env_flag("FEATURE_FLAG", default=True) is False

    monkeypatch.delenv("COUNT", raising=False)
    assert adapter_module._env_positive_int("COUNT", 7) == 7
    monkeypatch.setenv("COUNT", "not-a-number")
    assert adapter_module._env_positive_int("COUNT", 7) == 7
    monkeypatch.setenv("COUNT", "-1")
    assert adapter_module._env_positive_int("COUNT", 7) == 7
    monkeypatch.setenv("COUNT", "9")
    assert adapter_module._env_positive_int("COUNT", 7) == 9

    monkeypatch.delenv("GROK_NATIVE_REASONING_EFFORT", raising=False)
    monkeypatch.setenv("GROK_REASONING_EFFORT", " HIGH ")
    assert adapter_module._grok_native_reasoning_effort() == "high"


def test_responses_payload_extracts_nested_text_and_reasoning():
    data = {
        "output_text": "top-level",
        "output": [
            "ignored non-dict",
            {
                "type": "reasoning",
                "summary": [{"text": "summary-a"}, "summary-b", ""],
                "content": {"content": "detail"},
                "text": {"unrecognised": "ignored"},
            },
            {"type": "message", "content": [{"text": "not duplicated"}]},
        ],
    }

    text, reasoning = adapter_module._extract_openai_responses_payload(data)

    assert text == "top-level"
    assert reasoning == "summary-a\nsummary-b\ndetail"

    text, reasoning = adapter_module._extract_openai_responses_payload(
        {"output": [{"type": "message", "content": ["answer", {"text": " two"}]}]}
    )
    assert text == "answer\n two"
    assert reasoning == ""
    assert adapter_module._openai_responses_incomplete_reason({"incomplete_details": "stopped"}) == "stopped"
    assert adapter_module._openai_responses_incomplete_reason({"incomplete_details": {}}) == "unknown"
    untouched = []
    adapter_module._collect_response_text(untouched, 123)
    assert untouched == []


def test_model_provenance_defaults_and_malformed_metadata_are_safe():
    no_metadata = adapter_module.get_model_provenance(SimpleNamespace(content=""), "requested")
    malformed_metadata = adapter_module.get_model_provenance(
        SimpleNamespace(content="ok", response_metadata="not-a-dict"), "requested"
    )
    malformed_provenance = adapter_module.get_model_provenance(
        SimpleNamespace(content="ok", response_metadata={"model_provenance": "bad"}), "requested"
    )
    normalized = adapter_module.get_model_provenance(
        SimpleNamespace(
            content="",
            response_metadata={
                "model_provenance": {
                    "requested_model": "requested",
                    "attempted_models": "bad",
                    "fallback_used": True,
                    "fallback_model": "backup",
                    "effective_model": "effective",
                }
            },
        ),
        "default",
    )

    assert no_metadata["attempted_models"] == ["requested"]
    assert no_metadata["effective_model"] == ""
    assert malformed_metadata["effective_model"] == "requested"
    assert malformed_provenance["response_model"] == "requested"
    assert normalized["attempted_models"] == ["requested", "backup"]
    assert normalized["response_model"] == "effective"

    already_complete = adapter_module.get_model_provenance(
        SimpleNamespace(
            content="ok",
            response_metadata={
                "model_provenance": {
                    "requested_model": "requested",
                    "attempted_models": ["requested", "", "backup"],
                    "fallback_used": True,
                    "fallback_model": "backup",
                    "response_model": "served",
                    "effective_model": "effective",
                }
            },
        )
    )
    assert already_complete["attempted_models"] == ["requested", "backup"]
    assert already_complete["effective_model"] == "effective"


def test_tool_schema_prompt_and_parser_cover_supported_shapes():
    class ArgsSchema:
        @staticmethod
        def schema():
            return {
                "properties": {
                    "query": {"type": "string", "description": "search phrase"},
                    "limit": {},
                }
            }

    tool = SimpleNamespace(name="search", description="Search", args_schema=ArgsSchema())
    prompt = adapter_module._build_tools_prompt([tool, {"name": "plain", "parameters": {}}])
    assert "**search**: Search" in prompt
    assert "`query` (string): search phrase" in prompt
    assert "`limit` (string):" in prompt
    assert adapter_module._build_tools_prompt([]) == ""
    assert adapter_module._format_tool_schema({"name": "raw"}) == {"name": "raw"}
    assert adapter_module._format_tool_schema(42) == {"name": "42", "description": ""}
    assert adapter_module._format_tool_schema(
        SimpleNamespace(name="no-schema", description="plain", args_schema=None)
    ) == {"name": "no-schema", "description": "plain"}

    broken_tool = SimpleNamespace(
        name="broken",
        description=None,
        args_schema=SimpleNamespace(schema=lambda: (_ for _ in ()).throw(RuntimeError("bad schema"))),
    )
    assert adapter_module._format_tool_schema(broken_tool) == {"name": "broken", "description": ""}

    parsed = adapter_module._parse_tool_calls(
        '```tool_call\n{"name":"first","args":{"x":1}}\n```\n'
        '```tool_call\n{"name":"second","arguments":{"y":2}}\n```'
    )
    assert [(item["name"], item["args"], item["type"]) for item in parsed] == [
        ("first", {"x": 1}, "tool_call"),
        ("second", {"y": 2}, "tool_call"),
    ]

    fallback = adapter_module._parse_tool_calls(
        '```tool_call\nnot json\n``` {"name":"fallback","arguments":{"ok":true}}'
    )
    assert fallback[0]["name"] == "fallback"
    assert fallback[0]["args"] == {"ok": True}
    assert adapter_module._parse_tool_calls(
        '{"name":"broken","arguments":{"value": nope}}'
    ) == []


def test_adapter_properties_provider_selection_binding_and_repr(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_BASE_URL", BASE_URL)
    monkeypatch.setenv("ANTIGRAVITY_API_KEY", API_KEY)
    anthropic = NativeLLMAdapter(model="claude-basic")
    google = make_adapter("gemini-basic")
    generic = make_adapter("ordinary")

    assert anthropic.provider == "anthropic"
    assert google.provider == "google"
    assert generic.provider == "openai_compat"
    assert anthropic.InputType is Any
    assert anthropic.OutputType is Any
    assert repr(anthropic) == "NativeLLMAdapter(model=claude-basic, provider=anthropic)"
    assert generic.bind(stop=["done"]) is generic
    assert generic.with_config(tags=["test"]) is generic

    bound = generic.bind_tools([{"name": "lookup", "description": "Lookup"}])
    assert bound is not generic
    assert bound.provider == generic.provider
    assert bound._bound_tools == [{"name": "lookup", "description": "Lookup"}]
    assert "lookup" in bound._tools_prompt


def test_message_parsing_accepts_langchain_tuples_dicts_and_unknown_values():
    adapter = make_adapter()
    messages = [
        ("system", "tuple system"),
        ("user", "tuple user"),
        ("assistant", "tuple assistant"),
        ("custom", "tuple custom"),
        SimpleNamespace(type="system", content="object system"),
        SimpleNamespace(type="human", content="object human"),
        SimpleNamespace(type="ai", content="ignored", tool_calls=[{"name": "x"}]),
        SimpleNamespace(type="ai", content="object assistant", tool_calls=[]),
        SimpleNamespace(type="ai", content="", tool_calls=[]),
        SimpleNamespace(type="tool", content="tool output", name="calc"),
        SimpleNamespace(type="tool", content="unknown tool output"),
        SimpleNamespace(type="custom", content=123),
        {"role": "system", "content": "dict system"},
        {"role": "tool", "content": "dict tool"},
        {"content": "dict user"},
        99,
    ]

    system, user = adapter._parse_messages(messages)

    assert system == "tuple system\nobject system\ndict system"
    assert "tuple user" in user
    assert "[Assistant]: tuple assistant" in user
    assert "[Assistant tool calls]:" in user
    assert "[Tool Result - calc]: tool output" in user
    assert "[Tool Result - unknown_tool]: unknown tool output" in user
    assert "[Tool Result - unknown_tool]: dict tool" in user
    assert user.endswith("99")
    assert adapter._parse_messages("plain") == ("", "plain")
    assert adapter._parse_messages(123) == ("", "123")


def test_message_parsing_handles_content_attribute_that_disappears():
    class VanishingContent:
        type = "custom"

        def __init__(self):
            self.reads = 0

        @property
        def content(self):
            self.reads += 1
            if self.reads == 1:
                return "present during dispatch"
            raise AttributeError("content disappeared")

        def __str__(self):
            return "vanished-message"

    adapter = make_adapter()
    assert adapter._parse_messages([VanishingContent()]) == ("", "vanished-message")


@pytest.mark.parametrize(
    ("provider", "method_name", "return_value", "expected_content", "expected_thinking"),
    [
        ("anthropic", "_call_anthropic", "anthropic text", "anthropic text", ""),
        ("anthropic", "_call_anthropic", ("answer", "thought"), "answer", "thought"),
        ("google", "_call_gemini", "gemini text", "gemini text", ""),
        ("openai_compat", "_call_openai_compat", "openai text", "openai text", ""),
    ],
)
def test_invoke_normalizes_provider_return_shapes(
    monkeypatch, provider, method_name, return_value, expected_content, expected_thinking
):
    adapter = make_adapter()
    object.__setattr__(adapter, "provider", provider)
    monkeypatch.setattr(NativeLLMAdapter, method_name, lambda *args: return_value)

    result = adapter.invoke([("system", "rules"), ("user", "question")], config={"tag": "ignored"})

    assert result.content == expected_content
    assert getattr(result, "thinking_content", "") == expected_thinking
    provenance = result.response_metadata["model_provenance"]
    assert provenance["effective_model"] == "local-model"
    assert provenance["adapter_provider"] == provider


def test_invoke_tool_call_returns_fake_message_without_langchain(monkeypatch):
    adapter = make_adapter().bind_tools([{"name": "lookup"}])
    monkeypatch.setattr(
        NativeLLMAdapter,
        "_call_openai_compat",
        lambda *_args: ('```tool_call\n{"name":"lookup","arguments":{"q":"x"}}\n```', ""),
    )
    monkeypatch.setattr(adapter_module, "HAS_LANGCHAIN", False)

    result = adapter.invoke("use tool")

    assert isinstance(result, FakeAIMessage)
    assert result.content == ""
    assert result.tool_calls[0]["name"] == "lookup"


def test_invoke_without_langchain_returns_fake_message(monkeypatch):
    adapter = make_adapter()
    monkeypatch.setattr(NativeLLMAdapter, "_call_openai_compat", lambda *_args: ("answer", "reason"))
    monkeypatch.setattr(adapter_module, "HAS_LANGCHAIN", False)

    result = adapter.invoke("question")

    assert isinstance(result, FakeAIMessage)
    assert result.content == "answer"
    assert result.thinking_content == "reason"


@pytest.mark.parametrize(
    ("error", "expected_category"),
    [
        (RuntimeError("HTTP 403"), "risk_control"),
        (RuntimeError("HTTP 429"), "rate_limit"),
        (RuntimeError("request timeout"), "timeout"),
        (RuntimeError("SSL connection failed"), "network_error"),
        (RuntimeError("mystery"), "unknown_error"),
    ],
)
def test_provider_error_handler_retries_and_sleeps(monkeypatch, error, expected_category):
    sleeps = []
    adapter = make_adapter(max_retries=3)
    monkeypatch.setattr(adapter_module.random, "uniform", lambda *_args: 0.5)
    monkeypatch.setattr(adapter_module.time, "sleep", sleeps.append)

    assert adapter._handle_provider_error("provider", error, 0, risk_hint="rotate") is True
    assert adapter.last_error_category == expected_category
    assert adapter.last_error_message == str(error)
    assert sleeps == [15.5]


def test_provider_error_handler_stops_non_retryable_and_final_attempt(monkeypatch):
    sleeps = []
    adapter = make_adapter(max_retries=2)
    monkeypatch.setattr(adapter_module.time, "sleep", sleeps.append)

    assert adapter._handle_provider_error("provider", RuntimeError("permission_denied"), 0) is False
    assert adapter._handle_provider_error("provider", RuntimeError("payload too large"), 0) is False
    assert adapter._handle_provider_error("provider", RuntimeError("HTTP 429"), 1) is False
    assert sleeps == []


def test_anthropic_client_initialization_and_thinking_contract(monkeypatch):
    constructor_calls = []
    request_calls = []
    response = SimpleNamespace(
        model="claude-server",
        content=[
            SimpleNamespace(type="thinking", thinking="step one"),
            SimpleNamespace(type="thinking", thinking="step two"),
            SimpleNamespace(type="text", text="final answer"),
        ],
    )

    class FakeAnthropic:
        def __init__(self, **kwargs):
            constructor_calls.append(kwargs)
            self.messages = SimpleNamespace(
                create=lambda **request: request_calls.append(request) or response
            )

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=FakeAnthropic))
    adapter = make_adapter("claude-opus-thinking", timeout=12, max_retries=1)

    text, thinking = adapter._call_anthropic("system rules", "question")

    assert text == "final answer"
    assert thinking == "step one\n\nstep two"
    assert adapter.last_response_model == "claude-server"
    assert constructor_calls == [
        {"api_key": API_KEY, "base_url": BASE_URL, "default_headers": adapter_module.FAKE_HEADERS}
    ]
    assert request_calls[0]["system"] == "system rules"
    assert request_calls[0]["messages"] == [{"role": "user", "content": "question"}]
    assert request_calls[0]["temperature"] == 1
    assert request_calls[0]["max_tokens"] == 128000


def test_anthropic_empty_blocks_and_retry_then_success(monkeypatch):
    calls = []
    sleeps = []
    responses = [RuntimeError("request timeout"), SimpleNamespace(content=[], model="")]

    def create(**kwargs):
        calls.append(kwargs)
        outcome = responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    adapter = make_adapter("claude-basic", max_retries=2)
    object.__setattr__(adapter, "_anthropic_client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    monkeypatch.setattr(adapter_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(adapter_module.random, "uniform", lambda *_args: 0)

    assert adapter._call_anthropic("", "question") == ("", "")
    assert len(calls) == 2
    assert "system" not in calls[0]
    assert calls[0]["max_tokens"] == 16000
    assert sleeps == [15]
    assert adapter.last_error_category == ""
    assert adapter.last_response_model == "claude-basic"


def test_anthropic_terminal_failure_records_category(monkeypatch):
    adapter = make_adapter("claude-basic", max_retries=1)
    failing = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("HTTP 503")))
    )
    object.__setattr__(adapter, "_anthropic_client", failing)
    monkeypatch.setattr(adapter_module.time, "sleep", lambda _seconds: pytest.fail("must not sleep"))

    assert adapter._call_anthropic("", "question") == ("", "")
    assert adapter.last_error_category == "rate_limit"


def _install_fake_google(monkeypatch, response, constructor_calls, generate_calls):
    class HttpOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class GenerateContentConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class GoogleSearch:
        pass

    class Tool:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    fake_types = SimpleNamespace(
        HttpOptions=HttpOptions,
        GenerateContentConfig=GenerateContentConfig,
        GoogleSearch=GoogleSearch,
        Tool=Tool,
    )

    class Client:
        def __init__(self, **kwargs):
            constructor_calls.append(kwargs)
            self.models = SimpleNamespace(
                generate_content=lambda **request: generate_calls.append(request) or response
            )

    fake_genai = SimpleNamespace(Client=Client, types=fake_types)
    import google

    monkeypatch.setattr(google, "genai", fake_genai, raising=False)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)


def test_gemini_official_client_search_tool_and_model_version(monkeypatch):
    constructor_calls = []
    generate_calls = []
    response = SimpleNamespace(text="answer", model_version="gemini-server", model="ignored")
    _install_fake_google(monkeypatch, response, constructor_calls, generate_calls)
    adapter = make_adapter("gemini-2.5-flash", timeout=0, max_retries=1)

    assert adapter._call_gemini("prompt") == "answer"
    assert adapter.last_response_model == "gemini-server"
    assert constructor_calls[0]["api_key"] == API_KEY
    assert constructor_calls[0]["http_options"].kwargs == {"timeout": 1}
    request = generate_calls[0]
    assert request["model"] == "gemini-2.5-flash"
    assert request["contents"] == "prompt"
    assert request["config"].temperature == 0.5
    assert len(request["config"].tools) == 1


def test_gemini_proxy_client_empty_text_and_model_fallback(monkeypatch):
    constructor_calls = []
    generate_calls = []
    response = SimpleNamespace(text=None, model_version="", model="")
    _install_fake_google(monkeypatch, response, constructor_calls, generate_calls)
    adapter = make_adapter("gemini-basic", timeout=2, max_retries=1)

    assert adapter._call_gemini("prompt") == ""
    assert adapter.last_response_model == "gemini-basic"
    assert constructor_calls[0]["http_options"].kwargs == {
        "base_url": BASE_URL,
        "timeout": 2000,
    }
    assert not hasattr(generate_calls[0]["config"], "tools")


def test_gemini_retry_then_success_and_terminal_failure(monkeypatch):
    class Models:
        def __init__(self):
            self.calls = 0

        def generate_content(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("connection reset")
            return SimpleNamespace(text="recovered", model_version="", model="served")

    sleeps = []
    models = Models()
    _install_fake_google(monkeypatch, SimpleNamespace(), [], [])
    adapter = make_adapter("gemini-basic", max_retries=2)
    object.__setattr__(adapter, "_gemini_client", SimpleNamespace(models=models))
    monkeypatch.setattr(adapter_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(adapter_module.random, "uniform", lambda *_args: 0)

    assert adapter._call_gemini("prompt") == "recovered"
    assert models.calls == 2
    assert sleeps == [15]
    assert adapter.last_response_model == "served"

    terminal = make_adapter("gemini-basic", max_retries=1)
    object.__setattr__(
        terminal,
        "_gemini_client",
        SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("deadline"))
            )
        ),
    )
    assert terminal._call_gemini("prompt") == ""
    assert terminal.last_error_category == "timeout"


def test_grok_native_flattens_all_supported_output_shapes(monkeypatch):
    import requests

    payload = {
        "model": "grok-served",
        "output": [
            7,
            {"text": "direct", "content": "content string"},
            {"content": 123},
            {
                "content": [
                    "list string",
                    {"text": "nested"},
                    {"other": "ignored"},
                    8,
                ]
            },
        ],
    }
    monkeypatch.setattr(requests, "post", lambda *_args, **_kwargs: FakeHTTPResponse(payload))
    adapter = make_adapter("grok-4", max_retries=1)

    assert adapter._call_grok_native("", "question") == (
        "direct\ncontent string\nlist string\nnested"
    )
    assert adapter.last_response_model == "grok-served"


def test_grok_native_retries_exception_and_handles_missing_output(monkeypatch):
    import requests

    calls = []
    sleeps = []

    def post(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("proxy connection failed")
        return FakeHTTPResponse({})

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(adapter_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(adapter_module.random, "uniform", lambda *_args: 0)
    adapter = make_adapter("grok-4", max_retries=2)

    assert adapter._call_grok_native("system", "question") == ""
    assert len(calls) == 2
    assert sleeps == [15]


def test_openai_compat_routes_grok_and_responses_api(monkeypatch):
    grok = make_adapter("grok-4")
    monkeypatch.setattr(NativeLLMAdapter, "_call_grok_native", lambda *_args: "grok answer")
    assert grok._call_openai_compat("system", "user") == ("grok answer", "")

    responses = make_adapter("local", use_responses_api=True)
    monkeypatch.setattr(
        NativeLLMAdapter, "_call_openai_responses", lambda *_args: ("response answer", "trace")
    )
    assert responses._call_openai_compat("system", "user") == ("response answer", "trace")


def test_openai_client_initialization_system_reasoning_and_retry(monkeypatch):
    constructor_calls = []
    request_calls = []
    outcomes = [
        RuntimeError("request timeout"),
        SimpleNamespace(
            model="served-model",
            choices=[SimpleNamespace(message=SimpleNamespace(content="answer", reasoning_content="trace"))],
        ),
    ]

    def create(**kwargs):
        request_calls.append(kwargs)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    class OpenAI:
        def __init__(self, **kwargs):
            constructor_calls.append(kwargs)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=OpenAI))
    monkeypatch.setattr(adapter_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(adapter_module.random, "uniform", lambda *_args: 0)
    monkeypatch.delenv("OPENAI_COMPAT_MAX_TOKENS", raising=False)
    adapter = make_adapter("local", max_retries=2, reasoning_effort="high")

    assert adapter._call_openai_compat("system", "question") == ("answer", "trace")
    assert constructor_calls == [
        {"api_key": API_KEY, "base_url": BASE_URL + "/v1", "max_retries": 0}
    ]
    assert request_calls[0]["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "question"},
    ]
    assert request_calls[0]["reasoning_effort"] == "high"
    assert adapter.last_response_model == "served-model"


def test_openai_responses_exception_retry_payload_and_error_reset(monkeypatch):
    import requests

    calls = []
    sleeps = []

    def post(*_args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("connection reset")
        return FakeHTTPResponse({"status": "completed", "model": "served", "output_text": "answer"})

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(adapter_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(adapter_module.random, "uniform", lambda *_args: 0)
    monkeypatch.setenv("OPENAI_COMPAT_MAX_TOKENS", "123")
    adapter = make_adapter("local", max_retries=2)
    adapter._set_last_error("old", "old error")

    assert adapter._call_openai_responses("instructions", "question") == ("answer", "")
    assert len(calls) == 2
    assert sleeps == [15]
    assert calls[0]["json"]["instructions"] == "instructions"
    assert calls[0]["json"]["max_output_tokens"] == 123
    assert "reasoning" not in calls[0]["json"]
    assert adapter.last_error_category == ""
    assert adapter.last_response_model == "served"


def test_openai_responses_terminal_exception_returns_empty(monkeypatch):
    import requests

    monkeypatch.setattr(
        requests,
        "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("HTTP 503")),
    )
    adapter = make_adapter("local", max_retries=1)

    assert adapter._call_openai_responses("", "question") == ("", "")
    assert adapter.last_error_category == "rate_limit"


@pytest.mark.parametrize("provider", ["anthropic", "google", "openai_compat"])
def test_fallback_dispatches_to_each_provider_and_accepts_string_results(monkeypatch, provider):
    adapter = make_adapter("gpt-5.6-sol", max_retries=1)
    object.__setattr__(adapter, "provider", provider)

    def fallback_call(self, *_args):
        if self.model == "gpt-5.6-sol":
            self._set_last_error("rate_limit", "HTTP 429")
            return ""
        self._reset_last_error()
        self.last_response_model = "backup-served"
        return "backup answer"

    method_name = {
        "anthropic": "_call_anthropic",
        "google": "_call_gemini",
        "openai_compat": "_call_openai_compat",
    }[provider]
    monkeypatch.setattr(NativeLLMAdapter, method_name, fallback_call)

    result = adapter.invoke("question")

    assert result.content == "backup answer"
    assert adapter.model == "gpt-5.6-sol"
    provenance = result.response_metadata["model_provenance"]
    assert provenance["fallback_used"] is True
    assert provenance["effective_model"] == "backup-served"


def test_anthropic_fallback_preserves_tuple_thinking(monkeypatch):
    adapter = make_adapter("gpt-5.6-sol", max_retries=1)
    object.__setattr__(adapter, "provider", "anthropic")

    def call(self, *_args):
        if self.model == "gpt-5.6-sol":
            self._set_last_error("rate_limit", "HTTP 429")
            return ""
        self._reset_last_error()
        return "backup answer", "backup thinking"

    monkeypatch.setattr(NativeLLMAdapter, "_call_anthropic", call)
    result = adapter.invoke("question")

    assert result.content == "backup answer"
    assert result.thinking_content == "backup thinking"


def test_zero_retry_budget_skips_all_provider_requests(monkeypatch):
    anthropic = make_adapter("claude-basic", max_retries=0)
    object.__setattr__(
        anthropic,
        "_anthropic_client",
        SimpleNamespace(
            messages=SimpleNamespace(create=lambda **_kwargs: pytest.fail("unexpected Anthropic request"))
        ),
    )
    assert anthropic._call_anthropic("", "question") == ("", "")

    _install_fake_google(monkeypatch, SimpleNamespace(), [], [])
    gemini = make_adapter("gemini-basic", max_retries=0)
    object.__setattr__(
        gemini,
        "_gemini_client",
        SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **_kwargs: pytest.fail("unexpected Gemini request")
            )
        ),
    )
    assert gemini._call_gemini("question") == ""

    grok = make_adapter("grok-4", max_retries=0)
    assert grok._call_grok_native("", "question") == ""

    openai = make_adapter("local", max_retries=0)
    object.__setattr__(
        openai,
        "_openai_client",
        SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_kwargs: pytest.fail("unexpected OpenAI request"))
            )
        ),
    )
    assert openai._call_openai_compat("", "question") == ("", "")

    responses = make_adapter("local", max_retries=0)
    assert responses._call_openai_responses("", "question") == ("", "")


def test_langchain_message_paths_with_minimal_compatible_fake(monkeypatch):
    class CompatibleMessage:
        def __init__(self, content, tool_calls=None, response_metadata=None):
            self.content = content
            self.tool_calls = tool_calls or []
            self.response_metadata = response_metadata or {}
            self.thinking_content = ""

    plain_adapter = make_adapter()
    tool_adapter = make_adapter().bind_tools([{"name": "lookup"}])
    monkeypatch.setattr(adapter_module, "HAS_LANGCHAIN", True)
    monkeypatch.setattr(adapter_module, "LCMessage", CompatibleMessage)
    monkeypatch.setattr(NativeLLMAdapter, "_call_openai_compat", lambda *_args: ("answer", "thought"))
    plain = plain_adapter.invoke("question")
    assert isinstance(plain, CompatibleMessage)
    assert plain.content == "answer"
    assert plain.thinking_content == "thought"

    monkeypatch.setattr(
        NativeLLMAdapter,
        "_call_openai_compat",
        lambda *_args: ('```tool_call\n{"name":"lookup","arguments":{}}\n```', ""),
    )
    tool_result = tool_adapter.invoke("question")
    assert isinstance(tool_result, CompatibleMessage)
    assert tool_result.content == ""
    assert tool_result.tool_calls[0]["name"] == "lookup"


def _load_adapter_variant(monkeypatch, name, replacements):
    for module_name, replacement in replacements.items():
        monkeypatch.setitem(sys.modules, module_name, replacement)
    spec = importlib.util.spec_from_file_location(name, adapter_module.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_module_import_fallbacks_without_optional_dependencies(monkeypatch):
    variant = _load_adapter_variant(
        monkeypatch,
        "native_llm_adapter_without_optional_dependencies",
        {
            "pydantic": None,
            "langchain_core": None,
            "langchain_core.runnables": None,
            "langchain_core.messages": None,
        },
    )

    assert variant.ConfigDict is None
    assert variant.HAS_LANGCHAIN is False
    assert variant.NativeLLMAdapter.Config.arbitrary_types_allowed is True
    instance = variant.NativeLLMAdapter(
        model="local",
        base_url=BASE_URL,
        api_key=API_KEY,
        max_retries=0,
    )
    assert instance.provider == "openai_compat"


def test_module_import_and_constructor_with_langchain_compatible_base(monkeypatch):
    class RunnableSerializable:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class AIMessage:
        pass

    langchain_package = SimpleNamespace()
    runnables = SimpleNamespace(RunnableSerializable=RunnableSerializable)
    messages = SimpleNamespace(AIMessage=AIMessage)
    variant = _load_adapter_variant(
        monkeypatch,
        "native_llm_adapter_with_langchain",
        {
            "langchain_core": langchain_package,
            "langchain_core.runnables": runnables,
            "langchain_core.messages": messages,
        },
    )

    instance = variant.NativeLLMAdapter(
        model="local",
        base_url=BASE_URL,
        api_key=API_KEY,
        max_retries=0,
    )
    assert variant.HAS_LANGCHAIN is True
    assert variant.LCMessage is AIMessage
    assert instance.model == "local"
    assert instance.extra_kwargs == {}
