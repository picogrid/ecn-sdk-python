# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError

from picogrid_ecn_client import (
    ECNClient,
    ECNClientError,
    Entity,
    EntityCategory,
    EntityEvent,
    TaskMode,
    TaskRegistration,
    TaskRequestContext,
    TaskResult,
    TaskStatus,
    ValidationError,
)
from picogrid_ecn_client.testing import MockECN
from picogrid_ecn_client.workflows import observe as workflow_observe
from picogrid_ecn_client.workflows import tasks as workflow_tasks
from picogrid_ecn_client.workflows.observe import (
    observe_mesh_data,
    watch_detections,
    watch_tracks,
)
from picogrid_ecn_client.workflows.publish import publish_entity
from picogrid_ecn_client.workflows.tasks import (
    EchoRequest,
    EchoResult,
    dispatch_mesh_task,
    dispatch_task,
    receive_mesh_task,
    receive_task,
)


@pytest.mark.parametrize("model", [EchoRequest, EchoResult])
def test_echo_models_reject_unknown_fields(model: type[EchoRequest | EchoResult]) -> None:
    with pytest.raises(PydanticValidationError, match="Extra inputs are not permitted"):
        model(message="run", mesage="wrong")


def _entity_event(index: int) -> EntityEvent:
    timestamp = datetime.now(UTC)
    return EntityEvent(
        timestamp=timestamp,
        entity=Entity(
            id=uuid4(),
            category=EntityCategory.TRACK,
            integration="workflow-test",
            recorded_at=timestamp,
            type=f"track-{index}",
        ),
    )


def test_workflows_share_private_retention_helper() -> None:
    assert workflow_observe._EventRetention is workflow_tasks._EventRetention


class _FiniteStream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self.closed = False

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Any]:
        for event in self._events:
            yield event

    async def aclose(self) -> None:
        self.closed = True


class _EntityService:
    def __init__(self, stream: _FiniteStream) -> None:
        self._stream = stream
        self.calls: list[dict[str, Any]] = []

    async def watch(self, **kwargs: Any) -> _FiniteStream:
        self.calls.append(kwargs)
        return self._stream


@pytest.mark.asyncio
async def test_unbounded_entity_watch_retains_client_buffer_and_delivers_all() -> None:
    events = [_entity_event(index) for index in range(4)]
    stream = _FiniteStream(events)
    client = SimpleNamespace(
        config=SimpleNamespace(watcher_buffer_size=2),
        entities=_EntityService(stream),
    )
    delivered: list[EntityEvent] = []

    result = await watch_tracks(client, limit=0, on_event=delivered.append)

    assert result.events == tuple(events[-2:])
    assert isinstance(result.events, tuple)
    assert delivered == events


@pytest.mark.asyncio
async def test_bounded_entity_watch_keeps_exact_limit() -> None:
    events = [_entity_event(index) for index in range(4)]
    stream = _FiniteStream(events)
    client = SimpleNamespace(
        config=SimpleNamespace(watcher_buffer_size=1),
        entities=_EntityService(stream),
    )
    delivered: list[EntityEvent] = []

    result = await watch_tracks(client, limit=3, on_event=delivered.append)

    assert result.events == tuple(events[:3])
    assert isinstance(result.events, tuple)
    assert delivered == events[:3]


@pytest.mark.parametrize("workflow", [watch_tracks, watch_detections])
@pytest.mark.parametrize("integration", ["", "   "])
@pytest.mark.asyncio
async def test_single_entity_watch_rejects_empty_integration_before_opening_stream(
    workflow: Callable[..., Any],
    integration: str,
) -> None:
    entities = _EntityService(_FiniteStream([]))
    client = SimpleNamespace(
        config=SimpleNamespace(watcher_buffer_size=1),
        entities=entities,
    )

    with pytest.raises(ValidationError, match="integration must be non-empty"):
        await workflow(client, integration=integration)

    assert entities.calls == []


@pytest.mark.parametrize("workflow", [watch_tracks, watch_detections])
@pytest.mark.asyncio
async def test_single_entity_watch_preserves_none_as_unfiltered(
    workflow: Callable[..., Any],
) -> None:
    entities = _EntityService(_FiniteStream([]))
    client = SimpleNamespace(
        config=SimpleNamespace(watcher_buffer_size=1),
        entities=entities,
    )

    await workflow(client, integration=None)

    assert entities.calls[0]["integrations"] is None


