# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import Iterable
from typing import cast

import pytest

from picogrid_ecn_client import BearerTokenAuth, NoAuth, ReviewedContainerNetwork
from picogrid_ecn_client.testing import (
    FULL_ACCESS_TOKEN,
    NO_ACCESS_TOKEN,
    READ_ONLY_TOKEN,
    MockECN,
)
from picogrid_ecn_client.testing.mock_ecn import main as mock_main


class _WriterProbe:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _remaining_length(length: int) -> bytes:
    result = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length:
            digit |= 0x80
        result.append(digit)
        if not length:
            return bytes(result)


def _field(value: str | bytes) -> bytes:
    encoded = value.encode() if isinstance(value, str) else value
    return len(encoded).to_bytes(2, "big") + encoded


async def _send_packet(writer: asyncio.StreamWriter, first_byte: int, payload: bytes) -> None:
    writer.write(bytes((first_byte,)) + _remaining_length(len(payload)) + payload)
    await writer.drain()


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    first_byte = (await reader.readexactly(1))[0]
    length = 0
    multiplier = 1
    while True:
        digit = (await reader.readexactly(1))[0]
        length += (digit & 0x7F) * multiplier
        if not digit & 0x80:
            break
        multiplier *= 128
    return first_byte, await reader.readexactly(length)


async def _connect(
    mock: MockECN,
    *,
    client_id: str,
    integration: str,
    token: str,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection(mock.host, mock.mqtt_port)
    variable_header = _field("MQTT") + b"\x05\xc2\x00\x3c\x00"
    payload = _field(client_id) + _field(integration) + _field(token)
    await _send_packet(writer, 0x10, variable_header + payload)
    first_byte, response = await _read_packet(reader)
    assert first_byte == 0x20
    assert response == b"\x00\x00\x00"
    return reader, writer


async def _subscribe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    filters: Iterable[tuple[str, int]],
    *,
    packet_id: int = 1,
) -> bytes:
    payload = packet_id.to_bytes(2, "big") + b"\x00"
    for topic_filter, qos in filters:
        payload += _field(topic_filter) + bytes((qos,))
    await _send_packet(writer, 0x82, payload)
    first_byte, response = await _read_packet(reader)
    assert first_byte == 0x90
    assert response[:3] == packet_id.to_bytes(2, "big") + b"\x00"
    return response[3:]


async def _unsubscribe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    topic_filter: str,
    *,
    packet_id: int = 7,
) -> bytes:
    payload = packet_id.to_bytes(2, "big") + b"\x00" + _field(topic_filter)
    await _send_packet(writer, 0xA2, payload)
    first_byte, response = await _read_packet(reader)
    assert first_byte == 0xB0
    assert response[:3] == packet_id.to_bytes(2, "big") + b"\x00"
    return response[3:]


async def _publish(
    writer: asyncio.StreamWriter,
    topic: str,
    payload: bytes,
    *,
    qos: int = 0,
    packet_id: int = 1,
) -> None:
    variable_header = _field(topic)
    if qos:
        variable_header += packet_id.to_bytes(2, "big")
    variable_header += b"\x00"
    await _send_packet(writer, 0x30 | (qos << 1), variable_header + payload)


def _published_payload(first_byte: int, packet: bytes) -> tuple[str, bytes]:
    topic_length = int.from_bytes(packet[:2], "big")
    position = 2 + topic_length
    topic = packet[2:position].decode()
    if ((first_byte >> 1) & 0x03) == 1:
        position += 2
    assert packet[position] == 0  # no MQTT v5 properties
    return topic, packet[position + 1 :]


async def _disconnect(writer: asyncio.StreamWriter) -> None:
    if not writer.is_closing():
        await _send_packet(writer, 0xE0, b"")
    writer.close()
    await writer.wait_closed()


@pytest.mark.parametrize(
    "wildcard_host",
    [str(ipaddress.IPv4Address(0)), str(ipaddress.IPv6Address(0))],
    ids=["ipv4-wildcard", "ipv6-wildcard"],
)
def test_mock_wildcard_bind_cannot_produce_client_config(wildcard_host: str) -> None:
    mock = MockECN(
        host=wildcard_host,
        mqtt_port=1883,
        allow_external_bind=True,
        allow_unauthenticated=True,
    )
    mock._running = True

    with pytest.raises(
        ValueError,
        match="wildcard bind address cannot produce a client configuration",
    ):
        mock.client_config(
            "external-integration",
            container_network="development-network",
        )


