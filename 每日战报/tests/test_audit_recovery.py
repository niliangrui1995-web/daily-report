"""Regression tests for P5/P7.5 audit recovery."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _StubAdapter:
    def __init__(self, response=None, error: Exception | None = None):
        self.model = "claude-sonnet-4-6"
        self.response = response
        self.error = error
        self.calls = []
        self.last_error_category = ""
        self.last_error_message = ""

    def invoke(self, prompt):
        self.calls.append(prompt)
        if self.error is not None:
            raise self.error
        return self.response


def test_audit_primary_success_does_not_use_fallback(monkeypatch):
    import audit_engine

    primary = _StubAdapter(SimpleNamespace(content="primary result", thinking_content=""))
    monkeypatch.setattr(audit_engine, "pro_adapter", primary)
    monkeypatch.setattr(
        audit_engine,
        "get_audit_fallback_adapter",
        lambda: (_ for _ in ()).throw(AssertionError("主接口成功时不应创建备用适配器")),
    )

    response, active_adapter, metrics = audit_engine._invoke_audit_with_fallback("prompt", "P5")

    assert response.content == "primary result"
    assert active_adapter is primary
    assert metrics["audit_fallback_used"] is False


def test_p5_and_p75_use_fallback_when_primary_raises(monkeypatch):
    import audit_engine

    primary = _StubAdapter(error=RuntimeError("429 rate limited"))
    fallback = _StubAdapter(SimpleNamespace(content="fallback result", thinking_content=""))
    monkeypatch.setattr(audit_engine, "pro_adapter", primary)
    monkeypatch.setattr(audit_engine, "get_audit_fallback_adapter", lambda: fallback)

    for layer_name in ("P5", "P7.5"):
        response, active_adapter, metrics = audit_engine._invoke_audit_with_fallback("prompt", layer_name)
        assert response.content == "fallback result"
        assert active_adapter is fallback
        assert metrics["audit_fallback_used"] is True
        assert "429 rate limited" in metrics["audit_primary_error"]


def test_p7a_uses_fallback_when_primary_response_is_too_short(monkeypatch):
    import audit_engine

    primary = _StubAdapter(SimpleNamespace(content="too short", thinking_content=""))
    fallback = _StubAdapter(SimpleNamespace(content="x" * 100, thinking_content=""))
    monkeypatch.setattr(audit_engine, "pro_adapter", primary)
    monkeypatch.setattr(audit_engine, "get_audit_fallback_adapter", lambda: fallback)

    response, active_adapter, metrics = audit_engine._invoke_audit_with_fallback(
        "prompt",
        "P7a",
        min_content_chars=100,
    )

    assert len(response.content) == 100
    assert active_adapter is fallback
    assert metrics["audit_fallback_used"] is True
    assert metrics["audit_primary_error"] == "empty_or_short_response"


def test_anthropic_messages_url_is_normalized_to_sdk_base_url():
    import settings

    assert settings._normalize_anthropic_base_url("https://cldapi.com/v1/messages") == "https://cldapi.com"
    assert settings._normalize_anthropic_base_url("https://cldapi.com/v1") == "https://cldapi.com"


def test_p5_audit_saves_invalid_raw_and_retries(monkeypatch):
    import audit_engine

    responses = [
        SimpleNamespace(content='{"summary":{"passed":1},"high_trust":["unterminated]', thinking_content=""),
        SimpleNamespace(
            content=(
                '{"summary":{"passed":1,"deleted":0},'
                '"high_trust":["1. [OK] fact"],'
                '"medium_trust":[],'
                '"deleted":[]}'
            ),
            thinking_content="",
        ),
    ]
    calls = []
    text_artifacts = []
    manifest_updates = []

    def fake_invoke(prompt):
        calls.append(prompt)
        return responses.pop(0)

    def fake_write_text_artifact(prefix, content, **kwargs):
        text_artifacts.append((prefix, content))
        return f"{prefix}.md"

    monkeypatch.setattr(audit_engine.pro_adapter, "invoke", fake_invoke)
    monkeypatch.setattr(audit_engine, "require_prompt", lambda name: "{raw_data}")
    monkeypatch.setattr(audit_engine, "write_text_artifact", fake_write_text_artifact)
    monkeypatch.setattr(audit_engine, "write_json_artifact", lambda prefix, data, **kwargs: f"{prefix}.json")
    monkeypatch.setattr(audit_engine, "update_run_manifest", lambda *args, **kwargs: manifest_updates.append((args, kwargs)))
    monkeypatch.setattr(audit_engine.time, "sleep", lambda seconds: None)

    result = audit_engine.gemini_pro_audit("raw input", audit_type="p5")

    assert len(calls) == 2
    assert "1. [OK] fact" in result
    assert any("_RAW_INVALID_1" in prefix for prefix, _ in text_artifacts)
    assert manifest_updates[-1][1]["status"] == "completed"
    assert manifest_updates[-1][1]["metrics"]["retry_count"] == 1


def test_p5_audit_repairs_unescaped_inner_quotes_without_retry(monkeypatch):
    import audit_engine

    raw_content = (
        '{"summary":{"passed":1,"deleted":0},'
        '"high_trust":["Samsung 劳资双方达成"2026年薪资协议暂定合约"；工会暂停罢工"],'
        '"medium_trust":[],"deleted":[]}'
    )
    calls = []
    manifest_updates = []

    def fake_invoke(prompt):
        calls.append(prompt)
        return SimpleNamespace(content=raw_content, thinking_content="")

    monkeypatch.setattr(audit_engine.pro_adapter, "invoke", fake_invoke)
    monkeypatch.setattr(audit_engine, "require_prompt", lambda name: "{raw_data}")
    monkeypatch.setattr(audit_engine, "write_text_artifact", lambda prefix, content, **kwargs: f"{prefix}.md")
    monkeypatch.setattr(audit_engine, "write_json_artifact", lambda prefix, data, **kwargs: f"{prefix}.json")
    monkeypatch.setattr(audit_engine, "update_run_manifest", lambda *args, **kwargs: manifest_updates.append((args, kwargs)))
    monkeypatch.setattr(audit_engine.time, "sleep", lambda seconds: None)

    result = audit_engine.gemini_pro_audit("raw input", audit_type="p5")

    assert len(calls) == 1
    assert 'Samsung 劳资双方达成"2026年薪资协议暂定合约"；工会暂停罢工' in result
    assert manifest_updates[-1][1]["status"] == "completed"
    assert manifest_updates[-1][1]["metrics"]["retry_count"] == 0


def test_p7a_marks_structured_degradation_when_json_parse_fails(monkeypatch):
    import audit_engine

    raw_content = (
        "not-json\n===MARKDOWN===\n## clean markdown\n"
        "P7a markdown is usable. " * 8
    )
    manifest_updates = []
    json_artifacts = []
    text_artifacts = []

    monkeypatch.setattr(
        audit_engine.pro_adapter,
        "invoke",
        lambda prompt: SimpleNamespace(content=raw_content, thinking_content=""),
    )
    monkeypatch.setattr(audit_engine, "require_prompt", lambda name: "{raw_deep_research}")

    def fake_write_json_artifact(prefix, data, **kwargs):
        json_artifacts.append((prefix, data))
        return f"{prefix}.json"

    def fake_write_text_artifact(prefix, content, **kwargs):
        text_artifacts.append((prefix, content))
        return f"{prefix}.md"

    monkeypatch.setattr(audit_engine, "write_json_artifact", fake_write_json_artifact)
    monkeypatch.setattr(audit_engine, "write_text_artifact", fake_write_text_artifact)
    monkeypatch.setattr(audit_engine, "update_run_manifest", lambda *args, **kwargs: manifest_updates.append((args, kwargs)))

    result = audit_engine.p7a_deep_research_audit("deep research input " * 20)

    assert result.startswith("## clean markdown\nP7a markdown is usable.")
    assert json_artifacts[-1][1]["raw_json_attempt"] == "not-json"
    assert text_artifacts[-1][0] == "P7a_深研审计"
    assert manifest_updates[-1][0] == ("p7a",)
    assert manifest_updates[-1][1]["status"] == "completed_structured_degraded"
    assert manifest_updates[-1][1]["metrics"]["structured_degraded"] is True
    assert manifest_updates[-1][1]["metrics"]["degrade_reason"].startswith("json_parse_failed:")
    assert manifest_updates[-1][1]["metrics"]["raw_json_attempt_chars"] == len("not-json")


def test_p7a_canonicalizes_known_alias_in_json_and_markdown(monkeypatch):
    import audit_engine

    raw_content = (
        '{"topics":['
        '{"title":"宏和电子/国际复材供给弹性","beneficiaries":["宏和电子","国际复材"]},'
        '{"title":"中国巨石供给弹性","beneficiaries":["中国巨石"]}'
        ']}'
        "\n===MARKDOWN===\n"
        "- 宏和电子（002011）已实现量产；国际复材认证有效。\n"
        "- 中国巨石（600176）供给弹性明确。\n"
    )
    json_artifacts = []
    manifest_updates = []

    monkeypatch.setattr(
        audit_engine.pro_adapter,
        "invoke",
        lambda _prompt: SimpleNamespace(content=raw_content, thinking_content=""),
    )
    monkeypatch.setattr(audit_engine, "require_prompt", lambda _name: "{raw_deep_research}")
    monkeypatch.setattr(
        audit_engine,
        "write_json_artifact",
        lambda _prefix, data, **_kwargs: json_artifacts.append(data) or "p7a.json",
    )
    monkeypatch.setattr(audit_engine, "write_text_artifact", lambda *args, **kwargs: "p7a.md")
    monkeypatch.setattr(
        audit_engine,
        "update_run_manifest",
        lambda *args, **kwargs: manifest_updates.append((args, kwargs)),
    )

    result = audit_engine.p7a_deep_research_audit(
        "P7 research input " * 20,
        rejected_target_names=["宏和电子"],
    )

    assert "宏和电子" not in result
    assert "宏和科技（603256）已实现量产" in result
    assert "中国巨石" in result
    assert "国际复材认证有效" in result
    assert "宏和电子" not in str(json_artifacts[-1])
    assert "宏和科技" in str(json_artifacts[-1])
    assert "国际复材" in str(json_artifacts[-1])
    assert manifest_updates[-1][1]["metrics"]["rejected_identity_items_removed"] == 0
