from __future__ import annotations

import json
import concurrent.futures
import inspect
import multiprocessing
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace


def test_run_daily_uses_one_canonical_app_directory() -> None:
    batch_path = Path(__file__).resolve().parents[2] / "run_daily.bat"
    batch_text = batch_path.read_text(encoding="utf-8")

    assert batch_text.index("chcp 65001 >nul") < batch_text.index(
        'set "APP_DIR=%ROOT_DIR%每日战报"'
    )
    assert 'set "APP_DIR=%ROOT_DIR%每日战报"' in batch_text
    assert 'set "PYTHONDONTWRITEBYTECODE=1"' in batch_text
    assert 'for /d %%D in ("%ROOT_DIR%*")' not in batch_text
    assert "call :maybe_preflight" not in batch_text


def test_google_client_receives_transport_timeout(monkeypatch) -> None:
    import native_llm_adapter

    client_kwargs: dict = {}

    class FakeConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeHttpOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeTool:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeGoogleSearch:
        pass

    class FakeModels:
        def generate_content(self, **_kwargs):
            return SimpleNamespace(text="OK")

    class FakeClient:
        def __init__(self, **kwargs):
            client_kwargs.update(kwargs)
            self.models = FakeModels()

    fake_types = SimpleNamespace(
        GenerateContentConfig=FakeConfig,
        HttpOptions=FakeHttpOptions,
        Tool=FakeTool,
        GoogleSearch=FakeGoogleSearch,
    )
    fake_genai = SimpleNamespace(Client=FakeClient, types=fake_types)
    monkeypatch.setitem(sys.modules, "google", SimpleNamespace(genai=fake_genai))
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)

    adapter = native_llm_adapter.NativeLLMAdapter(
        model="gemini-2.5-flash",
        api_key="test-key",
        timeout=12.5,
        max_retries=1,
    )

    assert adapter._call_gemini("ping") == "OK"
    assert client_kwargs["http_options"].timeout == 12_500


def test_audit_invocation_does_not_use_thread_timeout_wrapper(monkeypatch) -> None:
    import audit_engine

    class ForbiddenExecutor:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("audit invocation must rely on transport timeout and stage process deadline")

    payload = {
        "summary": {"passed": 1, "deleted": 0},
        "high_trust": ["verified"],
        "medium_trust": [],
        "deleted": [],
    }
    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", ForbiddenExecutor)
    monkeypatch.setattr(audit_engine, "require_prompt", lambda _name: "{raw_data}")
    monkeypatch.setattr(
        audit_engine.pro_adapter,
        "invoke",
        lambda _prompt: SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)),
    )
    monkeypatch.setattr(audit_engine, "write_json_artifact", lambda *_args, **_kwargs: "audit.json")
    monkeypatch.setattr(audit_engine, "write_text_artifact", lambda *_args, **_kwargs: "audit.md")
    monkeypatch.setattr(audit_engine, "update_run_manifest", lambda *_args, **_kwargs: None)

    assert "verified" in audit_engine.gemini_pro_audit("fresh evidence", audit_type="p5")


def test_collection_threads_do_not_claim_fake_wall_clock_timeouts() -> None:
    import orchestrator
    import p3_agent

    assert ".result(timeout=" not in inspect.getsource(orchestrator._stage_collect_intel)
    assert ".result(timeout=" not in inspect.getsource(p3_agent.collect_p3_parallel_only)


def test_p4_history_call_has_bounded_transport_timeout(monkeypatch) -> None:
    import pandas as pd
    import p4_vanguard
    import yfinance

    captured_kwargs: dict = {}
    yesterday = date.today() - timedelta(days=1)
    two_days_ago = yesterday - timedelta(days=1)

    class FakeTicker:
        def __init__(self, _ticker):
            pass

        def history(self, **kwargs):
            captured_kwargs.update(kwargs)
            return pd.DataFrame(
                {"Close": [100.0, 101.0]},
                index=pd.to_datetime([two_days_ago.isoformat(), yesterday.isoformat()]),
            )

    monkeypatch.setattr(yfinance, "Ticker", FakeTicker)
    monkeypatch.setattr(p4_vanguard, "VANGUARD_TICKERS", {"TestCo": "TEST"})
    monkeypatch.setattr(p4_vanguard, "SUPER_GIANTS", set())
    monkeypatch.setattr(p4_vanguard, "STRATEGIC_GIANTS", set())
    monkeypatch.setattr(p4_vanguard, "write_text_artifact", lambda *_args, **_kwargs: "p4.md")
    monkeypatch.setattr(p4_vanguard, "update_run_manifest", lambda *_args, **_kwargs: None)

    report = p4_vanguard._fetch_vanguard_prices_inline()

    assert "TestCo" in report
    assert 0 < captured_kwargs["timeout"] <= 20
    assert captured_kwargs["raise_errors"] is True


def test_p4_hard_timeout_terminates_worker(monkeypatch) -> None:
    import p4_vanguard

    manifest_updates: list[tuple[str, str]] = []

    class FakeRecv:
        def poll(self, _timeout):
            return False

        def close(self):
            pass

    class FakeSend:
        def close(self):
            pass

    class FakeProcess:
        def __init__(self):
            self.alive = False
            self.terminated = False

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.terminated = True
            self.alive = False

        def kill(self):
            self.alive = False

        def join(self, timeout=None):
            _ = timeout

    fake_process = FakeProcess()

    class FakeContext:
        def Pipe(self, duplex=False):
            assert duplex is False
            return FakeRecv(), FakeSend()

        def Process(self, **_kwargs):
            return fake_process

    monkeypatch.setattr(p4_vanguard.multiprocessing, "get_context", lambda _mode: FakeContext())
    monkeypatch.setattr(
        p4_vanguard,
        "update_run_manifest",
        lambda stage, *, status, **_kwargs: manifest_updates.append((stage, status)),
    )

    assert p4_vanguard.fetch_vanguard_prices() == ""
    assert fake_process.terminated is True
    assert manifest_updates == [("p4", "failed_timeout")]


def test_p4_stop_process_terminates_a_real_spawned_worker() -> None:
    import p4_vanguard

    process = multiprocessing.get_context("spawn").Process(target=time.sleep, args=(60,))
    process.start()
    started = time.monotonic()
    try:
        p4_vanguard._stop_process(process)
        assert not process.is_alive()
        assert process.exitcode is not None
        assert time.monotonic() - started < 10
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
