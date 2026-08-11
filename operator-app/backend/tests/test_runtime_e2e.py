# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from picogrid_ecn_client import (
    AuthorizationError,
    ConnectionStatus,
    DeliveryPhase,
    ECNClient,
    ECNClientError,
    OutcomeUnknownError,
    ResourceLimitError,
)
from picogrid_ecn_client import TimeoutError as ECNTimeoutError
from picogrid_ecn_client.interfaces import Entities, Locations

import operator_app.runtime as runtime_module
from operator_app.api_models import PrepareTaskRequest
from operator_app.app import create_app
from operator_app.runtime import OperatorActionError, OperatorCleanupError, OperatorRuntime
from operator_app.settings import OperatorSettings

_APPLICATION_ROOT = Path(__file__).resolve().parents[2]
_BROWSER_VIEW_ID = UUID("00000000-0000-4000-8000-000000000101")
_OTHER_BROWSER_VIEW_ID = UUID("00000000-0000-4000-8000-000000000102")
_MOCK_TARGET_ID = UUID("00000000-0000-4000-8000-000000000201")
_BROWSER_VIEW_GENERATION = UUID("00000000-0000-4000-8000-000000000301")
_NEXT_BROWSER_VIEW_GENERATION = UUID("00000000-0000-4000-8000-000000000302")
_OTHER_BROWSER_VIEW_GENERATION = UUID("00000000-0000-4000-8000-000000000303")


def _settings(root: Path, *, tasking: bool) -> OperatorSettings:
    environment = {
        "OPERATOR_MODE": "mock",
        "OPERATOR_ECN_CLIENT_INTEGRATION": "operator-console",
        "OPERATOR_ECN_INTEGRATION_ALLOWLIST": "mock-sensor,mock-target",
        "OPERATOR_ECN_CATEGORY_ALLOWLIST": "TRACK,DETECTION,DEVICE",
        "OPERATOR_ECN_WIRE_FORMAT": "json",
        "OPERATOR_TASKING_ENABLED": str(tasking).lower(),
        "OPERATOR_COMMANDS_FILE": "config/commands.example.json",
        "OPERATOR_SYNTHETIC_PERIOD_SECONDS": "0.1",
        "OPERATOR_STALE_AFTER_SECONDS": "2",
    }
    if tasking:
        environment["OPERATOR_TASK_ENTITY_ALLOWLIST"] = str(_MOCK_TARGET_ID)
    return OperatorSettings.from_env(environment, application_root=root)


async def _wait_for_entities(runtime: OperatorRuntime, count: int = 4) -> None:
    for _attempt in range(100):
        if len((await runtime.snapshot()).entities) >= count:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("mock entities were not observed before the deadline")


@pytest.mark.asyncio
async def test_mock_runtime_observes_state_dispatches_once_and_cleans_up(
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=True))
    await runtime.start()
    mock = runtime._mock
    connection_stream = runtime._connection_stream
    assert mock is not None
    assert connection_stream is not None
    try:
        await _wait_for_entities(runtime)
        snapshot = await runtime.snapshot()
        assert {item.category for item in snapshot.entities} >= {
            "TRACK",
            "DETECTION",
            "DEVICE",
            None,
        }
        assert all(item.entity_id.version == 4 for item in snapshot.entities)
        assert any(item.location_only for item in snapshot.entities)
        assert snapshot.health is not None
        assert snapshot.health.entity_scope_pairs == 6
        assert snapshot.health.location_scope_filters == 4
        assert snapshot.health.entity_decode_errors == 0
        assert snapshot.health.location_decode_errors == 0
        target = next(item for item in snapshot.entities if item.type == "synthetic-task-target")
        assert target.entity_id == _MOCK_TARGET_ID

        prepared = await runtime.prepare_task(
            PrepareTaskRequest(
                entity_id=target.entity_id,
                integration=target.integration,
                command="echo",
                payload={"message": "synthetic request"},
            )
        )
        outcome = await runtime.confirm_task(prepared.preparation_token)

        assert outcome.command == "echo"
        assert outcome.mode == "complete"
        assert outcome.status == "SUCCESS"

        acknowledged = await runtime.prepare_task(
            PrepareTaskRequest(
                entity_id=target.entity_id,
                integration=target.integration,
                command="echo_ack",
                payload={"message": "synthetic acknowledgment"},
            )
        )
        acknowledgment = await runtime.confirm_task(acknowledged.preparation_token)
        assert acknowledgment.mode == "acknowledgment"
        assert acknowledgment.status == "ACK"
        await asyncio.sleep(0.2)
        statuses = [item.status for item in (await runtime.snapshot()).task_outcomes]
        assert statuses.count("ACK") == 1
        assert statuses.count("SUCCESS") == 1
        with pytest.raises(OperatorActionError, match="invalid or expired"):
            await runtime.confirm_task(prepared.preparation_token)
    finally:
        await runtime.stop()

    assert mock.active_connection_count == 0
    assert mock.active_task_count == 0
    assert connection_stream.closed
    stopped = await runtime.snapshot()
    assert stopped.entities == ()
    assert stopped.connection is None


@pytest.mark.asyncio
async def test_task_api_rejects_each_non_allowlisted_dimension_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    application = create_app(
        _settings(_APPLICATION_ROOT, tasking=True), application_root=_APPLICATION_ROOT
    )
    mock = None
    async with application.router.lifespan_context(application):
        runtime = application.state.operator_runtime
        mock = runtime._mock
        assert mock is not None
        await _wait_for_entities(runtime)
        snapshot = await runtime.snapshot()
        target = next(item for item in snapshot.entities if item.type == "synthetic-task-target")
        other = next(item for item in snapshot.entities if item.type == "synthetic-track")
        assert target.entity_id == _MOCK_TARGET_ID
        assert runtime.client is not None
        dispatches = 0

        async def count_dispatch(*_args: object, **_kwargs: object) -> None:
            nonlocal dispatches
            dispatches += 1

        monkeypatch.setattr(type(runtime.client.tasks), "send", count_dispatch)
        assert (
            await runtime.activate_browser_view(
                _BROWSER_VIEW_ID,
                _BROWSER_VIEW_GENERATION,
            )
            is True
        )
        headers = {
            "Origin": "http://127.0.0.1:4173",
            "X-Operator-Intent": "prepare",
            "X-Operator-View": str(_BROWSER_VIEW_ID),
            "X-Operator-View-Generation": str(_BROWSER_VIEW_GENERATION),
        }
        cases = (
            (
                {
                    "entity_id": str(target.entity_id),
                    "integration": "unlisted-integration",
                    "command": "echo",
                    "payload": {"message": "synthetic"},
                },
                403,
                "target integration is not allowlisted",
            ),
            (
                {
                    "entity_id": str(target.entity_id),
                    "integration": target.integration,
                    "command": "unlisted-command",
                    "payload": {"message": "synthetic"},
                },
                422,
                "command is not present in the operator allowlist",
            ),
            (
                {
                    "entity_id": str(other.entity_id),
                    "integration": other.integration,
                    "command": "echo",
                    "payload": {"message": "synthetic"},
                },
                403,
                "target entity UUID is not allowlisted",
            ),
        )

        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            for body, status_code, detail in cases:
                response = await client.post("/api/tasks/prepare", headers=headers, json=body)
                assert response.status_code == status_code
                assert response.json() == {"detail": detail}

        assert dispatches == 0
        assert runtime._prepared == {}

    assert mock is not None
    assert mock.active_connection_count == 0
    assert mock.active_task_count == 0


