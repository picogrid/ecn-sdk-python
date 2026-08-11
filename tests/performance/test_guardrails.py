# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
import time
import tracemalloc
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import BaseModel

from picogrid_ecn_client import (
    DeliveryPolicy,
    ECNClient,
    EventStream,
    TaskResult,
)
from picogrid_ecn_client._protocol import decode_entity_json
from picogrid_ecn_client.testing import MockECN

FIXTURES = Path(__file__).parents[1] / "fixtures" / "protocol"
ENTITY_ID = UUID("30000000-0000-4000-8000-000000000001")


class IncrementRequest(BaseModel):
    value: int


class IncrementResult(BaseModel):
    value: int


@pytest.mark.performance
def test_representative_track_decode_guardrail() -> None:
    payload = (FIXTURES / "track_event.json").read_bytes()
    topic = "entity/synthetic-vendor/10000000-0000-4000-8000-000000000001/track"
    started = time.perf_counter()
    for _ in range(5_000):
        event = decode_entity_json(topic, payload, 64 * 1024)
    elapsed = time.perf_counter() - started
    assert event.entity.type == "synthetic-air-track"
    assert elapsed < 3.0


@pytest.mark.asyncio
@pytest.mark.performance
async def test_latest_watcher_memory_remains_bounded_under_burst() -> None:
    stream = EventStream[int](buffer_size=32, delivery_policy=DeliveryPolicy.LATEST)
    tracemalloc.start()
    baseline, _ = tracemalloc.get_traced_memory()
    for value in range(20_000):
        stream._put_nowait(value)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert stream.buffer_size == 32
    assert stream.dropped_count == 20_000 - stream.buffer_size
    assert current - baseline < 512 * 1024
    assert peak - baseline < 2 * 1024 * 1024
    assert await anext(stream) == 20_000 - stream.buffer_size
    await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.performance
async def test_task_round_trip_and_cancellation_guardrails() -> None:
    async with MockECN() as mock:
        client = ECNClient(mock.client_config("performance-client"))
        await client.start()

        async def increment(request: IncrementRequest) -> IncrementResult:
            return IncrementResult(value=request.value + 1)

        await client.tasks.register(
            entity_id=ENTITY_ID,
            command="increment",
            request_model=IncrementRequest,
            result_model=IncrementResult,
            handler=increment,
        )
        started = time.perf_counter()
        for value in range(25):
            result = await client.tasks.send(
                target_entity_id=ENTITY_ID,
                target_integration="performance-client",
                command="increment",
                request=IncrementRequest(value=value),
                result_model=IncrementResult,
                timeout=1,
            )
            assert isinstance(result, TaskResult)
            assert isinstance(result.data, IncrementResult)
            assert result.data.value == value + 1
        assert time.perf_counter() - started < 5.0

        release = asyncio.Event()
        eight_started = asyncio.Event()
        active_handlers = 0

        async def wait_for_release(request: IncrementRequest) -> IncrementResult:
            nonlocal active_handlers
            active_handlers += 1
            if active_handlers == 8:
                eight_started.set()
            await release.wait()
            return IncrementResult(value=request.value)

        await client.tasks.register(
            entity_id=ENTITY_ID,
            command="wait",
            request_model=IncrementRequest,
            result_model=IncrementResult,
            handler=wait_for_release,
        )
        pending = [
            asyncio.create_task(
                client.tasks.send(
                    target_entity_id=ENTITY_ID,
                    target_integration="performance-client",
                    command="wait",
                    request=IncrementRequest(value=value),
                    result_model=IncrementResult,
                    timeout=10,
                )
            )
            for value in range(8)
        ]
        await asyncio.wait_for(eight_started.wait(), timeout=2)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        close_started = time.perf_counter()
        await client.close()
        assert time.perf_counter() - close_started < 2.0
        assert mock.active_connection_count == 0
