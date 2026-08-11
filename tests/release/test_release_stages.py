# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest
from scripts._release_stages import (
    Stage,
    StageGraphError,
    configure_resources,
    emit,
    resource,
    run_stages,
)


def test_diamond_dag_returns_values_and_respects_dependencies() -> None:
    completed: list[str] = []
    completed_lock = threading.Lock()

    def record(name: str) -> str:
        with completed_lock:
            completed.append(name)
        return f"{name}-value"

    def finish() -> str:
        with completed_lock:
            assert "left" in completed
            assert "right" in completed
            completed.append("finish")
        return "finish-value"

    results = run_stages(
        [
            Stage("start", lambda: record("start")),
            Stage("left", lambda: record("left"), ("start",)),
            Stage("right", lambda: record("right"), ("start",)),
            Stage("finish", finish, ("left", "right")),
        ],
        jobs=2,
    )

    assert results == {
        "start": "start-value",
        "left": "left-value",
        "right": "right-value",
        "finish": "finish-value",
    }
    assert completed[0] == "start"
    assert completed[-1] == "finish"


def test_independent_stages_overlap() -> None:
    rendezvous = threading.Barrier(2)

    def wait_for_peer(name: str) -> str:
        rendezvous.wait(timeout=2)
        return name

    assert run_stages(
        [
            Stage("one", lambda: wait_for_peer("one")),
            Stage("two", lambda: wait_for_peer("two")),
        ],
        jobs=2,
    ) == {"one": "one", "two": "two"}


def test_jobs_one_uses_deterministic_topological_order() -> None:
    order: list[str] = []

    def record(name: str) -> str:
        order.append(name)
        return name

    results = run_stages(
        [
            Stage("root-a", lambda: record("root-a")),
            Stage("root-b", lambda: record("root-b")),
            Stage("child-a", lambda: record("child-a"), ("root-a",)),
            Stage("child-b", lambda: record("child-b"), ("root-b",)),
        ],
        jobs=1,
    )

    assert results == {
        "root-a": "root-a",
        "root-b": "root-b",
        "child-a": "child-a",
        "child-b": "child-b",
    }
    assert order == ["root-a", "root-b", "child-a", "child-b"]


@pytest.mark.parametrize(
    "stages",
    [
        [Stage("same", lambda: None), Stage("same", lambda: None)],
        [Stage("stage", lambda: None, ("missing",))],
        [
            Stage("one", lambda: None, ("two",)),
            Stage("two", lambda: None, ("one",)),
        ],
    ],
    ids=["duplicate-name", "unknown-dependency", "cycle"],
)
def test_malformed_graph_raises(stages: list[Stage]) -> None:
    with pytest.raises(StageGraphError):
        run_stages(stages, jobs=2)


def test_failure_reraises_original_exception_and_skips_dependents() -> None:
    class SentinelError(Exception):
        pass

    expected = SentinelError("original message")
    dependent_ran = False

    def fail() -> None:
        raise expected

    def dependent() -> None:
        nonlocal dependent_ran
        dependent_ran = True

    with pytest.raises(SentinelError, match=r"^original message$") as raised:
        run_stages(
            [Stage("failure", fail), Stage("dependent", dependent, ("failure",))],
            jobs=2,
        )

    assert raised.value is expected
    assert not dependent_ran