@pytest.mark.asyncio
async def test_reconnect_generation_discards_prepared_task_without_publication(
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=True))
    await runtime.start()
    release_connection_event = asyncio.Event()
    try:
        await _wait_for_entities(runtime)
        mock = runtime._mock
        assert mock is not None
        target = next(
            item
            for item in (await runtime.snapshot()).entities
            if item.type == "synthetic-task-target"
        )
        prepared = await runtime.prepare_task(
            PrepareTaskRequest(
                entity_id=target.entity_id,
                integration=target.integration,
                command="echo",
                payload={"message": "must not cross reconnect"},
            )
        )
        assert runtime.client is not None
        client = runtime.client
        prepared_generation = client.status.connection_generation
        assert runtime._prepared[prepared.preparation_token].connection_generation == (
            prepared_generation
        )
        sends = 0

        async def count_send(*_args: object, **_kwargs: object) -> None:
            nonlocal sends
            sends += 1

        monkeypatch.setattr(type(client.tasks), "send", count_send)
        original_apply = runtime._apply_connection_status
        reconnect_event_consumed = asyncio.Event()

        async def delay_first_reconnect_event(
            status: ConnectionStatus,
            *,
            previous_ready: bool | None,
            previous_generation: int | None,
            previous_summary: str | None,
        ) -> None:
            if not status.ready and not reconnect_event_consumed.is_set():
                reconnect_event_consumed.set()
                await release_connection_event.wait()
            await original_apply(
                status,
                previous_ready=previous_ready,
                previous_generation=previous_generation,
                previous_summary=previous_summary,
            )

        monkeypatch.setattr(runtime, "_apply_connection_status", delay_first_reconnect_event)
        await mock.disconnect_clients()
        await asyncio.wait_for(reconnect_event_consumed.wait(), timeout=2)
        ready = await client.wait_until_ready(timeout=3)
        assert ready.connection_generation > prepared_generation
        await asyncio.gather(
            *(
                mock_client.wait_until_ready(timeout=3)
                for mock_client in runtime._mock_clients.values()
            )
        )

        # The newer READY event is queued behind the deliberately paused operator
        # consumer, so operator state still reflects the preparation generation.
        stale_operator_state = await runtime.snapshot()
        assert stale_operator_state.connection is not None
        assert stale_operator_state.connection.connection_generation == prepared_generation

        with pytest.raises(OperatorActionError, match="connection changed") as captured:
            await runtime.confirm_task(prepared.preparation_token)
        assert captured.value.outcome_status == "RECONNECT"
        assert sends == 0
        assert any(
            item.code == "task_generation_changed"
            for item in (await runtime.snapshot()).diagnostics
        )
        assert any(item.status == "RECONNECT" for item in (await runtime.snapshot()).task_outcomes)
    finally:
        release_connection_event.set()
        await runtime.stop()


@pytest.mark.asyncio
async def test_reconnect_after_confirmation_validation_cannot_publish_on_new_generation(
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=True))
    await runtime.start()
    release_send = asyncio.Event()
    confirming: asyncio.Task[object] | None = None
    try:
        await _wait_for_entities(runtime)
        mock = runtime._mock
        client = runtime.client
        assert mock is not None
        assert client is not None
        target = next(
            item
            for item in (await runtime.snapshot()).entities
            if item.type == "synthetic-task-target"
        )
        prepared = await runtime.prepare_task(
            PrepareTaskRequest(
                entity_id=target.entity_id,
                integration=target.integration,
                command="echo",
                payload={"message": "must remain bound to the prepared connection"},
            )
        )
        prepared_generation = client.status.connection_generation
        mock_client_generations = {
            integration: mock_client.status.connection_generation
            for integration, mock_client in runtime._mock_clients.items()
        }
        task_topic = f"task/{target.integration}/{target.entity_id}/echo"
        task_publications: list[bytes] = []
        original_mqtt_published = mock.mqtt_published

        async def record_mqtt_publication(
            identity: Any,
            topic: str,
            payload: bytes,
        ) -> Any:
            if topic == task_topic:
                task_publications.append(payload)
            return await original_mqtt_published(identity, topic, payload)

        monkeypatch.setattr(mock, "mqtt_published", record_mqtt_publication)
        send_entered = asyncio.Event()
        observed_expected_generations: list[int | None] = []
        original_send = type(client.tasks).send

        async def pause_before_sdk_send(
            tasks: Any,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            observed_expected_generations.append(kwargs.get("expected_connection_generation"))
            send_entered.set()
            await release_send.wait()
            return await original_send(tasks, *args, **kwargs)

        monkeypatch.setattr(type(client.tasks), "send", pause_before_sdk_send)
        confirming = asyncio.create_task(runtime.confirm_task(prepared.preparation_token))
        await asyncio.wait_for(send_entered.wait(), timeout=1)

        await mock.disconnect_clients()
        for _attempt in range(150):
            if (
                client.is_ready
                and client.status.connection_generation > prepared_generation
                and all(
                    mock_client.is_ready
                    and mock_client.status.connection_generation
                    > mock_client_generations[integration]
                    for integration, mock_client in runtime._mock_clients.items()
                )
            ):
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("mock clients did not complete the forced reconnect")
        release_send.set()

        with pytest.raises(OperatorActionError, match="connection changed") as captured:
            await confirming
        assert captured.value.status_code == 409
        assert captured.value.outcome_status == "RECONNECT"
        assert observed_expected_generations == [prepared_generation]
        assert task_publications == []
        snapshot = await runtime.snapshot()
        assert any(item.code == "task_generation_changed" for item in snapshot.diagnostics)
        assert any(item.status == "RECONNECT" for item in snapshot.task_outcomes)
    finally:
        release_send.set()
        if confirming is not None and not confirming.done():
            confirming.cancel()
            await asyncio.gather(confirming, return_exceptions=True)
        await runtime.stop()


@pytest.mark.asyncio
async def test_normal_reconnect_event_discards_prepared_task_without_publication(
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=True))
    await runtime.start()
    try:
        await _wait_for_entities(runtime)
        mock = runtime._mock
        client = runtime.client
        assert mock is not None
        assert client is not None
        target = next(
            item
            for item in (await runtime.snapshot()).entities
            if item.type == "synthetic-task-target"
        )
        prepared = await runtime.prepare_task(
            PrepareTaskRequest(
                entity_id=target.entity_id,
                integration=target.integration,
                command="echo",
                payload={"message": "must be discarded on reconnect"},
            )
        )
        prepared_generation = client.status.connection_generation
        sends = 0

        async def count_send(*_args: object, **_kwargs: object) -> None:
            nonlocal sends
            sends += 1

        monkeypatch.setattr(type(client.tasks), "send", count_send)
        await mock.disconnect_clients()

        for _attempt in range(100):
            snapshot = await runtime.snapshot()
            invalidated = any(
                item.code == "task_preparations_invalidated" for item in snapshot.diagnostics
            )
            if invalidated and prepared.preparation_token not in runtime._prepared:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("normal reconnect event did not invalidate the prepared task")

        ready = await client.wait_until_ready(timeout=3)
        assert ready.connection_generation > prepared_generation
        await asyncio.gather(
            *(
                mock_client.wait_until_ready(timeout=3)
                for mock_client in runtime._mock_clients.values()
            )
        )
        with pytest.raises(OperatorActionError, match="invalid or expired"):
            await runtime.confirm_task(prepared.preparation_token)
        assert sends == 0
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_connection_stream_terminal_error_is_redacted_and_not_a_cleanup_failure(
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=False))
    await runtime.start()
    try:
        stream = runtime._connection_stream
        assert stream is not None
        await stream._fail(
            ECNClientError(
                "connection-secret-canary",
                code="synthetic-secret-bearing-code",
                operation="connection.events",
            )
        )

        for _attempt in range(50):
            snapshot = await runtime.snapshot()
            terminal_diagnostics = [
                item for item in snapshot.diagnostics if item.code == "connection_observer_stopped"
            ]
            if terminal_diagnostics:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("connection observer terminal state was not surfaced")

        assert len(terminal_diagnostics) == 1
        assert terminal_diagnostics[0].level == "error"
        assert terminal_diagnostics[0].message == "ECN connection observer stopped"
        serialized = snapshot.model_dump_json()
        assert "connection-secret-canary" not in serialized
        assert "synthetic-secret-bearing-code" not in serialized

        await runtime.stop()
    finally:
        if runtime.running:
            await runtime.stop()


