from __future__ import annotations

import logging
import multiprocessing
import os
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from artifact_store import get_error_log_path, get_pipeline_log_path, update_run_manifest

StageHandler = Callable[[dict[str, Any]], None]

class StageTimeoutError(RuntimeError):
    def __init__(self, stage_name: str, timeout_seconds: int) -> None:
        super().__init__(f"stage '{stage_name}' exceeded timeout_seconds={timeout_seconds}")
        self.stage_name = stage_name
        self.timeout_seconds = timeout_seconds


def _configure_child_logging() -> None:
    root_logger = logging.getLogger()
    if root_logger.handlers:
        return

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    root_logger.setLevel(logging.INFO)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return

    try:
        pipeline_handler = logging.FileHandler(get_pipeline_log_path(), encoding="utf-8")
        pipeline_handler.setLevel(logging.INFO)
        pipeline_handler.setFormatter(formatter)
        root_logger.addHandler(pipeline_handler)
    except Exception:
        pass

    try:
        error_handler = logging.FileHandler(get_error_log_path(), encoding="utf-8")
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(formatter)
        root_logger.addHandler(error_handler)
    except Exception:
        pass


def _stage_execution_mode() -> str:
    return os.environ.get("VCP_STAGE_EXECUTION_MODE", "process").strip().lower()


@dataclass(frozen=True)
class StageSpec:
    name: str
    handler: StageHandler
    timeout_seconds: int | None = None
    retry_limit: int = 0
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    description: str = ""


def run_stage_registry(
    stages: Iterable[StageSpec],
    context: dict[str, Any],
    *,
    start_at: str | None = None,
) -> None:
    started = start_at is None

    for stage in stages:
        if not started and stage.name != start_at:
            continue
        started = True
        _run_single_stage(stage, context)


def _run_single_stage(stage: StageSpec, context: dict[str, Any]) -> None:
    attempts = max(stage.retry_limit, 0) + 1

    for attempt in range(1, attempts + 1):
        started_at = time.time()
        update_run_manifest(
            stage.name,
            status="running",
            metrics={
                "attempt": attempt,
                "retry_limit": stage.retry_limit,
                "timeout_seconds": stage.timeout_seconds or 0,
            },
        )
        try:
            _execute_stage_handler(stage, context)
        except StageTimeoutError as exc:
            elapsed = round(time.time() - started_at, 1)
            is_last_attempt = attempt >= attempts
            update_run_manifest(
                stage.name,
                status="failed_timeout" if is_last_attempt else "retrying",
                metrics={
                    "attempt": attempt,
                    "retry_limit": stage.retry_limit,
                    "control_elapsed_seconds": elapsed,
                    "timeout_seconds": stage.timeout_seconds or 0,
                    "error": str(exc),
                },
            )
            logging.error("[control] stage %s timed out on attempt %s", stage.name, attempt)
            if is_last_attempt:
                raise
            time.sleep(min(2 ** (attempt - 1), 5))
            continue
        except Exception as exc:
            elapsed = round(time.time() - started_at, 1)
            is_last_attempt = attempt >= attempts
            update_run_manifest(
                stage.name,
                status="failed_exception" if is_last_attempt else "retrying",
                metrics={
                    "attempt": attempt,
                    "retry_limit": stage.retry_limit,
                    "control_elapsed_seconds": elapsed,
                    "error": str(exc),
                },
            )
            logging.exception("[control] stage %s failed on attempt %s", stage.name, attempt)
            if is_last_attempt:
                raise
            time.sleep(min(2 ** (attempt - 1), 5))
            continue

        elapsed = round(time.time() - started_at, 1)
        update_run_manifest(
            stage.name,
            status="completed",
            metrics={
                "attempt": attempt,
                "retry_limit": stage.retry_limit,
                "control_elapsed_seconds": elapsed,
            },
        )
        return


def _execute_stage_handler(stage: StageSpec, context: dict[str, Any]) -> None:
    if not stage.timeout_seconds or stage.timeout_seconds <= 0:
        stage.handler(context)
        return

    if _stage_execution_mode() != "process":
        logging.info("[control] inline stage execution enabled for %s", stage.name)
        stage.handler(context)
        return

    runtime_result = _run_stage_in_subprocess(stage, context)
    if runtime_result["status"] == "timeout":
        raise StageTimeoutError(stage.name, int(stage.timeout_seconds))

    if runtime_result["status"] == "error":
        error = runtime_result.get("error") or f"stage {stage.name} failed in subprocess"
        trace = runtime_result.get("traceback", "")
        if trace:
            logging.error("[control] stage %s subprocess traceback:\n%s", stage.name, trace.rstrip())
        raise RuntimeError(error)

    updated_context = runtime_result.get("context")
    if isinstance(updated_context, dict):
        context.clear()
        context.update(updated_context)


def _run_stage_in_subprocess(stage: StageSpec, context: dict[str, Any]) -> dict[str, Any]:
    mp_context = multiprocessing.get_context("spawn")
    result_queue: multiprocessing.queues.Queue = mp_context.Queue(maxsize=1)
    process = mp_context.Process(
        target=_stage_process_entrypoint,
        args=(stage, context, result_queue),
        name=f"stage-{stage.name}",
    )
    process.start()
    process.join(stage.timeout_seconds)

    if process.is_alive():
        logging.error(
            "[control] stage %s exceeded timeout_seconds=%s; terminating subprocess pid=%s",
            stage.name,
            stage.timeout_seconds,
            process.pid,
        )
        process.terminate()
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        return {"status": "timeout"}

    result: dict[str, Any] | None = None
    try:
        if not result_queue.empty():
            result = result_queue.get_nowait()
    except Exception:
        result = None
    finally:
        result_queue.close()
        result_queue.join_thread()

    if result is None:
        if process.exitcode == 0:
            return {"status": "success", "context": context}
        return {
            "status": "error",
            "error": f"stage subprocess exited unexpectedly with code={process.exitcode}",
        }

    return result


def _stage_process_entrypoint(
    stage: StageSpec,
    context: dict[str, Any],
    result_queue: multiprocessing.queues.Queue,
) -> None:
    _configure_child_logging()
    try:
        stage.handler(context)
        result_queue.put({"status": "success", "context": context})
    except Exception as exc:
        result_queue.put(
            {
                "status": "error",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
