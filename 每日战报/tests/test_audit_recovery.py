"""Regression tests for P5/P7.5 audit recovery."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