@pytest.mark.asyncio
async def test_normal_connection_stream_exhaustion_transitions_to_reconnecting(
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=False))
    await runtime.start()
    try:
        stream = runtime._connection_stream
        assert stream is not None
        await stream.aclose()

        for _attempt in range(50):
            snapshot = await runtime.snapshot()
            if snapshot.connection_summary == "reconnecting":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("normal connection observer exhaustion was not surfaced")

        assert any(item.code == "connection_observer_stopped" for item in snapshot.diagnostics)
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_type", "error_type", "expected_summary", "watcher_task"),
    [
        (
            Entities,
            AuthorizationError,
            "subscription denied",
            "operator-entity-watch",
        ),
        (
            Locations,
            ResourceLimitError,
            "subscription resource-limited",
            "operator-location-watch",
        ),
    ],
)
async def test_initial_subscription_denial_keeps_the_runtime_and_ui_available(
    service_type: type[Entities | Locations],
    error_type: type[AuthorizationError | ResourceLimitError],
    expected_summary: str,
    watcher_task: str,
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    async def deny_subscription(
        _service: Entities | Locations,
        **_kwargs: object,
    ) -> None:
        raise error_type(
            "initial-subscription-secret-canary",
            operation="mqtt.subscribe",
        )

    monkeypatch.setattr(service_type, "watch", deny_subscription)
    application = create_app(
        _settings(_APPLICATION_ROOT, tasking=True),
        application_root=_APPLICATION_ROOT,
    )

    async with application.router.lifespan_context(application):
        runtime = application.state.operator_runtime
        assert runtime.running
        assert all(task.get_name() != watcher_task for task in runtime._tasks)

        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            assert (await client.get("/")).status_code == 200
            response = await client.get("/api/state")

        assert response.status_code == 200
        state = response.json()
        assert state["connection_summary"] == expected_summary
        assert "initial-subscription-secret-canary" not in response.text


@pytest.mark.asyncio
async def test_prepare_not_ready_503_is_marked_as_reconnect(
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=True))
    await runtime.start()
    try:
        await _wait_for_entities(runtime)
        target = next(
            item
            for item in (await runtime.snapshot()).entities
            if item.type == "synthetic-task-target"
        )
        assert runtime.client is not None
        monkeypatch.setattr(type(runtime.client), "is_ready", property(lambda _client: False))

        with pytest.raises(OperatorActionError, match="observers are not ready") as captured:
            await runtime.prepare_task(
                PrepareTaskRequest(
                    entity_id=target.entity_id,
                    integration=target.integration,
                    command="echo",
                    payload={"message": "requires reconnect"},
                )
            )

        assert captured.value.status_code == 503
        assert captured.value.outcome_status == "RECONNECT"
    finally:
        monkeypatch.undo()
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream_attribute", "error_type", "expected_summary", "failed_health_attribute"),
    [
        (
            "_entity_stream",
            AuthorizationError,
            "subscription denied",
            "entity_watcher_active",
        ),
        (
            "_location_stream",
            ResourceLimitError,
            "subscription resource-limited",
            "location_watcher_active",
        ),
    ],
)
async def test_isolated_essential_watcher_failure_overrides_ready_and_disables_tasking(
    stream_attribute: str,
    error_type: type[AuthorizationError | ResourceLimitError],
    expected_summary: str,
    failed_health_attribute: str,
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=True))
    await runtime.start()
    try:
        await _wait_for_entities(runtime)
        target = next(
            item
            for item in (await runtime.snapshot()).entities
            if item.type == "synthetic-task-target"
        )
        stream = getattr(runtime, stream_attribute)
        assert stream is not None
        sends = 0

        async def count_send(*_args: object, **_kwargs: object) -> None:
            nonlocal sends
            sends += 1

        assert runtime.client is not None
        monkeypatch.setattr(type(runtime.client.tasks), "send", count_send)
        await stream._fail(
            error_type(
                "synthetic broker detail that must not be exposed",
                operation="mqtt.restore_subscription",
            )
        )
        for _attempt in range(50):
            snapshot = await runtime.snapshot()
            if snapshot.connection_summary == expected_summary:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("watcher terminal state was not surfaced")

        assert snapshot.connection is not None
        assert snapshot.connection.ready
        assert snapshot.health is not None
        assert getattr(snapshot.health, failed_health_attribute) is False
        assert "synthetic broker detail" not in snapshot.model_dump_json()
        with pytest.raises(OperatorActionError, match="observers are not ready"):
            await runtime.prepare_task(
                PrepareTaskRequest(
                    entity_id=target.entity_id,
                    integration=target.integration,
                    command="echo",
                    payload={"message": "must not publish"},
                )
            )
        assert sends == 0
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_reports_malformed_entity_and_location_payloads(
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=False))
    await runtime.start()
    mock = runtime._mock
    assert mock is not None
    try:
        await _wait_for_entities(runtime)
        mock.malform_next_messages(5)
        for _attempt in range(100):
            health = (await runtime.snapshot()).health
            if (
                health is not None
                and health.entity_decode_errors >= 3
                and health.location_decode_errors >= 2
            ):
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("watcher decode counters did not observe malformed mock payloads")

        for _attempt in range(40):
            snapshot = await runtime.snapshot()
            if any(item.code == "payload_decode_error" for item in snapshot.diagnostics):
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("decode-error warning was not emitted")
    finally:
        await runtime.stop()

    assert mock.active_connection_count == 0
    assert mock.active_task_count == 0