@pytest.mark.parametrize(
    ("integrations", "message"),
    [
        ([], "integrations must be non-empty"),
        ([""], "integration names must be non-empty"),
        (["   "], "integration names must be non-empty"),
    ],
)
@pytest.mark.asyncio
async def test_mesh_observation_rejects_empty_integrations_before_opening_streams(
    integrations: list[str],
    message: str,
) -> None:
    entities = _EntityService(_FiniteStream([]))
    locations = _EntityService(_FiniteStream([]))
    client = SimpleNamespace(
        config=SimpleNamespace(watcher_buffer_size=1),
        entities=entities,
        locations=locations,
    )

    with pytest.raises(ValidationError, match=message):
        await observe_mesh_data(client, integrations=integrations)

    assert entities.calls == []
    assert locations.calls == []


def _context(index: int, entity_id: UUID) -> TaskRequestContext:
    return TaskRequestContext(
        task_id=f"task-{index}",
        target_entity_id=entity_id,
        command="echo",
        source="local",
        mode=TaskMode.COMPLETE,
        received_at=datetime.now(UTC),
    )


class _TaskService:
    def __init__(
        self,
        contexts: list[TaskRequestContext],
        *,
        emit_during_unregister: bool = False,
    ) -> None:
        self._contexts = contexts
        self._emit_during_unregister = emit_during_unregister
        self._handler: Callable[..., Any] | None = None
        self.registration = TaskRegistration(
            registration_id=uuid4(),
            entity_id=contexts[0].target_entity_id,
            command="echo",
            request_schema=EchoRequest.model_json_schema(),
            result_schema=EchoResult.model_json_schema(),
            registered_at=datetime.now(UTC),
        )
        self.unregister_calls = 0
        self.rejected_after_limit: Exception | None = None

    async def register(self, *, handler: Callable[..., Any], **_: Any) -> Any:
        self._handler = handler
        return self.registration

    async def emit_all(self) -> None:
        assert self._handler is not None
        for context in self._contexts:
            await self._handler(context, EchoRequest(message=context.task_id))

    async def unregister(self, registration: Any) -> None:
        assert registration is self.registration
        self.unregister_calls += 1
        if not self._emit_during_unregister:
            return
        assert self._handler is not None
        extra = _context(999, self._contexts[0].target_entity_id)
        try:
            await self._handler(extra, EchoRequest(message=extra.task_id))
        except ValidationError as error:
            self.rejected_after_limit = error


@pytest.mark.asyncio
async def test_unbounded_task_watch_retains_client_buffer_and_delivers_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_id = uuid4()
    service = _TaskService([_context(index, entity_id) for index in range(4)])
    client = SimpleNamespace(config=SimpleNamespace(watcher_buffer_size=2), tasks=service)
    release = asyncio.Event()
    delivered: list[TaskRequestContext] = []

    class _UnlimitedCompletion:
        def set(self) -> None:
            release.set()

        async def wait(self) -> None:
            await release.wait()

    real_event = asyncio.Event
    monkeypatch.setattr(
        workflow_tasks,
        "asyncio",
        SimpleNamespace(Event=_UnlimitedCompletion),
    )
    assert asyncio.Event is real_event

    async def produce() -> None:
        await service.emit_all()
        release.set()

    producer_task: asyncio.Task[None] | None = None

    def on_registered(_: Any) -> None:
        nonlocal producer_task
        producer_task = asyncio.create_task(produce())

    result = await receive_task(
        client,
        entity_id=entity_id,
        command="echo",
        task_limit=0,
        on_event=delivered.append,
        on_registered=on_registered,
    )
    assert producer_task is not None
    await producer_task

    assert [context.task_id for context in result.contexts] == ["task-2", "task-3"]
    assert [item.message for item in result.results] == ["task-2", "task-3"]
    assert [context.task_id for context in delivered] == [
        "task-0",
        "task-1",
        "task-2",
        "task-3",
    ]
    assert service.unregister_calls == 1


