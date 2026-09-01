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


def _p7a_markdown_fixture(topic_names: list[str]) -> str:
    return "\n\n".join(
        (
            f"## 主题{index}：{name}\n\n"
            "### 核心发现\n"
            f"- {name} 的可核验事实。\n\n"
            "### 产业链影响推演\n"
            "- 上游到下游的传导路径。\n\n"
            "### 数据锚点\n"
            f"- {index} 个明确数据锚点。"
        )
        for index, name in enumerate(topic_names, start=1)
    )


def _render_p7a_prompt_template() -> str:
    from pathlib import Path

    prompt_template = (Path(__file__).resolve().parents[1] / "prompts" / "p7a_audit.txt").read_text(
        encoding="utf-8"
    )
    return prompt_template.format(
        today_str="2026年09月01日 北京时间09:00",
        vanguard_list="测试公司甲, 测试公司乙",
        raw_deep_research="P7 原始研究哨兵文本",
        # P7 evidence is prepended by audit_engine after formatting.  Keep the
        # existing formatter argument so this test tracks the caller contract.
        p7_evidence_boundary="P7 证据边界哨兵文本",
    )


def test_p7a_prompt_keeps_literal_json_example_braces_after_formatting():
    prompt = _render_p7a_prompt_template()

    assert "按下方“第二部分：Markdown 可读报告”的结构输出精炼报告" in prompt
    assert "JSON 部分的第一个非空字符必须是 `{`" in prompt
    assert "不得输出前言、解释文字或代码围栏" in prompt
    assert '\n{\n  "meta": {\n' in prompt
    assert '\n  ]\n}\n\n===MARKDOWN===' in prompt
    assert "（纯文本 Markdown，不要 JSON）" not in prompt


def test_p7a_prompt_formats_required_inputs_without_unresolved_placeholders():
    prompt = _render_p7a_prompt_template()

    assert "⚠️ 当前北京时间：2026年09月01日 北京时间09:00" in prompt
    assert "测试公司甲, 测试公司乙" in prompt
    assert "P7 原始研究哨兵文本" in prompt
    for placeholder in ("{today_str}", "{vanguard_list}", "{raw_deep_research}"):
        assert placeholder not in prompt


def test_p7a_reaches_model_invoke_after_real_template_formatting(monkeypatch):
    import audit_engine

    calls = []
    manifest_updates = []
    raw_research = "P7 原始研究内容。" * 30
    markdown = _p7a_markdown_fixture(["测试主题"])
    raw_content = (
        '{"meta":{"input_chars":300,"output_chars":200,"topics_count":1},'
        '"topics":[{"name":"测试主题"}]}'
        "\n===MARKDOWN===\n"
        + markdown
    )

    def fake_invoke(prompt, layer_name, *, min_content_chars):
        calls.append((prompt, layer_name, min_content_chars))
        return (
            SimpleNamespace(content=raw_content, thinking_content=""),
            SimpleNamespace(last_error_category="", last_error_message=""),
            {"audit_fallback_used": False},
        )

    monkeypatch.setattr(audit_engine, "_invoke_audit_with_fallback", fake_invoke)
    monkeypatch.setattr(audit_engine, "write_json_artifact", lambda *_args, **_kwargs: "p7a.json")
    monkeypatch.setattr(audit_engine, "write_text_artifact", lambda *_args, **_kwargs: "p7a.md")
    monkeypatch.setattr(
        audit_engine,
        "update_run_manifest",
        lambda *args, **kwargs: manifest_updates.append((args, kwargs)),
    )

    result = audit_engine.p7a_deep_research_audit(
        raw_research,
        p7_evidence_provenance={
            "primary_source_count": 1,
            "summary_source_count": 2,
            "source_url_count": 3,
        },
    )

    assert len(calls) == 1
    prompt, layer_name, min_content_chars = calls[0]
    assert layer_name == "P7a"
    assert min_content_chars == 100
    assert raw_research in prompt
    assert "【P7 证据层级（系统强制）】" in prompt
    assert '\n{\n  "meta": {\n' in prompt
    assert "## 主题1：测试主题" in result
    assert manifest_updates[-1][1]["status"] == "completed"