@pytest.mark.asyncio
async def test_task_timeout_is_persisted_as_a_bounded_outcome(
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=True))
    await runtime.start()
    try:
        await _wait_for_entities(runtime)
        target = next(
            item
            for item in (await runtime.snapshot()).entities
            if item.type == "synthetic-task-target"
        )
        prepared = await runtime.prepare_task(
            PrepareTaskRequest(
                entity_id=target.entity_id,
                integration=target.integration,
                command="echo",
                payload={"message": "synthetic timeout"},
            )
        )
        assert runtime.client is not None
        send_calls = 0

        async def time_out(*_args: object, **_kwargs: object) -> None:
            nonlocal send_calls
            send_calls += 1
            raise ECNTimeoutError("synthetic timeout", operation="task.send")

        monkeypatch.setattr(type(runtime.client.tasks), "send", time_out)
        with pytest.raises(OperatorActionError, match="timed out") as captured:
            await runtime.confirm_task(prepared.preparation_token)
        assert send_calls == 1
        assert captured.value.status_code == 504
        outcome = (await runtime.snapshot()).task_outcomes[0]
        assert outcome.status == "TIMEOUT"
        assert outcome.mode == "complete"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [
        DeliveryPhase.LOCAL_SEND_UNCERTAIN,
        DeliveryPhase.BROKER_ACKNOWLEDGMENT_PENDING,
        DeliveryPhase.RESPONSE_PENDING,
    ],
)
async def test_task_outcome_unknown_retains_correlation_without_retry(
    phase: DeliveryPhase,
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=True))
    await runtime.start()
    try:
        await _wait_for_entities(runtime)
        target = next(
            item
            for item in (await runtime.snapshot()).entities
            if item.type == "synthetic-task-target"
        )
        prepared = await runtime.prepare_task(
            PrepareTaskRequest(
                entity_id=target.entity_id,
                integration=target.integration,
                command="echo",
                payload={"message": "synthetic uncertain outcome"},
            )
        )
        assert runtime.client is not None
        send_calls = 0

        async def lose_outcome(*_args: object, **_kwargs: object) -> None:
            nonlocal send_calls
            send_calls += 1
            raise OutcomeUnknownError(
                "synthetic uncertain outcome",
                delivery_phase=phase,
                operation="task.send",
                task_id="synthetic-task-correlation",
            )

        monkeypatch.setattr(type(runtime.client.tasks), "send", lose_outcome)
        with pytest.raises(OperatorActionError, match="task outcome is unknown") as captured:
            await runtime.confirm_task(prepared.preparation_token)

        assert send_calls == 1
        assert captured.value.status_code == 409
        assert captured.value.outcome_status == "OUTCOME_UNKNOWN"
        assert "synthetic-task-correlation" in str(captured.value)
        assert phase.value in str(captured.value)
        snapshot = await runtime.snapshot()
        outcome = snapshot.task_outcomes[0]
        assert outcome.status == "OUTCOME_UNKNOWN"
        assert outcome.task_id == "synthetic-task-correlation"
        assert phase.value in outcome.detail
        assert any(item.code == "outcome_unknown" for item in snapshot.diagnostics)
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_total_confirmation_deadline_cancels_send_but_preserves_typed_uncertainty(
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=True))
    monkeypatch.setattr(
        runtime_module,
        "_TASK_CONFIRMATION_DEADLINE_SECONDS",
        0.02,
        raising=False,
    )
    never_release = asyncio.Event()
    await runtime.start()
    try:
        await _wait_for_entities(runtime)
        target = next(
            item
            for item in (await runtime.snapshot()).entities
            if item.type == "synthetic-task-target"
        )

        async def prepare(message: str) -> str:
            prepared = await runtime.prepare_task(
                PrepareTaskRequest(
                    entity_id=target.entity_id,
                    integration=target.integration,
                    command="echo",
                    payload={"message": message},
                )
            )
            return prepared.preparation_token

        assert runtime.client is not None
        entered_send = asyncio.Event()
        send_cancelled = asyncio.Event()

        async def block_forever(*_args: object, **_kwargs: object) -> None:
            entered_send.set()
            try:
                await never_release.wait()
            finally:
                send_cancelled.set()

        monkeypatch.setattr(type(runtime.client.tasks), "send", block_forever)
        blocked_token = await prepare("bounded confirmation deadline")
        with pytest.raises(OperatorActionError, match="timed out") as captured:
            await asyncio.wait_for(runtime.confirm_task(blocked_token), timeout=0.5)

        assert entered_send.is_set()
        assert send_cancelled.is_set()
        assert captured.value.status_code == 504
        assert captured.value.outcome_status == "TIMEOUT"
        definite = (await runtime.snapshot()).task_outcomes[0]
        assert definite.status == "TIMEOUT"
        assert definite.task_id is None
        assert "nothing was published" in definite.detail

        uncertainty_cancelled = asyncio.Event()

        async def become_uncertain(*_args: object, **_kwargs: object) -> None:
            try:
                await never_release.wait()
            except asyncio.CancelledError:
                uncertainty_cancelled.set()
                raise OutcomeUnknownError(
                    "typed uncertainty after cancellation",
                    delivery_phase=DeliveryPhase.LOCAL_SEND_UNCERTAIN,
                    operation="task.send",
                    task_id="deadline-uncertain-correlation",
                ) from None

        monkeypatch.setattr(type(runtime.client.tasks), "send", become_uncertain)
        uncertain_token = await prepare("deadline uncertainty")
        with pytest.raises(OperatorActionError, match="task outcome is unknown") as uncertain:
            await asyncio.wait_for(runtime.confirm_task(uncertain_token), timeout=0.5)

        assert uncertainty_cancelled.is_set()
        assert uncertain.value.status_code == 409
        assert uncertain.value.outcome_status == "OUTCOME_UNKNOWN"
        snapshot = await runtime.snapshot()
        uncertain_outcome = next(
            item
            for item in snapshot.task_outcomes
            if item.task_id == "deadline-uncertain-correlation"
        )
        assert uncertain_outcome.status == "OUTCOME_UNKNOWN"
    finally:
        never_release.set()
        await runtime.stop()


