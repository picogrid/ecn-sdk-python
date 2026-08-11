# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import contextvars
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

_CURRENT_BUFFER: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "release_stage_buffer", default=None
)
_STAGE_PERMIT_HELD: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "release_stage_permit_held", default=False
)
_OUTPUT_LOCK = threading.Lock()
_RESOURCE_LOCK = threading.Lock()
_RESOURCE_SEMAPHORES: dict[str, threading.BoundedSemaphore] = {}
_WORKER_PERMIT_LOCK = threading.Lock()
_WORKER_PERMITS: threading.BoundedSemaphore | None = None


def configure_resources(limits: Mapping[str, int]) -> None:
    """Replace the process-wide concurrency limits for named resources."""
    semaphores: dict[str, threading.BoundedSemaphore] = {}
    for name, limit in limits.items():
        if limit < 1:
            raise ValueError(f"resource limit for {name!r} must be at least 1")
        semaphores[name] = threading.BoundedSemaphore(limit)

    with _RESOURCE_LOCK:
        global _RESOURCE_SEMAPHORES
        _RESOURCE_SEMAPHORES = semaphores


@contextmanager
def resource(name: str) -> Iterator[None]:
    """Hold a configured resource slot for the duration of the context."""
    with _RESOURCE_LOCK:
        semaphore = _RESOURCE_SEMAPHORES.get(name)
    if semaphore is None:
        yield
        return

    semaphore.acquire()
    try:
        yield
    finally:
        semaphore.release()


@dataclass(frozen=True)
class Stage:
    name: str
    run: Callable[[], Any]
    deps: tuple[str, ...] = ()


class StageGraphError(RuntimeError):
    """Raised for a malformed stage graph (duplicate name, unknown dep, cycle)."""


def emit(text: str) -> None:
    """Write text to the current stage's buffer, or immediately outside a stage."""
    buffer = _CURRENT_BUFFER.get()
    if buffer is not None:
        buffer.append(text)
        return
    with _OUTPUT_LOCK:
        print(text, flush=True)


@dataclass
class _RunState:
    durations: dict[str, float] = field(default_factory=dict)
    first_error: BaseException | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record_failure(self, error: BaseException) -> None:
        with self.lock:
            if self.first_error is None:
                self.first_error = error

    def has_failed(self) -> bool:
        with self.lock:
            return self.first_error is not None


def _validate_graph(stages: Sequence[Stage]) -> tuple[list[int], list[list[int]], list[int]]:
    indices_by_name: dict[str, int] = {}
    for index, stage in enumerate(stages):
        if stage.name in indices_by_name:
            raise StageGraphError(f"duplicate stage name: {stage.name}")
        indices_by_name[stage.name] = index

    dependents: list[list[int]] = [[] for _ in stages]
    dependency_counts: list[int] = []
    for index, stage in enumerate(stages):
        dependency_counts.append(len(stage.deps))
        for dependency_name in stage.deps:
            dependency_index = indices_by_name.get(dependency_name)
            if dependency_index is None:
                raise StageGraphError(
                    f"stage {stage.name!r} has unknown dependency {dependency_name!r}"
                )
            dependents[dependency_index].append(index)

    remaining_counts = dependency_counts.copy()
    ready = [index for index, count in enumerate(remaining_counts) if count == 0]
    topological_order: list[int] = []
    while ready:
        index = ready.pop(0)
        topological_order.append(index)
        for dependent_index in dependents[index]:
            remaining_counts[dependent_index] -= 1
            if remaining_counts[dependent_index] == 0:
                ready.append(dependent_index)
        ready.sort()

    if len(topological_order) != len(stages):
        raise StageGraphError("stage graph contains a cycle")
    return topological_order, dependents, dependency_counts


def _write_stage_start(name: str) -> None:
    with _OUTPUT_LOCK:
        print(f"> {name}", flush=True)


def _write_stage_end(name: str, elapsed: float, buffer: list[str], *, failed: bool) -> None:
    marker = "x" if failed else "+"
    with _OUTPUT_LOCK:
        print(f"{marker} {name} ({elapsed:.1f}s)")
        for text in buffer:
            print(text)
        sys.stdout.flush()