@pytest.mark.asyncio
async def test_bounded_task_watch_keeps_exact_limit_and_rejects_late_task() -> None:
    entity_id = uuid4()
    service = _TaskService(
        [_context(index, entity_id) for index in range(3)],
        emit_during_unregister=True,
    )
    client = SimpleNamespace(config=SimpleNamespace(watcher_buffer_size=1), tasks=service)
    delivered: list[TaskRequestContext] = []
    producer_task: asyncio.Task[None] | None = None

    def on_registered(_: Any) -> None:
        nonlocal producer_task
        producer_task = asyncio.create_task(service.emit_all())

    result = await receive_task(
        client,
        entity_id=entity_id,
        command="echo",
        task_limit=3,
        on_event=delivered.append,
        on_registered=on_registered,
    )
    assert producer_task is not None
    await producer_task

    assert [context.task_id for context in result.contexts] == [
        "task-0",
        "task-1",
        "task-2",
    ]
    assert [item.message for item in result.results] == [
        "task-0",
        "task-1",
        "task-2",
    ]
    assert len(delivered) == 3
    assert isinstance(service.rejected_after_limit, ValidationError)


@pytest.mark.asyncio
async def test_task_callback_failure_does_not_desynchronize_retained_pairs() -> None:
    entity_id = uuid4()
    contexts = [_context(index, entity_id) for index in range(2)]
    service = _TaskService(contexts)
    client = SimpleNamespace(config=SimpleNamespace(watcher_buffer_size=2), tasks=service)
    producer_task: asyncio.Task[None] | None = None

    def on_event(context: TaskRequestContext) -> None:
        if context is contexts[0]:
            raise ValueError("consumer rejected context")

    async def produce() -> None:
        assert service._handler is not None
        with pytest.raises(ValueError, match="consumer rejected"):
            await service._handler(contexts[0], EchoRequest(message="first"))
        await service._handler(contexts[1], EchoRequest(message="second"))

    def on_registered(_: Any) -> None:
        nonlocal producer_task
        producer_task = asyncio.create_task(produce())

    result = await receive_task(
        client,
        entity_id=entity_id,
        command="echo",
        task_limit=1,
        on_event=on_event,
        on_registered=on_registered,
    )
    assert producer_task is not None
    await producer_task

    assert result.contexts == (contexts[1],)
    assert tuple(item.message for item in result.results) == ("second",)


@pytest.mark.asyncio
async def test_receive_task_rejects_negative_limit() -> None:
    client = SimpleNamespace(
        config=SimpleNamespace(watcher_buffer_size=1),
        tasks=SimpleNamespace(),
    )

    with pytest.raises(ValidationError, match="task_limit must be non-negative"):
        await receive_task(
            client,
            entity_id=uuid4(),
            command="echo",
            task_limit=-1,
        )


@pytest.mark.asyncio
async def test_bounded_task_receiver_waits_for_final_response_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_id = uuid4()
    response_publish_started = asyncio.Event()
    release_response_publish = asyncio.Event()
    unregister_checkpoint = asyncio.Event()
    registered = asyncio.Event()

    async with MockECN() as mock:
        receiver = ECNClient(mock.client_config("workflow-receiver"))
        sender = ECNClient(mock.client_config("workflow-sender"))
        original_publish = receiver._mqtt_transport.publish
        original_unregister = type(receiver.tasks).unregister

        async def delayed_response_publish(
            topic: str,
            payload: bytes,
            qos: int = 1,
            **kwargs: Any,
        ) -> Any:
            if topic.endswith("/response"):
                response_publish_started.set()
                await release_response_publish.wait()
            return await original_publish(topic, payload, qos, **kwargs)

        async def checkpoint_unregister(tasks: Any, registration: Any) -> None:
            await original_unregister(tasks, registration)
            if tasks is receiver.tasks:
                asyncio.get_running_loop().call_soon(unregister_checkpoint.set)

        monkeypatch.setattr(receiver._mqtt_transport, "publish", delayed_response_publish)
        monkeypatch.setattr(type(receiver.tasks), "unregister", checkpoint_unregister)

        async def run_receiver() -> Any:
            async with receiver:
                return await receive_task(
                    receiver,
                    entity_id=entity_id,
                    command="echo",
                    task_limit=1,
                    on_registered=lambda _: registered.set(),
                )

        receiver_task = asyncio.create_task(run_receiver())
        await registered.wait()
        async with sender:
            dispatch = asyncio.create_task(
                sender.tasks.send(
                    target_entity_id=entity_id,
                    target_integration="workflow-receiver",
                    command="echo",
                    request=EchoRequest(message="final"),
                    result_model=workflow_tasks.EchoResult,
                    timeout=0.5,
                    mode=TaskMode.COMPLETE,
                )
            )
            await response_publish_started.wait()
            await unregister_checkpoint.wait()
            release_response_publish.set()
            response = await dispatch

        received = await receiver_task

    assert isinstance(response, TaskResult)
    assert isinstance(response.data, workflow_tasks.EchoResult)
    assert response.data.message == "final"
    assert tuple(result.message for result in received.results) == ("final",)