@pytest.mark.asyncio
async def test_task_cancellation_and_reconnect_are_persisted_distinctly(
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=True))
    await runtime.start()
    try:
        await _wait_for_entities(runtime)
        target = next(
            item
            for item in (await runtime.snapshot()).entities
            if item.type == "synthetic-task-target"
        )
        cancelled_preparation = await runtime.prepare_task(
            PrepareTaskRequest(
                entity_id=target.entity_id,
                integration=target.integration,
                command="echo",
                payload={"message": "synthetic cancellation"},
            )
        )
        assert runtime.client is not None
        started = asyncio.Event()

        async def block(*_args: object, **_kwargs: object) -> None:
            started.set()
            await asyncio.Event().wait()

        with monkeypatch.context() as patch:
            patch.setattr(type(runtime.client.tasks), "send", block)
            dispatch = asyncio.create_task(
                runtime.confirm_task(cancelled_preparation.preparation_token)
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            dispatch.cancel()
            with pytest.raises(asyncio.CancelledError):
                await dispatch

        reconnect_preparation = await runtime.prepare_task(
            PrepareTaskRequest(
                entity_id=target.entity_id,
                integration=target.integration,
                command="echo",
                payload={"message": "synthetic reconnect"},
            )
        )
        with monkeypatch.context() as patch:
            patch.setattr(type(runtime.client), "is_ready", property(lambda _client: False))
            with pytest.raises(OperatorActionError, match="not ready") as captured:
                await runtime.confirm_task(reconnect_preparation.preparation_token)
            assert captured.value.status_code == 503

        statuses = [item.status for item in (await runtime.snapshot()).task_outcomes]
        assert statuses[:2] == ["RECONNECT", "CANCELLED"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_discarded_preparation_cannot_publish_via_direct_confirm(
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=True))
    await runtime.start()
    try:
        await _wait_for_entities(runtime)
        target = next(
            item
            for item in (await runtime.snapshot()).entities
            if item.type == "synthetic-task-target"
        )
        prepared = await runtime.prepare_task(
            PrepareTaskRequest(
                entity_id=target.entity_id,
                integration=target.integration,
                command="echo",
                payload={"message": "discard without publication"},
            )
        )
        assert await runtime.discard_prepared_task(prepared.preparation_token) == 1
        # A lost successful response can retry only because the runtime retains a
        # bounded proof that this exact token was invalidated before publication.
        assert await runtime.discard_prepared_task(prepared.preparation_token) == 1
        assert runtime.client is not None
        sends = 0

        async def count_send(*_args: object, **_kwargs: object) -> None:
            nonlocal sends
            sends += 1

        monkeypatch.setattr(type(runtime.client.tasks), "send", count_send)
        for _attempt in range(2):
            with pytest.raises(OperatorActionError, match="invalid or expired") as error:
                await runtime.confirm_task(prepared.preparation_token)
            assert error.value.status_code == 409
        assert sends == 0
        assert (await runtime.snapshot()).task_outcomes == ()
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_browser_view_loss_invalidates_preparation(
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=True))
    await runtime.start()
    try:
        await _wait_for_entities(runtime)
        target = next(
            item
            for item in (await runtime.snapshot()).entities
            if item.type == "synthetic-task-target"
        )
        assert (
            await runtime.activate_browser_view(
                _BROWSER_VIEW_ID,
                _BROWSER_VIEW_GENERATION,
            )
            is True
        )
        prepared = await runtime.prepare_task(
            PrepareTaskRequest(
                entity_id=target.entity_id,
                integration=target.integration,
                command="echo",
                payload={"message": "view loss before confirmation"},
            ),
            view_id=_BROWSER_VIEW_ID,
            view_generation=_BROWSER_VIEW_GENERATION,
        )
        assert (
            await runtime.deactivate_browser_view(
                _BROWSER_VIEW_ID,
                _BROWSER_VIEW_GENERATION,
            )
            == 1
        )
        assert runtime.client is not None
        sends = 0

        async def count_send(*_args: object, **_kwargs: object) -> None:
            nonlocal sends
            sends += 1

        monkeypatch.setattr(type(runtime.client.tasks), "send", count_send)
        with pytest.raises(OperatorActionError, match="browser view is disconnected") as error:
            await runtime.confirm_task(
                prepared.preparation_token,
                view_id=_BROWSER_VIEW_ID,
                view_generation=_BROWSER_VIEW_GENERATION,
            )
        assert error.value.status_code == 409
        assert sends == 0
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_duplicate_browser_view_identity_is_refused(
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=True))
    await runtime.start()
    try:
        assert (
            await runtime.activate_browser_view(
                _BROWSER_VIEW_ID,
                _BROWSER_VIEW_GENERATION,
            )
            is True
        )
        assert (
            await runtime.activate_browser_view(
                _BROWSER_VIEW_ID,
                _NEXT_BROWSER_VIEW_GENERATION,
            )
            is False
        )
        assert (
            await runtime.activate_browser_view(
                _OTHER_BROWSER_VIEW_ID,
                _OTHER_BROWSER_VIEW_GENERATION,
            )
            is True
        )
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_view_retirement_requires_bounded_exact_generation_proof(
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(
        replace(
            _settings(_APPLICATION_ROOT, tasking=True),
            maximum_browser_clients=2,
        )
    )
    await runtime.start()
    try:
        view_ids = tuple(UUID(int=100 + index) for index in range(3))
        generations = tuple(UUID(int=200 + index) for index in range(3))
        for view_id, generation in zip(view_ids, generations, strict=True):
            assert await runtime.activate_browser_view(view_id, generation) is True
            assert await runtime.retire_browser_view(view_id, generation) == 0

        assert await runtime.retire_browser_view(view_ids[-1], generations[-1]) == 0
        with pytest.raises(OperatorActionError, match="not proven"):
            await runtime.retire_browser_view(view_ids[-1], UUID(int=999))
        with pytest.raises(OperatorActionError, match="not proven"):
            await runtime.retire_browser_view(UUID(int=998), UUID(int=997))
        with pytest.raises(OperatorActionError, match="not proven"):
            await runtime.retire_browser_view(view_ids[0], generations[0])
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_retired_view_proves_late_preparation_invalidation(
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=True))
    await runtime.start()
    try:
        await _wait_for_entities(runtime)
        target = next(
            item
            for item in (await runtime.snapshot()).entities
            if item.type == "synthetic-task-target"
        )
        assert (
            await runtime.activate_browser_view(
                _BROWSER_VIEW_ID,
                _BROWSER_VIEW_GENERATION,
            )
            is True
        )
        prepared = await runtime.prepare_task(
            PrepareTaskRequest(
                entity_id=target.entity_id,
                integration=target.integration,
                command="echo",
                payload={"message": "retire before response delivery"},
            ),
            view_id=_BROWSER_VIEW_ID,
            view_generation=_BROWSER_VIEW_GENERATION,
        )

        assert (
            await runtime.deactivate_browser_view(
                _BROWSER_VIEW_ID,
                _BROWSER_VIEW_GENERATION,
            )
            == 1
        )
        assert (
            await runtime.discard_prepared_task(
                prepared.preparation_token,
                view_id=_BROWSER_VIEW_ID,
                view_generation=_BROWSER_VIEW_GENERATION,
            )
            == 1
        )
        assert (
            await runtime.discard_prepared_task(
                prepared.preparation_token,
                view_id=_OTHER_BROWSER_VIEW_ID,
                view_generation=_OTHER_BROWSER_VIEW_GENERATION,
            )
            == 0
        )
        with pytest.raises(OperatorActionError, match="disconnected") as error:
            await runtime.confirm_task(
                prepared.preparation_token,
                view_id=_BROWSER_VIEW_ID,
                view_generation=_BROWSER_VIEW_GENERATION,
            )
        assert error.value.status_code == 409
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_reactivated_browser_view_cannot_resurrect_discarded_preparation(
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=True))
    await runtime.start()
    try:
        await _wait_for_entities(runtime)
        target = next(
            item
            for item in (await runtime.snapshot()).entities
            if item.type == "synthetic-task-target"
        )
        assert (
            await runtime.activate_browser_view(
                _BROWSER_VIEW_ID,
                _BROWSER_VIEW_GENERATION,
            )
            is True
        )
        prepared = await runtime.prepare_task(
            PrepareTaskRequest(
                entity_id=target.entity_id,
                integration=target.integration,
                command="echo",
                payload={"message": "discard before view identity reuse"},
            ),
            view_id=_BROWSER_VIEW_ID,
            view_generation=_BROWSER_VIEW_GENERATION,
        )
        assert (
            await runtime.deactivate_browser_view(
                _BROWSER_VIEW_ID,
                _BROWSER_VIEW_GENERATION,
            )
            == 1
        )
        assert (
            await runtime.activate_browser_view(
                _BROWSER_VIEW_ID,
                _NEXT_BROWSER_VIEW_GENERATION,
            )
            is True
        )
        assert runtime.client is not None
        sends = 0

        async def count_send(*_args: object, **_kwargs: object) -> None:
            nonlocal sends
            sends += 1

        monkeypatch.setattr(type(runtime.client.tasks), "send", count_send)
        with pytest.raises(OperatorActionError, match="invalid or expired") as error:
            await runtime.confirm_task(
                prepared.preparation_token,
                view_id=_BROWSER_VIEW_ID,
                view_generation=_NEXT_BROWSER_VIEW_GENERATION,
            )
        assert error.value.status_code == 409
        assert sends == 0
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_reactivation_waits_for_prior_generation_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=True))
    release_confirmation = asyncio.Event()
    await runtime.start()
    try:
        await _wait_for_entities(runtime)
        target = next(
            item
            for item in (await runtime.snapshot()).entities
            if item.type == "synthetic-task-target"
        )
        assert (
            await runtime.activate_browser_view(
                _BROWSER_VIEW_ID,
                _BROWSER_VIEW_GENERATION,
            )
            is True
        )
        prepared = await runtime.prepare_task(
            PrepareTaskRequest(
                entity_id=target.entity_id,
                integration=target.integration,
                command="echo",
                payload={"message": "hold successor until confirmation settles"},
            ),
            view_id=_BROWSER_VIEW_ID,
            view_generation=_BROWSER_VIEW_GENERATION,
        )
        assert runtime.client is not None
        entered_confirmation = asyncio.Event()
        task_service = type(runtime.client.tasks)
        original_send = task_service.send

        async def delayed_send(service: object, *args: object, **kwargs: object) -> object:
            entered_confirmation.set()
            await release_confirmation.wait()
            return await original_send(service, *args, **kwargs)

        monkeypatch.setattr(task_service, "send", delayed_send)
        confirming = asyncio.create_task(
            runtime.confirm_task(
                prepared.preparation_token,
                view_id=_BROWSER_VIEW_ID,
                view_generation=_BROWSER_VIEW_GENERATION,
            )
        )
        await asyncio.wait_for(entered_confirmation.wait(), timeout=1)

        assert (
            await runtime.deactivate_browser_view(
                _BROWSER_VIEW_ID,
                _BROWSER_VIEW_GENERATION,
            )
            == 0
        )
        assert (
            await runtime.activate_browser_view(
                _BROWSER_VIEW_ID,
                _NEXT_BROWSER_VIEW_GENERATION,
            )
            is False
        )

        release_confirmation.set()
        outcome = await asyncio.wait_for(confirming, timeout=2)
        assert outcome.status == "SUCCESS"
        assert (
            await runtime.activate_browser_view(
                _BROWSER_VIEW_ID,
                _NEXT_BROWSER_VIEW_GENERATION,
            )
            is True
        )
    finally:
        release_confirmation.set()
        await runtime.stop()


