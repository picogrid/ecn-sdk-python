# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio

import pytest

from picogrid_ecn_client import AuthorizationError, DeliveryPolicy, EventStream


@pytest.mark.asyncio
async def test_fifo_drops_newest_when_full() -> None:
    stream = EventStream[int](buffer_size=1, delivery_policy=DeliveryPolicy.FIFO)
    assert stream._put_nowait(1)
    assert not stream._put_nowait(2)
    assert stream.dropped_count == 1
    assert await anext(stream) == 1
    await stream.aclose()


@pytest.mark.asyncio
async def test_latest_replaces_oldest_when_full() -> None:
    stream = EventStream[int](buffer_size=1, delivery_policy=DeliveryPolicy.LATEST)
    assert stream._put_nowait(1)
    assert stream._put_nowait(2)
    assert stream.dropped_count == 1
    assert await anext(stream) == 2
    await stream.aclose()


@pytest.mark.asyncio
async def test_close_is_idempotent_and_unblocks_consumer() -> None:
    closed = 0

    async def on_close() -> None:
        nonlocal closed
        closed += 1

    stream = EventStream[int](
        buffer_size=1,
        delivery_policy=DeliveryPolicy.FIFO,
        on_close=on_close,
    )
    waiting = asyncio.create_task(anext(stream))
    await stream.aclose()
    with pytest.raises(StopAsyncIteration):
        await waiting
    await stream.aclose()
    assert closed == 1


@pytest.mark.asyncio
async def test_cancelled_close_keeps_one_shielded_resumable_cleanup() -> None:
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_calls = 0

    async def on_close() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        cleanup_started.set()
        await release_cleanup.wait()

    stream = EventStream[int](
        buffer_size=1,
        delivery_policy=DeliveryPolicy.FIFO,
        on_close=on_close,
    )
    closing = asyncio.create_task(stream.aclose())
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert stream.closed
    release_cleanup.set()
    await asyncio.wait_for(stream.aclose(), timeout=1)
    assert cleanup_calls == 1
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_terminal_failure_discards_buffer_and_releases_ownership_once() -> None:
    close_calls = 0

    async def on_close() -> None:
        nonlocal close_calls
        close_calls += 1

    stream = EventStream[int](
        buffer_size=2,
        delivery_policy=DeliveryPolicy.FIFO,
        on_close=on_close,
    )
    assert stream._put_nowait(1)
    error = AuthorizationError(
        "restored subscription was denied",
        operation="entities.watch",
    )

    await stream._fail(error)

    with pytest.raises(AuthorizationError) as caught:
        await anext(stream)
    assert caught.value is error
    with pytest.raises(AuthorizationError) as repeated:
        await anext(stream)
    assert repeated.value is error
    await stream.aclose()
    assert close_calls == 1


@pytest.mark.asyncio
async def test_first_terminal_action_wins() -> None:
    stream = EventStream[int](buffer_size=1, delivery_policy=DeliveryPolicy.LATEST)
    first = AuthorizationError("first denial", operation="entities.watch")
    second = AuthorizationError("second denial", operation="entities.watch")

    await stream._fail(first)
    await stream._fail(second)

    with pytest.raises(AuthorizationError) as caught:
        await anext(stream)
    assert caught.value is first
