from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing
import os
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from artifact_store import (
    UTF8_FILE_ENCODING,
    configure_utf8_environment,
    get_error_log_path,
    get_pipeline_log_path,
    update_run_manifest,
)
from utils import normalize_common_mojibake_artifacts

StageHandler = Callable[[dict[str, Any]], None]

class StageTimeoutError(RuntimeError):
    def __init__(self, stage_name: str, timeout_seconds: int) -> None:
        super().__init__(f"stage '{stage_name}' exceeded timeout_seconds={timeout_seconds}")
        self.stage_name = stage_name
        self.timeout_seconds = timeout_seconds


def _configure_child_logging() -> None:
    configure_utf8_environment()
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
        pipeline_handler = logging.FileHandler(get_pipeline_log_path(), encoding=UTF8_FILE_ENCODING)
        pipeline_handler.setLevel(logging.INFO)
        pipeline_handler.setFormatter(formatter)
        root_logger.addHandler(pipeline_handler)
    except Exception:
        pass

    try:
        error_handler = logging.FileHandler(get_error_log_path(), encoding=UTF8_FILE_ENCODING)
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
    context["current_stage"] = stage.name

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
        # Why: 用 update 而非 clear+update，保留主进程中不可 JSON 序列化的字段
        #      （如 datetime 对象），子进程落盘时会跳过这些字段
        context.update(updated_context)


def _run_stage_in_subprocess(stage: StageSpec, context: dict[str, Any]) -> dict[str, Any]:
    """在子进程中执行 stage handler，通过临时文件传递结果。

    Why: multiprocessing.Queue 底层基于管道(pipe)，Windows 管道缓冲区仅 64KB。
         当 context 数据量超过缓冲区时（如 71h 回溯窗口下 354 篇文章 ~500KB+），
         子进程 queue.put() 会阻塞等主进程 get() 腾空间，而主进程在 process.join()
         里等子进程退出 → 经典互等死锁。
         改用临时文件传递 context：Queue 只传几十字节的文件路径，永远不会阻塞管道。
    """
    mp_context = multiprocessing.get_context("spawn")
    # Why: Queue 只传极小的状态信号和文件路径，不传 context 数据本身
    result_queue: multiprocessing.queues.Queue = mp_context.Queue(maxsize=1)

    # Reserve a private random directory so concurrent/retried stages cannot
    # collide or consume a stale result left by another process.
    tmp_dir = tempfile.gettempdir()
    result_dir = tempfile.mkdtemp(prefix="vcp_stage_", dir=tmp_dir)
    result_file_path = os.path.join(result_dir, "context.json")

    try:
        process = mp_context.Process(
            target=_stage_process_entrypoint,
            args=(stage, context, result_queue, result_file_path),
            name=f"stage-{stage.name}",
        )
        process.start()

        # Drain the tiny status signal before join so the child cannot block on
        # a full multiprocessing pipe.
        result_signal: dict[str, Any] | None = None
        deadline = time.time() + (stage.timeout_seconds or 900)
        while process.is_alive() and time.time() < deadline:
            try:
                result_signal = result_queue.get(timeout=2.0)
                break
            except Exception:
                continue

        remaining_timeout = max(0.1, deadline - time.time())
        process.join(timeout=min(remaining_timeout, 30))

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

        if result_signal is None:
            try:
                result_signal = result_queue.get_nowait()
            except Exception:
                result_signal = None

        try:
            result_queue.close()
            result_queue.join_thread()
        except Exception:
            pass

        if result_signal is None:
            if process.exitcode != 0:
                return {
                    "status": "error",
                    "error": f"stage subprocess exited unexpectedly with code={process.exitcode}",
                }
            updated_context = _read_context_from_file(result_file_path)
            if updated_context is None:
                return {
                    "status": "error",
                    "error": "stage subprocess produced no valid context result",
                }
            return {"status": "success", "context": updated_context}

        if not isinstance(result_signal, dict):
            return {"status": "error", "error": "stage subprocess returned an invalid status signal"}

        status = result_signal.get("status", "error")
        if status == "error":
            return result_signal
        if status != "success":
            return {"status": "error", "error": f"stage subprocess returned unknown status={status}"}

        updated_context = _read_context_from_file(
            result_file_path,
            expected_sha256=str(result_signal.get("context_sha256") or "") or None,
        )
        if updated_context is None:
            return {
                "status": "error",
                "error": "stage subprocess context result is missing, corrupt, or failed validation",
            }
        return {"status": "success", "context": updated_context}
    finally:
        _cleanup_temp_file(result_file_path)
        try:
            os.rmdir(result_dir)
        except OSError:
            pass