@pytest.mark.asyncio
async def test_late_disconnect_discards_only_the_owning_browser_view(
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=True))
    await runtime.start()
    try:
        await _wait_for_entities(runtime)
        target = next(
            item
            for item in (await runtime.snapshot()).entities
            if item.type == "synthetic-task-target"
        )
        request = PrepareTaskRequest(
            entity_id=target.entity_id,
            integration=target.integration,
            command="echo",
            payload={"message": "browser view ownership"},
        )
        assert (
            await runtime.activate_browser_view(
                _BROWSER_VIEW_ID,
                _BROWSER_VIEW_GENERATION,
            )
            is True
        )
        assert (
            await runtime.activate_browser_view(
                _OTHER_BROWSER_VIEW_ID,
                _OTHER_BROWSER_VIEW_GENERATION,
            )
            is True
        )
        old_view = await runtime.prepare_task(
            request,
            view_id=_BROWSER_VIEW_ID,
            view_generation=_BROWSER_VIEW_GENERATION,
        )
        new_view = await runtime.prepare_task(
            request,
            view_id=_OTHER_BROWSER_VIEW_ID,
            view_generation=_OTHER_BROWSER_VIEW_GENERATION,
        )

        assert (
            await runtime.deactivate_browser_view(
                _BROWSER_VIEW_ID,
                _BROWSER_VIEW_GENERATION,
            )
            == 1
        )
        with pytest.raises(OperatorActionError, match="browser view is disconnected"):
            await runtime.confirm_task(
                old_view.preparation_token,
                view_id=_BROWSER_VIEW_ID,
                view_generation=_BROWSER_VIEW_GENERATION,
            )
        with pytest.raises(OperatorActionError, match="browser view is disconnected"):
            await runtime.confirm_task(
                new_view.preparation_token,
                view_id=_BROWSER_VIEW_ID,
                view_generation=_BROWSER_VIEW_GENERATION,
            )

        result = await runtime.confirm_task(
            new_view.preparation_token,
            view_id=_OTHER_BROWSER_VIEW_ID,
            view_generation=_OTHER_BROWSER_VIEW_GENERATION,
        )
        assert result.status == "SUCCESS"
    finally:
        await runtime.stop()