@pytest.mark.asyncio
async def test_bounded_task_receiver_awaits_final_delivery_when_unregister_fails() -> None:
    entity_id = uuid4()
    context = _context(0, entity_id)
    release_delivery = asyncio.Event()
    unregister_called = asyncio.Event()
    unregister_error = ECNClientError("unregister failed")
    producer_task: asyncio.Task[None] | None = None

    class _FailingUnregisterTaskService:
        def __init__(self) -> None:
            self.registration = SimpleNamespace(registration_id=uuid4())
            self.handler: Callable[..., Any] | None = None

        async def register(self, *, handler: Callable[..., Any], **_: Any) -> Any:
            self.handler = handler
            return self.registration

        async def unregister(self, registration: Any) -> None:
            assert registration is self.registration
            unregister_called.set()
            raise unregister_error

        async def deliver(self) -> None:
            assert self.handler is not None
            await self.handler(context, EchoRequest(message="final"))
            await release_delivery.wait()

    service = _FailingUnregisterTaskService()
    client = SimpleNamespace(config=SimpleNamespace(watcher_buffer_size=1), tasks=service)

    def on_registered(_: Any) -> None:
        nonlocal producer_task
        producer_task = asyncio.create_task(service.deliver())

    receiver_task = asyncio.create_task(
        receive_task(
            client,
            entity_id=entity_id,
            command="echo",
            task_limit=1,
            on_registered=on_registered,
        )
    )
    await unregister_called.wait()
    await asyncio.sleep(0)
    delivery_was_awaited = not receiver_task.done()
    release_delivery.set()
    assert producer_task is not None
    await producer_task

    with pytest.raises(ECNClientError, match="unregister failed") as raised:
        await receiver_task

    assert delivery_was_awaited
    assert raised.value is unregister_error


@pytest.mark.asyncio
async def test_receive_mesh_task_rejects_mismatched_terminal_id() -> None:
    configured_terminal_id = uuid4()
    client = SimpleNamespace(
        config=SimpleNamespace(
            terminal_id=configured_terminal_id,
            watcher_buffer_size=1,
        ),
        tasks=SimpleNamespace(),
    )

    with pytest.raises(ValidationError, match="terminal_id must match"):
        await receive_mesh_task(
            client,
            terminal_id=uuid4(),
            entity_id=uuid4(),
            command="echo",
        )


class _FailIfCalledTaskService:
    async def send(self, **kwargs: Any) -> None:
        pytest.fail(f"task service called with invalid workflow input: {kwargs}")


@pytest.mark.parametrize(
    ("workflow", "extra", "operation"),
    [
        (dispatch_task, {}, "workflow.dispatch_task"),
        (
            dispatch_mesh_task,
            {"target_terminal_id": uuid4()},
            "workflow.dispatch_mesh_task",
        ),
    ],
)
@pytest.mark.parametrize("message", ["", "x" * 1025])
@pytest.mark.asyncio
async def test_dispatch_workflow_normalizes_request_validation_errors(
    workflow: Callable[..., Any],
    extra: dict[str, Any],
    message: str,
    operation: str,
) -> None:
    client = SimpleNamespace(tasks=_FailIfCalledTaskService())

    with pytest.raises(ValidationError, match="echo request is invalid") as captured:
        await workflow(
            client,
            target_entity_id=uuid4(),
            target_integration="target",
            command="echo",
            message=message,
            **extra,
        )

    assert captured.value.operation == operation
    assert "string_too_" in captured.value.details["errors"]
    assert isinstance(captured.value.__cause__, PydanticValidationError)
    assert "message" in str(captured.value.__cause__)


