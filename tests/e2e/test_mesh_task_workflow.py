# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel

from picogrid_ecn_client import (
    DeliveryPhase,
    ECNClient,
    OutcomeUnknownError,
    TaskRequestContext,
)
from picogrid_ecn_client.testing import MockECN

SOURCE_TERMINAL_ID = UUID("11111111-1111-4111-8111-111111111111")
TARGET_TERMINAL_ID = UUID("22222222-2222-4222-8222-222222222222")
TARGET_ENTITY_ID = UUID("33333333-3333-4333-8333-333333333333")


class Echo(BaseModel):
    message: str


@pytest.mark.asyncio
async def test_minimal_mock_does_not_invent_terminal_route_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts: list[TaskRequestContext] = []
    publications: list[tuple[str, bytes]] = []
    async with MockECN() as mock:
        original_mqtt_published = mock.mqtt_published

        async def record_mqtt_publication(
            identity: Any,
            topic: str,
            payload: bytes,
        ) -> Any:
            publications.append((topic, payload))
            return await original_mqtt_published(identity, topic, payload)

        monkeypatch.setattr(mock, "mqtt_published", record_mqtt_publication)
        source_config = mock.client_config("mesh-sender").model_copy(
            update={"terminal_id": SOURCE_TERMINAL_ID}
        )
        target_config = mock.client_config("mesh-receiver").model_copy(
            update={"terminal_id": TARGET_TERMINAL_ID}
        )
        async with ECNClient(target_config) as receiver, ECNClient(source_config) as sender:

            async def echo(context: TaskRequestContext, request: Echo) -> Echo:
                contexts.append(context)
                return request

            registration = await receiver.tasks.register(
                entity_id=TARGET_ENTITY_ID,
                command="echo",
                request_model=Echo,
                result_model=Echo,
                handler=echo,
            )
            with pytest.raises(OutcomeUnknownError) as caught:
                await sender.tasks.send(
                    target_entity_id=TARGET_ENTITY_ID,
                    target_integration="mesh-receiver",
                    target_terminal_id=TARGET_TERMINAL_ID,
                    command="echo",
                    request=Echo(message="synthetic mesh task"),
                    result_model=Echo,
                    timeout=0.05,
                )
            assert caught.value.delivery_phase is DeliveryPhase.RESPONSE_PENDING
            assert caught.value.task_id is not None
            await receiver.tasks.unregister(registration)

        assert contexts == []
        assert len(publications) == 1
        topic, payload = publications[0]
        assert topic == f"{TARGET_TERMINAL_ID}/task/mesh-receiver/{TARGET_ENTITY_ID}/echo"
        assert json.loads(payload)["task_id"] == caught.value.task_id
        assert mock.active_connection_count == 0
        assert mock.active_task_count == 0