@pytest.mark.parametrize("stream_name", ["_entity_stream", "_location_stream"])
@pytest.mark.asyncio
async def test_watcher_exit_invalidates_preparation_before_confirm(
    stream_name: str,
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=True))
    await runtime.start()
    try:
        await _wait_for_entities(runtime)
        target = next(
            item
            for item in (await runtime.snapshot()).entities
            if item.type == "synthetic-task-target"
        )
        prepared = await runtime.prepare_task(
            PrepareTaskRequest(
                entity_id=target.entity_id,
                integration=target.integration,
                command="echo",
                payload={"message": "watcher closes before confirm"},
            )
        )
        stream = getattr(runtime, stream_name)
        assert stream is not None
        await stream.aclose()
        for _attempt in range(40):
            if any(
                item.code == "task_preparation_discarded"
                for item in (await runtime.snapshot()).diagnostics
            ):
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("watcher exit did not invalidate the prepared task")
        assert runtime.client is not None
        assert runtime.client.status.mqtt_connected is True
        sends = 0

        async def count_send(*_args: object, **_kwargs: object) -> None:
            nonlocal sends
            sends += 1

        monkeypatch.setattr(type(runtime.client.tasks), "send", count_send)
        with pytest.raises(OperatorActionError, match="invalid or expired") as captured:
            await runtime.confirm_task(prepared.preparation_token)
        assert captured.value.status_code == 409
        assert sends == 0
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_stale_preparation_cannot_revive_after_a_fresh_entity_update(
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    settings = replace(
        _settings(_APPLICATION_ROOT, tasking=True),
        stale_after_seconds=0.25,
        synthetic_period_seconds=10.0,
    )
    runtime = OperatorRuntime(settings)
    await runtime.start()
    try:
        await _wait_for_entities(runtime)
        target = next(
            item
            for item in (await runtime.snapshot()).entities
            if item.type == "synthetic-task-target"
        )
        prepared = await runtime.prepare_task(
            PrepareTaskRequest(
                entity_id=target.entity_id,
                integration=target.integration,
                command="echo",
                payload={"message": "stale then fresh"},
            )
        )
        await asyncio.sleep(0.3)
        stale_target = await runtime.state.task_target(
            integration=target.integration,
            entity_id=target.entity_id,
        )
        assert stale_target is not None
        assert stale_target.entity_freshness == "stale"

        await runtime._publish_mock_frame(1)
        for _attempt in range(40):
            refreshed = await runtime.state.task_target(
                integration=target.integration,
                entity_id=target.entity_id,
            )
            snapshot = await runtime.snapshot()
            if (
                refreshed is not None
                and refreshed.entity_freshness == "fresh"
                and any(item.code == "task_preparation_discarded" for item in snapshot.diagnostics)
            ):
                break
            await asyncio.sleep(0.025)
        else:
            raise AssertionError("fresh target update did not invalidate the stale preparation")

        assert runtime.client is not None
        sends = 0

        async def count_send(*_args: object, **_kwargs: object) -> None:
            nonlocal sends
            sends += 1

        monkeypatch.setattr(type(runtime.client.tasks), "send", count_send)
        with pytest.raises(OperatorActionError, match="invalid or expired") as captured:
            await runtime.confirm_task(prepared.preparation_token)
        assert captured.value.status_code == 409
        assert sends == 0
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_prepare_allows_an_eligible_entity_update_interleaved_before_token_insertion(
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=True))
    await runtime.start()
    try:
        await _wait_for_entities(runtime)
        target = next(
            item
            for item in (await runtime.snapshot()).entities
            if item.type == "synthetic-task-target"
        )
        original_task_target = runtime.state.task_target
        interleaved = False

        async def task_target_with_interleave(*, integration: str, entity_id: UUID) -> object:
            nonlocal interleaved
            observed = await original_task_target(
                integration=integration,
                entity_id=entity_id,
            )
            if not interleaved:
                interleaved = True
                assert observed is not None
                previous_observation = observed.last_observed_at
                await runtime._publish_mock_frame(17)
                for _attempt in range(40):
                    current = await original_task_target(
                        integration=integration,
                        entity_id=entity_id,
                    )
                    if current is not None and current.last_observed_at != previous_observation:
                        break
                    await asyncio.sleep(0.025)
                else:
                    raise AssertionError("forced entity update was not observed")
            return observed

        monkeypatch.setattr(runtime.state, "task_target", task_target_with_interleave)
        assert runtime.client is not None
        sends = 0

        async def count_send(*_args: object, **_kwargs: object) -> None:
            nonlocal sends
            sends += 1

        monkeypatch.setattr(type(runtime.client.tasks), "send", count_send)
        prepared = await runtime.prepare_task(
            PrepareTaskRequest(
                entity_id=target.entity_id,
                integration=target.integration,
                command="echo",
                payload={"message": "forced prepare interleave"},
            )
        )
        assert prepared.target_key == target.key
        assert sends == 0
        assert await runtime.discard_prepared_task(prepared.preparation_token) == 1
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_background_failure_is_reported_after_all_real_resources_close(
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=False))
    await runtime.start()
    mock = runtime._mock
    assert mock is not None

    async def fail_before_close() -> None:
        raise RuntimeError("synthetic background failure canary")

    failed = asyncio.create_task(fail_before_close(), name="operator-background-regression")
    runtime._tasks.append(failed)
    await asyncio.sleep(0)

    with pytest.raises(OperatorCleanupError) as captured:
        await runtime.stop()

    assert captured.value.components == ("background task operator-background-regression",)
    assert "synthetic background failure canary" not in str(captured.value)
    assert mock.active_connection_count == 0
    assert mock.active_task_count == 0
    assert runtime.client is None
    assert runtime._mock_clients == {}


@pytest.mark.asyncio
async def test_local_api_is_read_only_by_default_and_requires_intent_header(
    fail_on_unhandled_loop_exception: None,
) -> None:
    application = create_app(
        _settings(_APPLICATION_ROOT, tasking=False), application_root=_APPLICATION_ROOT
    )
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            response = await client.get("/api/config")
            assert response.status_code == 200
            assert response.json()["read_only"] is True
            assert response.json()["tasking_enabled"] is False

            await asyncio.sleep(0.2)
            state = (await client.get("/api/state")).json()
            target = next(
                item for item in state["entities"] if item["type"] == "synthetic-task-target"
            )
            body = {
                "entity_id": target["entity_id"],
                "integration": target["integration"],
                "command": "echo",
                "payload": {"message": "synthetic"},
            }
            origin = {
                "Origin": "http://127.0.0.1:4173",
                "X-Operator-View": str(_BROWSER_VIEW_ID),
                "X-Operator-View-Generation": str(_BROWSER_VIEW_GENERATION),
            }
            missing_intent = await client.post("/api/tasks/prepare", headers=origin, json=body)
            assert missing_intent.status_code == 400
            for path, mutation_body, required_intent in (
                (
                    "/api/tasks/confirm",
                    {"preparation_token": "x" * 32, "confirmed": True},
                    "confirm",
                ),
                (
                    "/api/tasks/discard",
                    {"preparation_token": "x" * 32},
                    "discard",
                ),
            ):
                missing_mutation_intent = await client.post(
                    path,
                    headers=origin,
                    json=mutation_body,
                )
                assert missing_mutation_intent.status_code == 400
                assert missing_mutation_intent.json() == {
                    "detail": f"explicit {required_intent} intent header is required"
                }
            missing_view = await client.post(
                "/api/tasks/prepare",
                headers={
                    "Origin": "http://127.0.0.1:4173",
                    "X-Operator-Intent": "prepare",
                },
                json=body,
            )
            assert missing_view.status_code == 400
            assert missing_view.json() == {"detail": "operator view identity is required"}
            missing_generation = await client.post(
                "/api/tasks/prepare",
                headers={
                    "Origin": "http://127.0.0.1:4173",
                    "X-Operator-Intent": "prepare",
                    "X-Operator-View": str(_BROWSER_VIEW_ID),
                },
                json=body,
            )
            assert missing_generation.status_code == 400
            assert missing_generation.json() == {"detail": "operator view generation is required"}
            denied = await client.post(
                "/api/tasks/prepare",
                headers=origin | {"X-Operator-Intent": "prepare"},
                json=body,
            )
            assert denied.status_code == 403
            assert denied.json() == {"detail": "tasking is disabled by deployment policy"}

            rejected_origin = await client.post(
                "/api/tasks/prepare",
                headers={
                    "Origin": "https://localhost",
                    "X-Operator-Intent": "prepare",
                    "X-Operator-View": str(_BROWSER_VIEW_ID),
                },
                json=body,
            )
            assert rejected_origin.status_code == 403

            canary = "secret-validation-canary"
            invalid = await client.post(
                "/api/tasks/prepare",
                headers=origin | {"X-Operator-Intent": "prepare"},
                json=body | {"entity_id": canary},
            )
            assert invalid.status_code == 422
            assert canary not in invalid.text
            assert invalid.json() == {"detail": "request validation failed"}

            oversized = await client.post(
                "/api/tasks/prepare",
                headers=origin | {"X-Operator-Intent": "prepare"},
                content=b"x" * (20 * 1024 + 1),
            )
            assert oversized.status_code == 413

        untrusted_transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=untrusted_transport, base_url="http://localhost"
        ) as client:
            rejected_host = await client.get("/healthz", headers={"Host": "invalid"})
            assert rejected_host.status_code == 400

    runtime = application.state.operator_runtime
    assert runtime.hub.client_count == 0