@pytest.mark.parametrize(
    ("field", "value", "model_name"),
    [
        ("display_name", "", "DisplayMetadata"),
        ("entity_type", "", "Entity"),
        ("name", "", "Entity"),
    ],
)
@pytest.mark.asyncio
async def test_publish_entity_normalizes_model_validation_errors(
    field: str,
    value: str,
    model_name: str,
) -> None:
    client = SimpleNamespace(
        config=SimpleNamespace(integration_name="workflow-test"),
        entities=SimpleNamespace(),
    )
    kwargs = {
        "entity_id": uuid4(),
        "category": EntityCategory.TRACK,
        "entity_type": "track",
        field: value,
    }

    with pytest.raises(ValidationError, match="entity input is invalid") as captured:
        await publish_entity(client, **kwargs)

    assert captured.value.operation == "workflow.publish_entity"
    assert "string_too_short" in captured.value.details["errors"]
    assert isinstance(captured.value.__cause__, PydanticValidationError)
    assert model_name in str(captured.value.__cause__)


@pytest.mark.asyncio
async def test_mesh_receiver_ignores_literal_local_requests() -> None:
    """A terminal-addressed route serves canonical source terminals only.

    A sender without a terminal identity publishes ``source="local"`` to the same
    entity and command topic, so without a source filter it would be echoed and
    counted against ``task_limit``, ending the receiver early.
    """
    entity_id = uuid4()
    terminal_id = uuid4()
    registered = asyncio.Event()

    async with MockECN() as mock:
        receiver = ECNClient(
            mock.client_config("mesh-receiver").model_copy(update={"terminal_id": terminal_id})
        )
        local_sender = ECNClient(mock.client_config("local-sender"))
        # A handler registers on the unprefixed request topic, so the canonical
        # terminal-sourced peer carries the same terminal identity as the route.
        mesh_sender = ECNClient(
            mock.client_config("mesh-sender").model_copy(update={"terminal_id": terminal_id})
        )

        async def run_receiver() -> Any:
            async with receiver:
                return await receive_mesh_task(
                    receiver,
                    terminal_id=terminal_id,
                    entity_id=entity_id,
                    command="echo",
                    task_limit=1,
                    on_registered=lambda _: registered.set(),
                )

        receiver_task = asyncio.create_task(run_receiver())
        await registered.wait()

        async with local_sender:
            rejected = await local_sender.tasks.send(
                target_entity_id=entity_id,
                target_integration="mesh-receiver",
                command="echo",
                request=EchoRequest(message="local"),
                result_model=workflow_tasks.EchoResult,
                timeout=1.0,
                mode=TaskMode.COMPLETE,
            )

        assert rejected.status is TaskStatus.FAILED
        # The local request must not have satisfied the bound.
        assert not receiver_task.done()

        async with mesh_sender:
            accepted = await mesh_sender.tasks.send(
                target_entity_id=entity_id,
                target_integration="mesh-receiver",
                command="echo",
                request=EchoRequest(message="mesh"),
                result_model=workflow_tasks.EchoResult,
                timeout=2,
                mode=TaskMode.COMPLETE,
            )

        assert isinstance(accepted, TaskResult)
        assert accepted.status is TaskStatus.SUCCESS

        received = await asyncio.wait_for(receiver_task, timeout=2)

    assert tuple(result.message for result in received.results) == ("mesh",)
    assert [context.source for context in received.contexts] == [terminal_id]


@pytest.mark.asyncio
async def test_mesh_observation_closes_the_entity_stream_when_location_cleanup_fails() -> None:
    """A failed unsubscribe on one stream must not strand the other watcher."""

    class _FailingCloseStream(_FiniteStream):
        async def aclose(self) -> None:
            await super().aclose()
            raise ECNClientError("unsubscribe rejected")

    entity_stream = _FiniteStream([])
    location_stream = _FailingCloseStream([])
    client = SimpleNamespace(
        config=SimpleNamespace(watcher_buffer_size=1),
        entities=_EntityService(entity_stream),
        locations=_EntityService(location_stream),
    )

    with pytest.raises(ECNClientError, match="unsubscribe rejected"):
        await observe_mesh_data(client, integrations=["mesh-peer"])

    assert location_stream.closed
    assert entity_stream.closed