def test_emit_output_stays_with_its_stage_under_concurrency(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rendezvous = threading.Barrier(2)

    def produce(name: str) -> None:
        emit(f"{name} before")
        rendezvous.wait(timeout=2)
        time.sleep(0.01 if name == "alpha" else 0.0)
        emit(f"{name} after")

    run_stages(
        [Stage("alpha", lambda: produce("alpha")), Stage("beta", lambda: produce("beta"))],
        jobs=2,
    )
    output = capsys.readouterr().out

    alpha_before = output.index("alpha before")
    alpha_after = output.index("alpha after")
    beta_before = output.index("beta before")
    beta_after = output.index("beta after")
    assert alpha_before < alpha_after
    assert beta_before < beta_after
    assert not (alpha_before < beta_before < alpha_after)
    assert not (beta_before < alpha_before < beta_after)


def test_emit_prints_immediately_outside_stage(capsys: pytest.CaptureFixture[str]) -> None:
    emit("outside")

    assert capsys.readouterr().out == "outside\n"


@pytest.fixture(autouse=True)
def reset_resource_limits() -> Iterator[None]:
    configure_resources({})
    yield
    configure_resources({})


def test_resource_limit_one_serializes_concurrent_stages() -> None:
    live = 0
    observed_maximum = 0
    counter_lock = threading.Lock()
    rendezvous = threading.Barrier(2)

    def contend() -> None:
        nonlocal live, observed_maximum
        with resource("network"):
            with counter_lock:
                live += 1
                observed_maximum = max(observed_maximum, live)
            try:
                rendezvous.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass
            finally:
                with counter_lock:
                    live -= 1

    configure_resources({"network": 1})
    run_stages(
        [Stage("one", contend), Stage("two", contend)],
        jobs=2,
    )

    assert observed_maximum == 1


def test_resource_limit_two_allows_exactly_two_concurrent_stages() -> None:
    first_two_entered = threading.Barrier(2)
    third_may_attempt = threading.Event()
    third_entered = threading.Event()
    release_first_two = threading.Event()
    third_overlapped_first_two = False

    def hold_first_slot(*, coordinate_third: bool) -> None:
        with resource("network"):
            first_two_entered.wait(timeout=2)
            if coordinate_third:
                third_may_attempt.set()
                third_entered.wait(timeout=0.5)
                release_first_two.set()
            else:
                release_first_two.wait(timeout=2)

    def enter_third() -> None:
        nonlocal third_overlapped_first_two
        third_may_attempt.wait(timeout=2)
        with resource("network"):
            third_overlapped_first_two = not release_first_two.is_set()
            third_entered.set()

    configure_resources({"network": 2})
    run_stages(
        [
            Stage("one", lambda: hold_first_slot(coordinate_third=True)),
            Stage("two", lambda: hold_first_slot(coordinate_third=False)),
            Stage("three", enter_third),
        ],
        jobs=3,
    )

    assert not third_overlapped_first_two


def test_unconfigured_resource_allows_full_concurrency() -> None:
    live = 0
    observed_maximum = 0
    counter_lock = threading.Lock()
    all_stages = threading.Barrier(3)

    def contend() -> None:
        nonlocal live, observed_maximum
        with resource("unconfigured"):
            with counter_lock:
                live += 1
                observed_maximum = max(observed_maximum, live)
            try:
                all_stages.wait(timeout=2)
            finally:
                with counter_lock:
                    live -= 1

    configure_resources({"network": 1})
    run_stages(
        [Stage("one", contend), Stage("two", contend), Stage("three", contend)],
        jobs=3,
    )

    assert observed_maximum == 3


def test_resource_releases_slot_when_body_raises() -> None:
    configure_resources({"network": 1})

    with pytest.raises(RuntimeError, match=r"^failure$"), resource("network"):
        raise RuntimeError("failure")

    with resource("network"):
        pass


def test_resource_limit_is_shared_with_nested_run_stages() -> None:
    live = 0
    observed_maximum = 0
    completed: list[str] = []
    counter_lock = threading.Lock()
    rendezvous = threading.Barrier(2)

    def contend(name: str) -> None:
        nonlocal live, observed_maximum
        with resource("network"):
            with counter_lock:
                live += 1
                observed_maximum = max(observed_maximum, live)
            try:
                rendezvous.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass
            finally:
                completed.append(name)
                with counter_lock:
                    live -= 1

    def run_nested() -> None:
        run_stages(
            [
                Stage("nested-one", lambda: contend("nested-one")),
                Stage("nested-two", lambda: contend("nested-two")),
            ],
            jobs=2,
        )

    configure_resources({"network": 1})
    run_stages(
        [
            Stage("nested-parent", run_nested),
            Stage("top-level-sibling", lambda: contend("top-level-sibling")),
        ],
        jobs=2,
    )

    assert observed_maximum == 1
    assert sorted(completed) == ["nested-one", "nested-two", "top-level-sibling"]


def test_jobs_cap_is_shared_with_nested_run_stages() -> None:
    live = 0
    observed_maximum = 0
    counter_lock = threading.Lock()
    first_three = threading.Barrier(3)

    def contend() -> None:
        nonlocal live, observed_maximum
        with counter_lock:
            live += 1
            observed_maximum = max(observed_maximum, live)
        try:
            first_three.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        finally:
            with counter_lock:
                live -= 1

    def run_nested() -> None:
        run_stages(
            [Stage("nested-one", contend), Stage("nested-two", contend)],
            jobs=2,
        )

    run_stages(
        [Stage("nested-parent", run_nested), Stage("top-level-sibling", contend)],
        jobs=2,
    )

    assert observed_maximum == 2


def test_jobs_one_is_serial_with_nested_run_stages() -> None:
    live = 0
    observed_maximum = 0
    counter_lock = threading.Lock()

    def run_nested() -> None:
        record()
        run_stages(
            [Stage("nested-one", record), Stage("nested-two", record)],
            jobs=1,
        )
        record()

    def record() -> None:
        nonlocal live, observed_maximum
        with counter_lock:
            live += 1
            observed_maximum = max(observed_maximum, live)
            live -= 1

    run_stages([Stage("nested-parent", run_nested)], jobs=1)

    assert observed_maximum == 1


def test_configure_resources_rejects_limit_below_one() -> None:
    with pytest.raises(ValueError):
        configure_resources({"network": 0})
