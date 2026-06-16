# -*- coding: utf-8 -*-
"""Tests for optional agent assistant outputs and fail-safe hooks."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class DummyAdapter:
    def __init__(self, payload: dict | None = None, *, error: Exception | None = None):
        self.payload = payload or {}
        self.error = error

    def invoke(self, prompt: str):
        if self.error:
            raise self.error
        return SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False), thinking_content="")


class TestAgentAssistantCore:
    def test_disabled_agent_returns_skip_payload(self, monkeypatch, tmp_path):
        import artifact_store
        import agent_assistant

        monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))
        monkeypatch.setattr(agent_assistant, "AGENT_ASSIST_ENABLED", False)
        monkeypatch.setattr(agent_assistant, "get_agent_adapter", lambda: None)

        result = agent_assistant.explain_preflight({"summary": {"exit_code": 0}})

        assert result["ok"] is False
        assert result["reason"] == "disabled"

    def test_preflight_explanation_writes_json_and_markdown(self, monkeypatch, tmp_path):
        import artifact_store
        import agent_assistant

        monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))
        monkeypatch.setattr(agent_assistant, "AGENT_ASSIST_ENABLED", True)
        monkeypatch.setattr(agent_assistant, "get_agent_adapter", lambda: DummyAdapter({
            "risk_level": "low",
            "summary": "可以运行",
            "actions": ["继续执行"],
        }))
        monkeypatch.setattr(agent_assistant, "require_prompt", lambda name: "Return JSON only")

        result = agent_assistant.explain_preflight({"summary": {"exit_code": 0}})

        assert result["ok"] is True
        assert Path(result["json_path"]).exists()
        assert Path(result["md_path"]).exists()
        payload = json.loads(Path(result["json_path"]).read_text(encoding="utf-8-sig"))
        assert payload["summary"] == "可以运行"
        assert payload["risk_level"] == "low"

    def test_failure_diagnosis_survives_adapter_error(self, monkeypatch, tmp_path):
        import artifact_store
        import agent_assistant

        monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))
        monkeypatch.setattr(agent_assistant, "AGENT_ASSIST_ENABLED", True)
        monkeypatch.setattr(agent_assistant, "get_agent_adapter", lambda: DummyAdapter(error=RuntimeError("boom")))
        monkeypatch.setattr(agent_assistant, "require_prompt", lambda name: "Return JSON only")

        result = agent_assistant.diagnose_failure("report", "timeout", {"stages": {}}, "tail")

        assert result["ok"] is False
        assert result["reason"] == "invoke_failed"
        assert Path(result["json_path"]).exists()

    def test_report_review_writes_under_agent_subdir(self, monkeypatch, tmp_path):
        import artifact_store
        import agent_assistant

        monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))
        monkeypatch.setattr(agent_assistant, "AGENT_ASSIST_ENABLED", True)
        monkeypatch.setattr(agent_assistant, "get_agent_adapter", lambda: DummyAdapter({
            "pass": True,
            "issues": [],
            "strengths": ["结构完整"],
            "suggestions": [],
        }))
        monkeypatch.setattr(agent_assistant, "require_prompt", lambda name: "Return JSON only")

        result = agent_assistant.review_final_report("# 战报", {"p5": "evidence"})

        assert result["ok"] is True
        assert "/agent/" in result["json_path"].replace("\\", "/")
        assert Path(result["md_path"]).exists()


class TestPipelineCliHooks:
    def test_cmd_preflight_writes_agent_manifest_when_enabled(self, monkeypatch, tmp_path, capsys):
        import artifact_store
        import pipeline_cli

        monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))
        monkeypatch.setattr(pipeline_cli, "_collect_preflight", lambda strict: {"checks": [], "summary": {"exit_code": 0}})
        monkeypatch.setattr(
            pipeline_cli,
            "explain_preflight_and_record",
            lambda result: {
                "ok": True,
                "json_path": str(tmp_path / "agent" / "Agent_预检解释_1.json"),
                "md_path": str(tmp_path / "agent" / "Agent_预检解释_1.md"),
            },
        )

        exit_code = pipeline_cli.cmd_preflight(SimpleNamespace())
        out = capsys.readouterr().out
        manifest = artifact_store.load_run_manifest()

        assert exit_code == 0
        assert "Agent preflight artifact" in out
        assert manifest["stages"]["agent_preflight"]["status"] == "completed"


class TestOrchestratorHooks:
    def test_run_agent_report_review_records_manifest(self, monkeypatch, tmp_path):
        import artifact_store
        import orchestrator

        monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))
        monkeypatch.setattr(
            orchestrator,
            "review_final_report_and_record",
            lambda report_md, context: {
                "ok": True,
                "json_path": str(tmp_path / "agent" / "Agent_终稿审校_1.json"),
                "md_path": str(tmp_path / "agent" / "Agent_终稿审校_1.md"),
            },
        )

        orchestrator._run_agent_report_review({"final_report": "# 战报", "p5_evidence": "ok"})
        manifest = artifact_store.load_run_manifest()

        assert manifest["stages"]["agent_review"]["status"] == "completed"

    def test_run_agent_failure_diagnosis_records_manifest(self, monkeypatch, tmp_path):
        import artifact_store
        import orchestrator

        monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))
        monkeypatch.setattr(
            orchestrator,
            "diagnose_failure_and_record",
            lambda stage_name, error_text, manifest, log_excerpt="": {
                "ok": True,
                "json_path": str(tmp_path / "agent" / "Agent_失败诊断_1.json"),
                "md_path": str(tmp_path / "agent" / "Agent_失败诊断_1.md"),
            },
        )

        orchestrator._run_agent_failure_diagnosis("report", RuntimeError("boom"))
        manifest = artifact_store.load_run_manifest()

        assert manifest["stages"]["agent_diagnose"]["status"] == "completed"