def _execute_stage(stage: Stage, buffer: list[str], state: _RunState) -> Any:
    token = _CURRENT_BUFFER.set(buffer)
    started = time.monotonic()
    _write_stage_start(stage.name)
    try:
        permits = _WORKER_PERMITS
        if permits is None:
            raise RuntimeError("stage executed without a worker permit pool")
        permits.acquire()
        permit_token = _STAGE_PERMIT_HELD.set(True)
        try:
            result = stage.run()
        finally:
            _STAGE_PERMIT_HELD.reset(permit_token)
            permits.release()
    except BaseException as error:
        elapsed = time.monotonic() - started
        state.record_failure(error)
        with state.lock:
            state.durations[stage.name] = elapsed
        _write_stage_end(stage.name, elapsed, buffer, failed=True)
        raise
    else:
        elapsed = time.monotonic() - started
        with state.lock:
            state.durations[stage.name] = elapsed
        _write_stage_end(stage.name, elapsed, buffer, failed=False)
        return result
    finally:
        _CURRENT_BUFFER.reset(token)


def _write_summary(stages: Sequence[Stage], durations: dict[str, float], wall: float) -> None:
    ordered = sorted(
        ((stage.name, durations[stage.name]) for stage in stages),
        key=lambda item: item[1],
        reverse=True,
    )
    name_width = max((len(name) for name, _ in ordered), default=5)
    serial_sum = sum(duration for _, duration in ordered)
    speedup = serial_sum / wall if wall > 0.0 else 1.0
    with _OUTPUT_LOCK:
        print("Stage timings (slowest first):")
        for name, duration in ordered:
            print(f"  {name:<{name_width}}  {duration:>7.1f}s")
        print(f"wall {wall:.1f}s | serial-sum {serial_sum:.1f}s | speedup {speedup:.2f}x")
        sys.stdout.flush()


def run_stages(stages: Sequence[Stage], *, jobs: int) -> dict[str, Any]:
    """Execute a stage dependency graph and return each stage's result."""
    global _WORKER_PERMITS
    if jobs < 1:
        raise ValueError("jobs must be at least 1")

    if _STAGE_PERMIT_HELD.get():
        permits = _WORKER_PERMITS
        if permits is None:
            raise RuntimeError("nested stage run without a worker permit pool")
        # _STAGE_PERMIT_HELD is set only after _execute_stage acquires exactly
        # one permit, so lending that permit to the child DAG is balanced.
        permits.release()
        try:
            return _run_stages(stages, jobs=jobs)
        finally:
            permits.acquire()

    with _WORKER_PERMIT_LOCK:
        _WORKER_PERMITS = threading.BoundedSemaphore(jobs)
        try:
            return _run_stages(stages, jobs=jobs)
        finally:
            _WORKER_PERMITS = None


def _run_stages(stages: Sequence[Stage], *, jobs: int) -> dict[str, Any]:
    topological_order, dependents, dependency_counts = _validate_graph(stages)
    state = _RunState()
    results: dict[str, Any] = {}
    wall_started = time.monotonic()

    if jobs == 1:
        for index in topological_order:
            stage = stages[index]
            results[stage.name] = _execute_stage(stage, [], state)
    else:
        ready = [index for index, count in enumerate(dependency_counts) if count == 0]
        in_flight: dict[Future[Any], int] = {}
        with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="release-stage") as executor:
            while ready or in_flight:
                while ready and len(in_flight) < jobs:
                    with state.lock:
                        if state.first_error is not None:
                            # Stop submitting, but deliberately drain in-flight work so
                            # its cleanup completes; _execute_stage already reported the
                            # failing stage and its buffered output.
                            break
                        index = ready.pop(0)
                        future = executor.submit(_execute_stage, stages[index], [], state)
                        in_flight[future] = index

                if not in_flight:
                    break
                completed, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in sorted(completed, key=lambda item: in_flight[item]):
                    index = in_flight.pop(future)
                    stage = stages[index]
                    if future.exception() is not None:
                        continue
                    results[stage.name] = future.result()
                    if state.has_failed():
                        continue
                    for dependent_index in dependents[index]:
                        dependency_counts[dependent_index] -= 1
                        if dependency_counts[dependent_index] == 0:
                            ready.append(dependent_index)
                    ready.sort()

    if state.first_error is not None:
        raise state.first_error
    wall = time.monotonic() - wall_started
    _write_summary(stages, state.durations, wall)
    return results