@pytest.mark.asyncio
async def test_view_retirement_requires_exact_generation_and_retains_bounded_idempotency_proof(
    fail_on_unhandled_loop_exception: None,
) -> None:
    settings = replace(
        _settings(_APPLICATION_ROOT, tasking=True),
        maximum_browser_clients=1,
        prepared_task_limit=1,
    )
    application = create_app(settings, application_root=_APPLICATION_ROOT)

    async with application.router.lifespan_context(application):
        runtime = application.state.operator_runtime
        transport = httpx.ASGITransport(app=application)

        async def retire(
            client: httpx.AsyncClient,
            view_id: UUID,
            generation: UUID,
        ) -> httpx.Response:
            return await client.post(
                "/api/view/retire",
                headers={
                    "Origin": "http://127.0.0.1:4173",
                    "X-Operator-Intent": "retire-view",
                    "X-Operator-View": str(view_id),
                    "X-Operator-View-Generation": str(generation),
                },
                json={},
            )

        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            assert await runtime.activate_browser_view(
                _BROWSER_VIEW_ID,
                _BROWSER_VIEW_GENERATION,
            )
            first = await retire(client, _BROWSER_VIEW_ID, _BROWSER_VIEW_GENERATION)
            exact_retry = await retire(client, _BROWSER_VIEW_ID, _BROWSER_VIEW_GENERATION)
            unknown_generation = await retire(
                client,
                _BROWSER_VIEW_ID,
                _NEXT_BROWSER_VIEW_GENERATION,
            )

            assert first.status_code == 200
            assert first.json() == {"retired": True}
            assert exact_retry.status_code == 200
            assert exact_retry.json() == {"retired": True}
            assert unknown_generation.status_code == 409

            assert await runtime.activate_browser_view(
                _OTHER_BROWSER_VIEW_ID,
                _OTHER_BROWSER_VIEW_GENERATION,
            )
            assert (
                await retire(
                    client,
                    _OTHER_BROWSER_VIEW_ID,
                    _OTHER_BROWSER_VIEW_GENERATION,
                )
            ).status_code == 200

            evicted_exact_generation = await retire(
                client,
                _BROWSER_VIEW_ID,
                _BROWSER_VIEW_GENERATION,
            )
            retained_exact_generation = await retire(
                client,
                _OTHER_BROWSER_VIEW_ID,
                _OTHER_BROWSER_VIEW_GENERATION,
            )
            assert evicted_exact_generation.status_code == 409
            assert retained_exact_generation.status_code == 200


@pytest.mark.asyncio
async def test_discard_api_rejects_a_no_op_as_unconfirmed_invalidation(
    fail_on_unhandled_loop_exception: None,
) -> None:
    application = create_app(
        _settings(_APPLICATION_ROOT, tasking=True), application_root=_APPLICATION_ROOT
    )
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            response = await client.post(
                "/api/tasks/discard",
                headers={
                    "Origin": "http://127.0.0.1:4173",
                    "X-Operator-Intent": "discard",
                    "X-Operator-View": str(_BROWSER_VIEW_ID),
                    "X-Operator-View-Generation": str(_BROWSER_VIEW_GENERATION),
                },
                json={"preparation_token": "x" * 32},
            )

    assert response.status_code == 409
    assert response.json() == {"detail": "prepared task is no longer available"}


@pytest.mark.asyncio
async def test_html_and_api_responses_have_fail_closed_browser_headers(
    fail_on_unhandled_loop_exception: None,
) -> None:
    application = create_app(
        _settings(_APPLICATION_ROOT, tasking=False),
        application_root=_APPLICATION_ROOT,
    )
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            responses = (await client.get("/"), await client.get("/api/config"))

    assert responses[0].headers["content-type"].startswith("text/html")
    for response in responses:
        policy = response.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in policy
        assert "img-src 'self' data:" in policy
        assert "https://" not in policy
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.asyncio
async def test_browser_csp_admits_only_the_validated_https_basemap_origin(
    fail_on_unhandled_loop_exception: None,
) -> None:
    settings = replace(
        _settings(_APPLICATION_ROOT, tasking=False),
        basemap_url_template="https://tiles.example.invalid:8443/{z}/{x}/{y}.png",
        basemap_attribution="Authorized map data",
    )
    application = create_app(settings, application_root=_APPLICATION_ROOT)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            responses = (await client.get("/"), await client.get("/api/config"))

    for response in responses:
        policy = response.headers["content-security-policy"]
        assert "img-src 'self' data: https://tiles.example.invalid:8443" in policy
        assert "{z}" not in policy
        assert "*.example.invalid" not in policy
        assert "http://tiles.example.invalid" not in policy


@pytest.mark.asyncio
async def test_shutdown_continues_when_client_close_raises(
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(_APPLICATION_ROOT, tasking=False))
    await runtime.start()
    mock = runtime._mock
    assert mock is not None
    await _wait_for_entities(runtime)
    assert isinstance(runtime.client, ECNClient)
    original_close = runtime.client.close

    async def failing_close() -> None:
        await original_close()
        raise RuntimeError("synthetic post-close failure")

    monkeypatch.setattr(runtime.client, "close", failing_close)
    with pytest.raises(OperatorCleanupError) as captured:
        await runtime.stop()

    assert mock.active_connection_count == 0
    assert mock.active_task_count == 0
    assert captured.value.components == ("public MQTT client",)
    assert "synthetic post-close failure" not in str(captured.value)
    assert runtime.client is None
    assert runtime._mock_clients == {}
    assert (await runtime.snapshot()).entities == ()


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.asyncio
async def test_task_api_rejects_non_finite_raw_json(
    constant: str,
    fail_on_unhandled_loop_exception: None,
) -> None:
    application = create_app(
        _settings(_APPLICATION_ROOT, tasking=True), application_root=_APPLICATION_ROOT
    )
    async with application.router.lifespan_context(application):
        service = application.state.operator_runtime
        await _wait_for_entities(service)
        target = next(
            item
            for item in (await service.snapshot()).entities
            if item.type == "synthetic-task-target"
        )
        assert (
            await service.activate_browser_view(
                _BROWSER_VIEW_ID,
                _BROWSER_VIEW_GENERATION,
            )
            is True
        )
        raw = (
            '{"entity_id":"'
            + str(target.entity_id)
            + '","integration":"mock-target","command":"echo",'
            + '"payload":{"message":"synthetic","numeric_canary":'
            + constant
            + "}}"
        ).encode()
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            response = await client.post(
                "/api/tasks/prepare",
                headers={
                    "Content-Type": "application/json",
                    "Origin": "http://127.0.0.1:4173",
                    "X-Operator-Intent": "prepare",
                    "X-Operator-View": str(_BROWSER_VIEW_ID),
                    "X-Operator-View-Generation": str(_BROWSER_VIEW_GENERATION),
                },
                content=raw,
            )
        assert response.status_code == 422
        assert response.json() == {"detail": "task payload contains non-finite numbers"}
        assert (
            await service.deactivate_browser_view(
                _BROWSER_VIEW_ID,
                _BROWSER_VIEW_GENERATION,
            )
            == 0
        )