def test_p7a_builds_markdown_derived_summary_when_json_parse_fails(monkeypatch):
    import audit_engine

    topic_names = ["覆铜板涨价", "封装级液冷", "CPO 设备"]
    raw_content = (
        "not-json\n===MARKDOWN===\n" + _p7a_markdown_fixture(topic_names)
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

    assert result.startswith("> 【P7a结构化降级】")
    assert result.endswith(_p7a_markdown_fixture(topic_names))
    assert json_artifacts[-1][1]["raw_json_attempt"] == "not-json"
    assert json_artifacts[-1][1]["meta"]["structured_degraded"] is True
    assert json_artifacts[-1][1]["meta"]["structured_source"] == "markdown_derived"
    assert [item["name"] for item in json_artifacts[-1][1]["topics"]] == topic_names
    assert text_artifacts[-1][0] == "P7a_深研审计"
    assert manifest_updates[-1][0] == ("p7a",)
    assert manifest_updates[-1][1]["status"] == "completed_structured_degraded"
    assert manifest_updates[-1][1]["metrics"]["structured_degraded"] is True
    assert manifest_updates[-1][1]["metrics"]["degrade_reason"].startswith("json_parse_failed:")
    assert manifest_updates[-1][1]["metrics"]["data_state"] == "markdown_derived_degraded"
    assert manifest_updates[-1][1]["metrics"]["topics_count"] == 3
    assert manifest_updates[-1][1]["metrics"]["raw_json_attempt_chars"] == len("not-json")


def test_p7a_repairs_fenced_json_with_unescaped_inner_quotes(monkeypatch):
    import audit_engine

    topic_names = ["覆铜板涨价", "封装级液冷", "CPO 设备"]
    markdown = _p7a_markdown_fixture(topic_names)
    raw_content = (
        "I will provide the requested audit.\n\n"
        "```json\n"
        '{"meta":{"input_chars":1,"output_chars":1,"topics_count":3},"topics":['
        '{"name":"覆铜板涨价","core_findings":["铜箔涨价持续。"]},'
        '{"name":"封装级液冷","core_findings":["TSMC称"Direct-to-Silicon Liquid Cooling"可降低热阻。"]},'
        '{"name":"CPO 设备","core_findings":["设备订单仍待核验。"]}]}'
        "\n```\n===MARKDOWN===\n"
        + markdown
    )
    json_artifacts = []
    manifest_updates = []

    monkeypatch.setattr(audit_engine, "require_prompt", lambda _name: "{raw_deep_research}")
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *_args, **_kwargs: (
            SimpleNamespace(content=raw_content, thinking_content=""),
            SimpleNamespace(last_error_category="", last_error_message=""),
            {"audit_fallback_used": False},
        ),
    )
    monkeypatch.setattr(
        audit_engine,
        "write_json_artifact",
        lambda _prefix, data, **_kwargs: json_artifacts.append(data) or "p7a.json",
    )
    monkeypatch.setattr(audit_engine, "write_text_artifact", lambda *_args, **_kwargs: "p7a.md")
    monkeypatch.setattr(
        audit_engine,
        "update_run_manifest",
        lambda *args, **kwargs: manifest_updates.append((args, kwargs)),
    )

    result = audit_engine.p7a_deep_research_audit("deep research input " * 20)

    assert result == markdown
    assert '"Direct-to-Silicon Liquid Cooling"' in json_artifacts[-1]["topics"][1]["core_findings"][0]
    assert json_artifacts[-1]["meta"]["input_chars"] == len("deep research input " * 20)
    assert json_artifacts[-1]["meta"]["output_chars"] == len(markdown)
    assert json_artifacts[-1]["meta"]["topics_count"] == 3
    assert manifest_updates[-1][1]["status"] == "completed"
    assert manifest_updates[-1][1]["metrics"]["structured_repaired"] is True
    assert manifest_updates[-1][1]["metrics"]["topics_count"] == 3


