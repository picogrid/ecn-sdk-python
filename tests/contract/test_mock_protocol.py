# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
import json

import aiomqtt
import pytest

from picogrid_ecn_client.testing import FULL_ACCESS_TOKEN, MockECN


@pytest.mark.asyncio
@pytest.mark.contract
async def test_mock_broker_is_compatible_with_mqtt_v5_client() -> None:
    topic = "entity/publisher/00000000-0000-4000-8000-000000000501/track"
    payload = json.dumps(
        {
            "id": "00000000-0000-4000-8000-000000000501",
            "integration_name": "publisher",
            "recorded_at": "2026-08-05T12:00:00Z",
            "category": "TRACK",
            "type_": "synthetic-track",
            "metadata": {},
        },
        separators=(",", ":"),
    ).encode()
    async with MockECN() as mock:
        subscriber = aiomqtt.Client(
            mock.host,
            mock.mqtt_port,
            identifier="contract-subscriber",
            username="subscriber",
            password=FULL_ACCESS_TOKEN,
            protocol=aiomqtt.ProtocolVersion.V5,
        )
        publisher = aiomqtt.Client(
            mock.host,
            mock.mqtt_port,
            identifier="contract-publisher",
            username="publisher",
            password=FULL_ACCESS_TOKEN,
            protocol=aiomqtt.ProtocolVersion.V5,
        )
        async with subscriber, publisher:
            await subscriber.subscribe(topic, qos=1)
            await publisher.publish(topic, payload, qos=0)
            message = await asyncio.wait_for(anext(subscriber.messages), timeout=1)
            assert str(message.topic) == topic
            assert bytes(message.payload) == payload
            assert message.qos == 0
