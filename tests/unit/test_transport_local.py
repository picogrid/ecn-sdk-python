# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from paho.mqtt import client as paho

from picogrid_ecn_client._protocol import (
    ENTITY_JSON_SUBSCRIPTION,
    build_entity_topic,
    encode_entity_json,
)
from picogrid_ecn_client._transport import MQTTTransport
from picogrid_ecn_client.exceptions import AuthenticationError, AuthorizationError
from picogrid_ecn_client.models import Entity, EntityCategory, Location
from picogrid_ecn_client.testing import NO_ACCESS_TOKEN, MockECN


@pytest.mark.asyncio
async def test_real_aiomqtt_adapter_against_local_mock() -> None:
    entity_id = UUID("12345678-1234-5678-9234-567812345678")
    now = datetime(2026, 8, 5, 12, 30, tzinfo=UTC)
    async with MockECN() as mock:
        config = mock.client_config("transport-test").model_copy(
            update={"connection_timeout": 2.0, "reconnect_delay": 0.05}
        )
        reconnected = asyncio.Event()
        connection_states: list[bool] = []

        def on_connection_change(connected: bool) -> None:
            connection_states.append(connected)
            if connection_states.count(True) == 2:
                reconnected.set()

        mqtt = MQTTTransport(config, on_connection_change)
        received = asyncio.Event()

        async def on_entity(_topic: str, _payload: bytes) -> None:
            received.set()

        try:
            await mqtt.start()
            await mqtt.subscribe(ENTITY_JSON_SUBSCRIPTION, on_entity)
            entity = Entity(
                id=entity_id,
                category=EntityCategory.TRACK,
                integration="transport-test",
                recorded_at=now,
                type="UAV",
                position=Location(latitude=34.5, longitude=-117.25, recorded_at=now),
            )
            topic = build_entity_topic(entity.integration, entity.id, entity.category)
            await mqtt.publish(
                topic,
                encode_entity_json(entity, config.maximum_payload_size),
                qos=1,
            )
            async with asyncio.timeout(2):
                await received.wait()

            await mock.disconnect_clients()
            async with asyncio.timeout(2):
                await reconnected.wait()
            assert connection_states[:3] == [True, False, True]
            assert mqtt.connected
        finally:
            await mqtt.close()
        assert not mqtt.connected


@pytest.mark.asyncio
async def test_real_aiomqtt_authentication_rejection_is_translated() -> None:
    async with MockECN() as mock:
        config = mock.client_config("transport-test", token="invalid-synthetic-token").model_copy(
            update={"connection_timeout": 0.1, "reconnect_delay": 0.05}
        )
        transport = MQTTTransport(config)
        with pytest.raises(AuthenticationError):
            await transport.start()
        assert mock.events.authentication_failed.is_set()
        assert not transport.connected


@pytest.mark.asyncio
async def test_real_aiomqtt_subscription_rejection_is_translated() -> None:
    async with MockECN() as mock:
        config = mock.client_config("transport-test", token=NO_ACCESS_TOKEN).model_copy(
            update={"connection_timeout": 0.1, "reconnect_delay": 0.05}
        )
        transport = MQTTTransport(config)

        async def callback(_topic: str, _payload: bytes) -> None:
            return None

        await transport.start()
        with pytest.raises(AuthorizationError):
            await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)
        assert mock.events.authorization_denied.is_set()
        assert transport.connected
        await transport.close()


@pytest.mark.asyncio
async def test_real_aiomqtt_negative_puback_is_translated_without_disconnect() -> None:
    async with MockECN() as mock:
        config = mock.client_config("transport-test", token=NO_ACCESS_TOKEN).model_copy(
            update={"connection_timeout": 0.1, "reconnect_delay": 0.05}
        )
        transport = MQTTTransport(config)
        topic = build_entity_topic("transport-test", UUID(int=1), EntityCategory.DETECTION)

        await transport.start()
        with pytest.raises(AuthorizationError, match="publication") as raised:
            await transport.publish(topic, b"{}", qos=1)

        assert raised.value.operation == "mqtt.publish"
        assert mock.events.authorization_denied.is_set()
        assert transport.connected
        await transport.close()


@pytest.mark.asyncio
async def test_real_aiomqtt_negative_unsuback_fail_closes_and_reconnects() -> None:
    async with MockECN() as mock:
        config = mock.client_config("transport-test").model_copy(
            update={"connection_timeout": 0.1, "reconnect_delay": 0.05}
        )
        transport = MQTTTransport(config)

        async def callback(_topic: str, _payload: bytes) -> None:
            return None

        await transport.start()
        handle = await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)
        mock.events.mqtt_connected.clear()
        mock.set_authorization_failure("entity.read")
        with pytest.raises(AuthorizationError, match="unsubscription") as raised:
            await transport.unsubscribe(handle)

        assert raised.value.operation == "mqtt.unsubscribe"
        assert mock.events.authorization_denied.is_set()
        assert not transport.connected
        await asyncio.wait_for(mock.events.mqtt_connected.wait(), timeout=1)
        await asyncio.wait_for(transport._connected_event.wait(), timeout=1)
        await transport.close()


@pytest.mark.asyncio
async def test_negative_unsuback_force_closes_when_graceful_disconnect_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with MockECN() as mock:
        config = mock.client_config("transport-test").model_copy(
            update={"connection_timeout": 0.1, "reconnect_delay": 0.05}
        )
        transport = MQTTTransport(config)

        async def callback(_topic: str, _payload: bytes) -> None:
            return None

        await transport.start()
        handle = await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)
        old_adapter = transport._client
        assert old_adapter is not None
        monkeypatch.setattr(
            old_adapter._client,
            "disconnect",
            lambda: paho.MQTT_ERR_NO_CONN,
        )
        mock.events.mqtt_connected.clear()
        mock.set_authorization_failure("entity.read")

        with pytest.raises(AuthorizationError, match="unsubscription"):
            await transport.unsubscribe(handle)

        assert not transport.connected
        assert old_adapter._client.socket() is None
        await asyncio.wait_for(mock.events.mqtt_connected.wait(), timeout=1)
        await asyncio.wait_for(transport._connected_event.wait(), timeout=1)
        await transport.close()

    assert mock.active_connection_count == 0
    assert mock.active_task_count == 0