def test_p7a_uses_markdown_summary_when_same_count_topics_disagree(monkeypatch):
    import audit_engine

    markdown = _p7a_markdown_fixture(["Markdown 主题"])
    raw_content = (
        '{"topics":[{"name":"JSON 不一致主题","core_findings":["待核验"]}]}'
        "\n===MARKDOWN===\n"
        + markdown
    )
    json_artifacts = []
    manifest_updates = []

    monkeypatch.setattr(audit_engine, "require_prompt", lambda _name: "{raw_deep_research}")
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *_args, **_kwargs: (
            SimpleNamespace(content=raw_content, thinking_content=""),
            SimpleNamespace(last_error_category="", last_error_message=""),
            {"audit_fallback_used": False},
        ),
    )
    monkeypatch.setattr(
        audit_engine,
        "write_json_artifact",
        lambda _prefix, data, **_kwargs: json_artifacts.append(data) or "p7a.json",
    )
    monkeypatch.setattr(audit_engine, "write_text_artifact", lambda *_args, **_kwargs: "p7a.md")
    monkeypatch.setattr(
        audit_engine,
        "update_run_manifest",
        lambda *args, **kwargs: manifest_updates.append((args, kwargs)),
    )

    result = audit_engine.p7a_deep_research_audit("deep research input " * 20)

    assert result.startswith("> 【P7a结构化降级】")
    assert result.endswith(markdown)
    assert [item["name"] for item in json_artifacts[-1]["topics"]] == ["Markdown 主题"]
    assert json_artifacts[-1]["meta"]["structured_source"] == "markdown_derived"
    assert manifest_updates[-1][1]["metrics"]["degrade_reason"].startswith("structured_topics_mismatch:")


def test_p7a_does_not_accept_empty_json_object_with_markdown_topics(monkeypatch):
    import audit_engine

    topic_names = ["覆铜板涨价", "封装级液冷", "CPO 设备"]
    markdown = _p7a_markdown_fixture(topic_names)
    json_artifacts = []
    manifest_updates = []

    monkeypatch.setattr(audit_engine, "require_prompt", lambda _name: "{raw_deep_research}")
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *_args, **_kwargs: (
            SimpleNamespace(content="{}\n===MARKDOWN===\n" + markdown, thinking_content=""),
            SimpleNamespace(last_error_category="", last_error_message=""),
            {"audit_fallback_used": False},
        ),
    )
    monkeypatch.setattr(
        audit_engine,
        "write_json_artifact",
        lambda _prefix, data, **_kwargs: json_artifacts.append(data) or "p7a.json",
    )
    monkeypatch.setattr(audit_engine, "write_text_artifact", lambda *_args, **_kwargs: "p7a.md")
    monkeypatch.setattr(
        audit_engine,
        "update_run_manifest",
        lambda *args, **kwargs: manifest_updates.append((args, kwargs)),
    )

    result = audit_engine.p7a_deep_research_audit("deep research input " * 20)

    assert result.startswith("> 【P7a结构化降级】")
    assert result.endswith(markdown)
    assert [item["name"] for item in json_artifacts[-1]["topics"]] == topic_names
    assert manifest_updates[-1][1]["status"] == "completed_structured_degraded"
    assert manifest_updates[-1][1]["metrics"]["topics_count"] == 3


def test_p7a_rejects_response_without_usable_json_or_markdown(monkeypatch):
    import audit_engine

    raw_input = "deep research input " * 20
    raw_content = "upstream transport transcript without audit structure " * 4
    json_artifacts = []
    text_artifacts = []
    manifest_updates = []

    monkeypatch.setattr(audit_engine, "require_prompt", lambda _name: "{raw_deep_research}")
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *_args, **_kwargs: (
            SimpleNamespace(content=raw_content, thinking_content=""),
            SimpleNamespace(last_error_category="", last_error_message=""),
            {"audit_fallback_used": False},
        ),
    )
    monkeypatch.setattr(
        audit_engine,
        "write_json_artifact",
        lambda *args, **_kwargs: json_artifacts.append(args) or "p7a.json",
    )
    monkeypatch.setattr(
        audit_engine,
        "write_text_artifact",
        lambda *args, **_kwargs: text_artifacts.append(args) or "p7a.md",
    )
    monkeypatch.setattr(
        audit_engine,
        "update_run_manifest",
        lambda *args, **kwargs: manifest_updates.append((args, kwargs)),
    )

    result = audit_engine.p7a_deep_research_audit(raw_input)

    assert result.startswith("> 【P7a结构化降级】")
    assert result.endswith(raw_input)
    assert json_artifacts == []
    assert text_artifacts == []
    assert manifest_updates[-1][1]["status"] == "failed_semantic_output"
    assert manifest_updates[-1][1]["metrics"]["data_state"] == "invalid"
    assert manifest_updates[-1][1]["metrics"]["semantic_error"] == "missing_usable_p7a_markdown"


