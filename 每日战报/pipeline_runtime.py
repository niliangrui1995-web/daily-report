from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from artifact_store import update_run_manifest

StageHandler = Callable[[dict[str, Any]], None]


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
            stage.handler(context)
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