def _context_sha256(context: dict[str, Any]) -> str:
    canonical = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_context_from_file(
    file_path: str,
    *,
    expected_sha256: str | None = None,
) -> dict[str, Any] | None:
    """从临时文件读取子进程序列化的 context。"""
    try:
        if not os.path.exists(file_path):
            return None
        with open(file_path, "r", encoding=UTF8_FILE_ENCODING) as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("context_file_version") == 1:
            saved_context = data.get("context")
            saved_sha256 = str(data.get("sha256") or "")
            if not isinstance(saved_context, dict) or not saved_sha256:
                raise ValueError("context result envelope is incomplete")
            actual_sha256 = _context_sha256(saved_context)
            if saved_sha256 != actual_sha256:
                raise ValueError("context result checksum mismatch")
            if expected_sha256 and expected_sha256 != actual_sha256:
                raise ValueError("context result signal checksum mismatch")
            return saved_context
        if expected_sha256:
            raise ValueError("context result envelope is missing")
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logging.warning("[control] 读取临时 context 文件失败: %s", exc)
        return None
    finally:
        _cleanup_temp_file(file_path)


def _cleanup_temp_file(file_path: str) -> None:
    """安全清理临时文件。"""
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        pass


def _stage_process_entrypoint(
    stage: StageSpec,
    context: dict[str, Any],
    result_queue: multiprocessing.queues.Queue,
    result_file_path: str,
) -> None:
    """子进程入口：执行 handler，通过临时文件传递大型结果。

    Why: Queue 只发送极小的信号 dict（状态 + traceback），
         大型 context 数据写入临时文件由主进程读取，
         避免管道缓冲区溢出导致的死锁。
    """
    _configure_child_logging()
    try:
        stage.handler(context)

        # Why: 先落盘再发信号，确保主进程读到信号时文件已就绪
        context_sha256 = _write_context_to_file(context, result_file_path)

        # The signal binds the child result to the validated on-disk envelope.
        result_queue.put({"status": "success", "context_sha256": context_sha256})
    except Exception as exc:
        result_queue.put(
            {
                "status": "error",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )


def _write_context_to_file(context: dict[str, Any], file_path: str) -> str:
    """将 context 序列化到临时文件。

    Why: context 可能包含不可 JSON 序列化的对象（如 datetime），
         需要做容错处理，丢弃不可序列化的字段比整体失败更好。
    """
    serializable_context = {}
    for key, value in context.items():
        value = normalize_common_mojibake_artifacts(value)
        try:
            json.dumps(value, ensure_ascii=False)
            serializable_context[key] = value
        except (TypeError, ValueError):
            # Why: 不可序列化的字段（如 datetime、handler 函数）跳过
            #      这些字段在主进程原始 context 中仍然存在
            if isinstance(value, str):
                serializable_context[key] = value
            else:
                logging.debug("[control] context key '%s' 不可序列化，跳过", key)

    context_sha256 = _context_sha256(serializable_context)
    envelope = {
        "context_file_version": 1,
        "context": serializable_context,
        "sha256": context_sha256,
    }
    target_dir = os.path.dirname(os.path.abspath(file_path))
    fd, temp_path = tempfile.mkstemp(prefix=".context_", suffix=".tmp", dir=target_dir)
    try:
        with os.fdopen(fd, "w", encoding=UTF8_FILE_ENCODING) as f:
            json.dump(envelope, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, file_path)
    finally:
        _cleanup_temp_file(temp_path)
    return context_sha256