def test_p7a_canonicalizes_known_alias_in_json_and_markdown(monkeypatch):
    import audit_engine

    raw_content = (
        '{"topics":['
        '{"title":"宏和电子/国际复材供给弹性","beneficiaries":["宏和电子","国际复材"]},'
        '{"title":"中国巨石供给弹性","beneficiaries":["中国巨石"]}'
        ']}'
        "\n===MARKDOWN===\n"
        "## 主题1：宏和电子与国际复材供给弹性\n"
        "- 宏和电子（002011）已实现量产；国际复材认证有效。\n\n"
        "## 主题2：中国巨石供给弹性\n"
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


def _install_p7a_semantic_stubs(monkeypatch, audit_engine, raw_content: str):
    updates = []
    text_artifacts = []
    monkeypatch.setattr(audit_engine, "require_prompt", lambda _name: "{raw_deep_research}")
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *_args, **_kwargs: (
            SimpleNamespace(content=raw_content, thinking_content=""),
            SimpleNamespace(last_error_category="", last_error_message=""),
            {},
        ),
    )
    monkeypatch.setattr(audit_engine, "write_json_artifact", lambda *args, **kwargs: "p7a.json")
    monkeypatch.setattr(
        audit_engine,
        "write_text_artifact",
        lambda prefix, content, **kwargs: text_artifacts.append((prefix, content)) or "p7a.md",
    )
    monkeypatch.setattr(
        audit_engine,
        "update_run_manifest",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )
    return updates, text_artifacts


def test_p7a_rejects_json_with_empty_markdown_section(monkeypatch):
    import audit_engine

    raw_content = '{"topics": [], "summary": "' + ("x" * 120) + '"}\n===MARKDOWN===\n'
    updates, text_artifacts = _install_p7a_semantic_stubs(monkeypatch, audit_engine, raw_content)
    raw_input = "deep research input " * 20

    result = audit_engine.p7a_deep_research_audit(raw_input)

    assert result.startswith("> 【P7a结构化降级】")
    assert result.endswith(raw_input)
    assert updates[-1][1]["status"] == "failed_semantic_output"
    assert updates[-1][1]["metrics"]["semantic_error"] == "empty_markdown_output"
    assert text_artifacts == []


def test_p7a_rejects_json_only_response_as_markdown(monkeypatch):
    import audit_engine

    raw_content = '{"topics": [], "summary": "' + ("x" * 120) + '"}'
    updates, text_artifacts = _install_p7a_semantic_stubs(monkeypatch, audit_engine, raw_content)
    raw_input = "deep research input " * 20

    result = audit_engine.p7a_deep_research_audit(raw_input)

    assert result.startswith("> 【P7a结构化降级】")
    assert result.endswith(raw_input)
    assert updates[-1][1]["status"] == "failed_semantic_output"
    assert updates[-1][1]["metrics"]["semantic_error"] == "json_without_markdown"
    assert text_artifacts == []


def test_p7a_no_evidence_markdown_is_valid_empty(monkeypatch):
    import audit_engine

    raw_content = (
        '{"topics": [], "summary": "'
        + ("x" * 120)
        + '"}\n===MARKDOWN===\n未找到相关证据。'
    )
    updates, text_artifacts = _install_p7a_semantic_stubs(monkeypatch, audit_engine, raw_content)

    result = audit_engine.p7a_deep_research_audit("deep research input " * 20)

    assert result == ""
    assert updates[-1][1]["status"] == "completed_no_results"
    assert updates[-1][1]["metrics"]["data_state"] == "valid_empty"
    assert text_artifacts == []


def test_p7a_rejects_nonempty_json_when_markdown_declares_no_evidence(monkeypatch):
    import audit_engine

    raw_content = (
        '{"topics": [{"name": "不应保留的主题"}], "summary": "'
        + ("x" * 120)
        + '"}\n===MARKDOWN===\n未找到相关证据。'
    )
    updates, _ = _install_p7a_semantic_stubs(monkeypatch, audit_engine, raw_content)
    raw_input = "deep research input " * 20

    result = audit_engine.p7a_deep_research_audit(raw_input)

    assert result.startswith("> 【P7a结构化降级】")
    assert result.endswith(raw_input)
    assert updates[-1][1]["status"] == "failed_semantic_output"
    assert updates[-1][1]["metrics"]["data_state"] == "invalid"
    assert updates[-1][1]["metrics"]["semantic_error"] == "structured_topics_mismatch_no_evidence"