def test_mock_constructor_rejects_invalid_external_bind_host() -> None:
    with pytest.raises(ValueError, match="DNS name or IP literal"):
        MockECN(host="http://example.invalid", allow_external_bind=True)


def test_mock_cli_rejects_invalid_external_bind_host(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        mock_main(["--host", "http://example.invalid", "--allow-external-bind"])

    assert caught.value.code == 2
    assert "DNS name or IP literal" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_mock_lifecycle_config_and_socket_cleanup() -> None:
    mock = MockECN()
    await mock.start()
    await mock.start()
    mqtt_port = mock.mqtt_port
    assert mqtt_port > 0

    config = mock.client_config("fixture-integration")
    assert config.host == "127.0.0.1"
    assert config.mqtt_port == mqtt_port
    assert config.allow_insecure is True

    await mock.close()
    await mock.close()
    assert mock.active_connection_count == 0
    assert mock.active_task_count == 0

    probe = await asyncio.start_server(lambda _reader, _writer: None, mock.host, mqtt_port)
    probe.close()
    await probe.wait_closed()


@pytest.mark.asyncio
async def test_mock_loopback_client_config_remains_valid_bearer_configuration() -> None:
    async with MockECN() as mock:
        config = mock.client_config("loopback-integration")

    assert isinstance(config.auth, BearerTokenAuth)
    assert config.auth.token is not None
    assert config.auth.token.get_secret_value() == FULL_ACCESS_TOKEN
    assert config.allow_insecure is True
    assert config.plaintext_container_network is None


def test_mock_external_unauthenticated_client_config_uses_supplied_reviewed_network() -> None:
    mock = MockECN(
        host="mock.example.invalid",
        mqtt_port=1883,
        allow_external_bind=True,
        allow_unauthenticated=True,
    )
    mock._running = True

    config = mock.client_config(
        "external-integration",
        container_network="development-network",
    )

    assert isinstance(config.auth, NoAuth)
    assert config.auth.model_dump() == {"kind": "none"}
    assert config.tls.enabled is False
    assert config.allow_insecure is False
    assert config.plaintext_container_network == ReviewedContainerNetwork(
        name="development-network"
    )


def test_mock_external_client_config_requires_allow_unauthenticated() -> None:
    mock = MockECN(
        host="mock.example.invalid",
        mqtt_port=1883,
        allow_external_bind=True,
    )
    mock._running = True

    with pytest.raises(
        ValueError,
        match=r"allow_external_bind=True.*allow_unauthenticated=True",
    ):
        mock.client_config(
            "external-integration",
            container_network="development-network",
        )


def test_mock_external_unauthenticated_client_config_requires_network_name() -> None:
    mock = MockECN(
        host="mock.example.invalid",
        mqtt_port=1883,
        allow_external_bind=True,
        allow_unauthenticated=True,
    )
    mock._running = True

    with pytest.raises(ValueError, match="container_network"):
        mock.client_config("external-integration")


@pytest.mark.asyncio
async def test_mock_accepts_only_mqtt_v5() -> None:
    async with MockECN() as mock:
        reader, writer = await asyncio.open_connection(mock.host, mock.mqtt_port)
        old_header = _field("MQTT") + b"\x04\xc2\x00\x3c"
        payload = _field("old-client") + _field("old") + _field(FULL_ACCESS_TOKEN)
        await _send_packet(writer, 0x10, old_header + payload)
        assert await asyncio.wait_for(reader.read(1), timeout=1) == b""
        writer.close()
        await writer.wait_closed()

        reader, writer = await _connect(
            mock,
            client_id="v5-client",
            integration="v5",
            token=FULL_ACCESS_TOKEN,
        )
        await _send_packet(writer, 0xC0, b"")
        assert await _read_packet(reader) == (0xD0, b"")
        await _disconnect(writer)


@pytest.mark.asyncio
async def test_narrow_filter_fanout_and_observed_state() -> None:
    entity_id = "00000000-0000-4000-8000-000000000101"
    entity = {
        "id": entity_id,
        "integration_name": "publisher",
        "recorded_at": "2026-08-05T12:00:00Z",
        "category": "TRACK",
        "type_": "synthetic-track",
        "metadata": {"quality": "test"},
    }
    location = {
        "latitude": 34.0,
        "longitude": -118.0,
        "recorded_at": "2026-08-05T12:00:01Z",
        "source": "synthetic",
    }

    async with MockECN() as mock:
        subscriber_reader, subscriber_writer = await _connect(
            mock,
            client_id="subscriber-client",
            integration="subscriber",
            token=READ_ONLY_TOKEN,
        )
        entity_filter = "entity/publisher/+/track"
        location_filter = f"entity_location/publisher/{entity_id}"
        assert (
            await _subscribe(
                subscriber_reader,
                subscriber_writer,
                ((entity_filter, 1), (location_filter, 0)),
            )
            == b"\x01\x00"
        )

        publisher_reader, publisher_writer = await _connect(
            mock,
            client_id="publisher-client",
            integration="publisher",
            token=FULL_ACCESS_TOKEN,
        )
        entity_topic = f"entity/publisher/{entity_id}/track"
        await _publish(
            publisher_writer,
            entity_topic,
            json.dumps(entity).encode(),
            qos=1,
            packet_id=41,
        )
        assert await _read_packet(publisher_reader) == (0x40, b"\x00)")
        first_byte, packet = await _read_packet(subscriber_reader)
        assert first_byte == 0x32
        assert _published_payload(first_byte, packet) == (
            entity_topic,
            json.dumps(entity).encode(),
        )

        location_topic = f"entity_location/publisher/{entity_id}"
        await _publish(
            publisher_writer,
            location_topic,
            json.dumps({"location": location}).encode(),
        )
        first_byte, packet = await _read_packet(subscriber_reader)
        assert _published_payload(first_byte, packet) == (
            location_topic,
            json.dumps({"location": location}).encode(),
        )
        assert mock.entity_state[entity_id]["category"] == "TRACK"
        observed_location = mock.location_state[entity_id]
        assert {key: observed_location[key] for key in location} == location
        assert all(
            observed_location[field] is None
            for field in (
                "altitude",
                "bearing",
                "accuracy",
                "velocity",
                "angular_velocity",
                "confidence",
            )
        )

        await _unsubscribe(subscriber_reader, subscriber_writer, entity_filter)
        await _disconnect(publisher_writer)
        await _disconnect(subscriber_writer)


@pytest.mark.asyncio
async def test_acl_rejects_broad_filters_and_denied_operations() -> None:
    async with MockECN() as mock:
        reader, writer = await _connect(
            mock,
            client_id="no-access-client",
            integration="blocked",
            token=NO_ACCESS_TOKEN,
        )
        assert await _subscribe(reader, writer, (("entity/+/+/track", 0),)) == b"\x87"
        assert mock.events.authorization_denied.is_set()
        await _disconnect(writer)

        reader, writer = await _connect(
            mock,
            client_id="read-client",
            integration="reader",
            token=READ_ONLY_TOKEN,
        )
        assert await _subscribe(reader, writer, (("entity/#", 0),), packet_id=2) == b"\x87"
        assert await _subscribe(reader, writer, (("entity/+/+/+", 0),), packet_id=3) == b"\x00"
        mock.set_authorization_failure("entity.read")
        assert await _subscribe(reader, writer, (("entity/+/+/track", 0),), packet_id=4) == b"\x87"
        mock.set_authorization_failure("entity.read", enabled=False)
        assert await _subscribe(reader, writer, (("entity/+/+/track", 0),), packet_id=5) == b"\x00"
        mock.set_authorization_failure("entity.read")
        assert (
            await _unsubscribe(
                reader,
                writer,
                "entity/+/+/track",
                packet_id=6,
            )
            == b"\x87"
        )
        await _disconnect(writer)

        reader, writer = await _connect(
            mock,
            client_id="read-only-publisher",
            integration="reader",
            token=READ_ONLY_TOKEN,
        )
        await _publish(
            writer,
            "entity/reader/00000000-0000-4000-8000-000000000102/track",
            b"{}",
            qos=1,
            packet_id=7,
        )
        assert await _read_packet(reader) == (0x40, b"\x00\x07\x87\x00")
        await _disconnect(writer)


@pytest.mark.asyncio
async def test_task_acl_supports_exact_request_and_response_topics() -> None:
    entity_id = "00000000-0000-4000-8000-000000000104"
    request_topic = f"task/handler/{entity_id}/slew"
    response_topic = f"{request_topic}/response"
    async with MockECN() as mock:
        handler_reader, handler_writer = await _connect(
            mock,
            client_id="handler-client",
            integration="handler",
            token=FULL_ACCESS_TOKEN,
        )
        assert await _subscribe(handler_reader, handler_writer, ((request_topic, 1),)) == b"\x01"
        assert (
            await _subscribe(
                handler_reader,
                handler_writer,
                ((f"task/handler/{entity_id}/+", 1),),
                packet_id=2,
            )
            == b"\x87"
        )
        sender_reader, sender_writer = await _connect(
            mock,
            client_id="sender-client",
            integration="sender",
            token=FULL_ACCESS_TOKEN,
        )
        assert await _subscribe(sender_reader, sender_writer, ((response_topic, 1),)) == b"\x01"

        request = b'{"source":"local","task_id":"task-1","_response_mode":"complete","payload":{}}'
        await _publish(sender_writer, request_topic, request, qos=1, packet_id=11)
        assert await _read_packet(sender_reader) == (0x40, b"\x00\x0b")
        first_byte, packet = await _read_packet(handler_reader)
        assert _published_payload(first_byte, packet) == (request_topic, request)

        response = b'{"task_id":"task-1","status":"SUCCESS","payload":{}}'
        await _publish(handler_writer, response_topic, response, qos=1, packet_id=12)
        assert await _read_packet(handler_reader) == (0x40, b"\x00\x0c")
        first_byte, packet = await _read_packet(sender_reader)
        assert _published_payload(first_byte, packet) == (response_topic, response)

        await _disconnect(handler_writer)
        await _disconnect(sender_writer)


@pytest.mark.asyncio
async def test_fault_scenarios_and_forced_disconnect_are_deterministic() -> None:
    async with MockECN() as mock:
        subscriber_reader, subscriber_writer = await _connect(
            mock,
            client_id="scenario-subscriber",
            integration="subscriber",
            token=FULL_ACCESS_TOKEN,
        )
        assert (
            await _subscribe(subscriber_reader, subscriber_writer, (("entity/+/+/track", 0),))
            == b"\x00"
        )
        _, publisher_writer = await _connect(
            mock,
            client_id="scenario-publisher",
            integration="publisher",
            token=FULL_ACCESS_TOKEN,
        )
        topic = "entity/publisher/00000000-0000-4000-8000-000000000103/track"

        mock.drop_next_messages()
        await _publish(publisher_writer, topic, b"{}")
        await asyncio.wait_for(mock.events.message_dropped.wait(), timeout=1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(subscriber_reader.readexactly(1), timeout=0.05)

        mock.malform_next_messages()
        await _publish(publisher_writer, topic, b"{}")
        first_byte, packet = await asyncio.wait_for(_read_packet(subscriber_reader), timeout=1)
        assert _published_payload(first_byte, packet) == (topic, b'{"malformed":')

        await mock.disconnect_clients()
        assert mock.events.forced_disconnect.is_set()
        assert await asyncio.wait_for(subscriber_reader.read(1), timeout=1) == b""
        publisher_writer.close()
        subscriber_writer.close()
        await publisher_writer.wait_closed()
        await subscriber_writer.wait_closed()


@pytest.mark.asyncio
async def test_publication_disconnect_controls_preserve_delivery_boundaries() -> None:
    topic = "entity/publisher/00000000-0000-4000-8000-000000000702/track"
    task_id = "00000000-0000-4000-8000-000000000703"
    request_topic = f"task/handler/{task_id}/synthetic"
    response_topic = f"{request_topic}/response"
    async with MockECN() as mock:
        before_reader, before_writer = await _connect(
            mock,
            client_id="before-send-client",
            integration="publisher",
            token=FULL_ACCESS_TOKEN,
        )
        await mock.disconnect_before_publish()
        assert await asyncio.wait_for(before_reader.read(1), timeout=1) == b""
        assert mock.events.publication_disconnected_before_send.is_set()
        assert not mock.events.message_received.is_set()
        before_writer.close()
        await before_writer.wait_closed()

        subscriber_reader, subscriber_writer = await _connect(
            mock,
            client_id="delivery-subscriber",
            integration="subscriber",
            token=FULL_ACCESS_TOKEN,
        )
        assert await _subscribe(subscriber_reader, subscriber_writer, ((topic, 1),)) == b"\x01"

        qos0_reader, qos0_writer = await _connect(
            mock,
            client_id="qos0-client",
            integration="publisher",
            token=FULL_ACCESS_TOKEN,
        )
        mock.disconnect_next_publications("qos0_before_completion")
        await _publish(qos0_writer, topic, b'{"phase":"qos1"}', qos=1, packet_id=23)
        assert await _read_packet(qos0_reader) == (0x40, b"\x00\x17")
        first_byte, packet = await _read_packet(subscriber_reader)
        assert _published_payload(first_byte, packet) == (topic, b'{"phase":"qos1"}')
        assert mock.scenario.publication_disconnects == ["qos0_before_completion"]

        mock.events.message_received.clear()
        mock.events.message_forwarded.clear()
        await _publish(qos0_writer, topic, b'{"phase":"qos0"}', qos=0)
        assert await asyncio.wait_for(qos0_reader.read(1), timeout=1) == b""
        assert mock.events.publication_disconnected_qos0_before_completion.is_set()
        assert not mock.events.message_received.is_set()
        assert not mock.events.message_forwarded.is_set()
        assert not mock.scenario.publication_disconnects
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(subscriber_reader.readexactly(1), timeout=0.05)
        qos0_writer.close()
        await qos0_writer.wait_closed()

        before_ack_reader, before_ack_writer = await _connect(
            mock,
            client_id="before-ack-client",
            integration="publisher",
            token=FULL_ACCESS_TOKEN,
        )
        mock.disconnect_next_publications("before_puback")
        await _publish(before_ack_writer, topic, b'{"phase":"before"}', qos=1, packet_id=24)
        assert await asyncio.wait_for(before_ack_reader.read(1), timeout=1) == b""
        first_byte, packet = await _read_packet(subscriber_reader)
        assert _published_payload(first_byte, packet) == (topic, b'{"phase":"before"}')
        assert mock.events.publication_disconnected_before_puback.is_set()
        before_ack_writer.close()
        await before_ack_writer.wait_closed()

        handler_reader, handler_writer = await _connect(
            mock,
            client_id="task-handler-client",
            integration="handler",
            token=FULL_ACCESS_TOKEN,
        )
        assert await _subscribe(handler_reader, handler_writer, ((request_topic, 1),)) == b"\x01"

        after_ack_reader, after_ack_writer = await _connect(
            mock,
            client_id="task-sender-client",
            integration="sender",
            token=FULL_ACCESS_TOKEN,
        )
        assert (
            await _subscribe(after_ack_reader, after_ack_writer, ((response_topic, 1),)) == b"\x01"
        )
        mock.disconnect_next_publications("after_puback")
        request = b'{"source":"local","task_id":"synthetic","payload":{}}'
        await _publish(after_ack_writer, request_topic, request, qos=1, packet_id=25)
        assert await _read_packet(after_ack_reader) == (0x40, b"\x00\x19")
        assert await asyncio.wait_for(after_ack_reader.read(1), timeout=1) == b""
        first_byte, packet = await _read_packet(handler_reader)
        assert _published_payload(first_byte, packet) == (request_topic, request)
        assert mock.events.publication_disconnected_after_puback.is_set()
        after_ack_writer.close()
        await after_ack_writer.wait_closed()

        await _disconnect(handler_writer)
        await _disconnect(subscriber_writer)

    assert mock.active_connection_count == 0
    assert mock.active_task_count == 0


def test_mock_configuration_controls() -> None:
    with pytest.raises(ValueError, match="host"):
        MockECN(host="")
    with pytest.raises(ValueError, match="loopback"):
        MockECN(host="0.0.0.0")
    with pytest.raises(ValueError, match="loopback"):
        MockECN(host="mock.example.invalid")
    assert MockECN(host="localhost").host == "localhost"
    assert MockECN(host="::1").host == "::1"
    with pytest.raises(ValueError, match="port"):
        MockECN(mqtt_port=-1)
    with pytest.raises(ValueError, match="packet"):
        MockECN(maximum_packet_size=128)
    with pytest.raises(ValueError, match="token"):
        MockECN(tokens={"": set()})
    with pytest.raises(ValueError, match="ACL"):
        MockECN(tokens={"custom": {"not-an-operation"}})

    mock = MockECN(tokens={"custom": {"entity.read"}})
    with pytest.raises(RuntimeError, match="started"):
        mock.client_config("before-start")
    with pytest.raises(ValueError, match="finite"):
        mock.set_delay(float("inf"))
    with pytest.raises(ValueError, match="operation"):
        mock.set_delay(1, operation="not-real")
    with pytest.raises(ValueError, match="drop"):
        mock.drop_next_messages(-1)
    with pytest.raises(ValueError, match="malformed"):
        mock.malform_next_messages(-1)
    with pytest.raises(ValueError, match="ACL"):
        mock.set_authorization_failure("not-real")
    with pytest.raises(ValueError, match="phase"):
        mock.disconnect_next_publications("after_response")

    mock.set_delay(0.01)
    mock.set_authorization_failure("entity.read")
    mock.disconnect_next_publications("before_puback", 2)
    mock.events.message_received.set()
    mock.events.publication_disconnected_qos0_before_completion.set()
    mock.reset_scenario()
    assert not mock.scenario.delays
    assert not mock.scenario.denied_operations
    assert not mock.scenario.publication_disconnects
    assert not mock.events.message_received.is_set()
    assert not mock.events.publication_disconnected_qos0_before_completion.is_set()


@pytest.mark.asyncio
async def test_authentication_failure_is_bounded_and_secret_safe() -> None:
    async with MockECN() as mock:
        reader, writer = await asyncio.open_connection(mock.host, mock.mqtt_port)
        header = _field("MQTT") + b"\x05\xc2\x00\x3c\x00"
        payload = _field("bad-client") + _field("bad") + _field("invalid-synthetic-token")
        await _send_packet(writer, 0x10, header + payload)
        assert await _read_packet(reader) == (0x20, b"\x00\x86\x00")
        assert mock.events.authentication_failed.is_set()
        assert "invalid-synthetic-token" not in repr(mock.events)
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_delayed_publication_does_not_block_mock_shutdown() -> None:
    """Regression for the Python 3.13 server-close ordering hang."""

    mock = MockECN()
    await mock.start()
    _, writer = await _connect(
        mock,
        client_id="delayed-publisher",
        integration="publisher",
        token=FULL_ACCESS_TOKEN,
    )
    try:
        mock.set_delay(30, operation="mqtt.publish")
        await _publish(
            writer,
            "entity/publisher/00000000-0000-4000-8000-000000000601/track",
            b"{}",
        )
        await asyncio.wait_for(mock.events.message_received.wait(), timeout=1)
        await asyncio.wait_for(mock.close(), timeout=1)
        assert mock.active_task_count == 0
    finally:
        writer.close()
        await writer.wait_closed()
        await mock.close()


@pytest.mark.asyncio
async def test_accepted_connection_handler_is_registered_before_shutdown_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = MockECN()
    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()
    original = mock._broker._handle_connection

    async def delayed_handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        handler_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise
        finally:
            writer.close()
        await original(reader, writer)

    monkeypatch.setattr(mock._broker, "_handle_connection", delayed_handler)
    await mock.start()
    reader, writer = await asyncio.open_connection(mock.host, mock.mqtt_port)
    del reader
    await asyncio.wait_for(handler_started.wait(), timeout=1)
    assert mock.active_task_count == 1

    await asyncio.wait_for(mock.close(), timeout=1)

    assert handler_cancelled.is_set()
    assert mock.active_connection_count == 0
    assert mock.active_task_count == 0
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_never_started_accepted_handler_still_closes_its_writer() -> None:
    mock = MockECN()
    writer = _WriterProbe()
    mock._broker._accept_connection(
        asyncio.StreamReader(),
        cast(asyncio.StreamWriter, writer),
    )
    assert mock.active_connection_count == 1
    assert mock.active_task_count == 1

    await asyncio.wait_for(mock.close(), timeout=1)

    assert writer.closed
    assert mock.active_connection_count == 0
    assert mock.active_task_count == 0


def test_mock_host_is_read_only() -> None:
    """The validated loopback host cannot be replaced after construction."""

    mock = MockECN()
    assert mock.host == "127.0.0.1"
    with pytest.raises(AttributeError):
        mock.host = "replaced"  # type: ignore[misc]
    assert mock.host == "127.0.0.1"
