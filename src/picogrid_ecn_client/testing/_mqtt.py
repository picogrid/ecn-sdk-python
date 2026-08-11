# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Small MQTT v5 broker used only by the offline ECN simulator.

This is deliberately not a general-purpose broker.  It implements the packet
types needed to exercise the public client and delegates every authentication
and authorization decision to :class:`MockECN`.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Literal, Protocol

_AcknowledgementKind = Literal["connack", "suback", "puback", "unsuback"]
_AcknowledgementResponse = int | Literal["malformed"]
_PublishDisconnectPhase = Literal[
    "qos0_before_completion",
    "before_puback",
    "after_puback",
]


class MQTTProtocolError(Exception):
    """Raised internally when a peer sends an invalid or unsupported packet."""


@dataclass(frozen=True, slots=True)
class BrokerIdentity:
    """Authenticated identity attached to one MQTT connection."""

    client_id: str
    integration_name: str
    token: str
    acl_grants: frozenset[str]


@dataclass(frozen=True, slots=True)
class PublishDecision:
    """Result of applying mock state and scenario behavior to a publication."""

    payload: bytes
    forward: bool = True


class BrokerDelegate(Protocol):
    """Authorization and scenario hooks supplied by the mock ECN."""

    async def mqtt_authenticate(
        self,
        *,
        client_id: str,
        username: str | None,
        password: str | None,
    ) -> BrokerIdentity | None: ...

    def mqtt_drop_connect(self) -> bool: ...

    def mqtt_acknowledgement_response(
        self,
        acknowledgement: _AcknowledgementKind,
    ) -> _AcknowledgementResponse | None: ...

    def mqtt_can_subscribe(self, identity: BrokerIdentity, topic_filter: str) -> bool: ...

    def mqtt_can_publish(self, identity: BrokerIdentity, topic: str) -> bool: ...

    async def mqtt_published(
        self,
        identity: BrokerIdentity,
        topic: str,
        payload: bytes,
    ) -> PublishDecision: ...

    def mqtt_publication_disconnect(self) -> _PublishDisconnectPhase | None: ...

    def mqtt_qos0_publication_disconnect(self) -> bool: ...

    async def mqtt_connected(self, identity: BrokerIdentity) -> None: ...

    async def mqtt_disconnected(self, identity: BrokerIdentity) -> None: ...

    async def mqtt_forwarded(self, topic: str, recipient_count: int) -> None: ...

    def mqtt_publication_disconnected(
        self,
        identity: BrokerIdentity,
        phase: _PublishDisconnectPhase,
    ) -> None: ...


@dataclass(eq=False, slots=True)
class _Connection:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    identity: BrokerIdentity | None = None
    subscriptions: dict[str, int] = field(default_factory=dict)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_packet_id: int = 1

    def packet_id(self) -> int:
        packet_id = self.next_packet_id
        self.next_packet_id = 1 if packet_id == 65_535 else packet_id + 1
        return packet_id


class MQTTBroker:
    """A fixed-purpose asyncio MQTT v5 broker."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        delegate: BrokerDelegate,
        maximum_packet_size: int,
    ) -> None:
        self._host = host
        self._requested_port = port
        self._delegate = delegate
        self._maximum_packet_size = maximum_packet_size
        self._server: asyncio.Server | None = None
        self._bound_port = port
        self._connections: set[_Connection] = set()
        self._accepted_writers: set[asyncio.StreamWriter] = set()
        self._connection_tasks: set[asyncio.Task[None]] = set()
        self._task_writers: dict[asyncio.Task[None], asyncio.StreamWriter] = {}
        self._closing = False

    @property
    def port(self) -> int:
        return self._bound_port

    @property
    def connection_count(self) -> int:
        return len(self._accepted_writers)

    @property
    def task_count(self) -> int:
        return sum(not task.done() for task in self._connection_tasks)

    async def start(self) -> None:
        if self._server is not None:
            return
        self._closing = False
        self._server = await asyncio.start_server(
            self._accept_connection,
            self._host,
            self._requested_port,
        )
        if self._server.sockets:
            self._bound_port = int(self._server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        self._closing = True
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        await self.disconnect_clients()
        if server is not None:
            await server.wait_closed()
        current = asyncio.current_task()
        tasks = [task for task in self._connection_tasks if task is not current]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connection_tasks.clear()

    def _accept_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Register an accepted connection before its coroutine can be scheduled."""

        if self._closing:
            writer.close()
            return
        self._accepted_writers.add(writer)
        task = asyncio.create_task(
            self._handle_connection(reader, writer),
            name="picogrid-ecn-mock-connection",
        )
        self._connection_tasks.add(task)
        self._task_writers[task] = writer
        task.add_done_callback(self._connection_task_done)

    def _connection_task_done(self, task: asyncio.Task[None]) -> None:
        self._connection_tasks.discard(task)
        writer = self._task_writers.pop(task, None)
        if writer is not None:
            self._accepted_writers.discard(writer)
            writer.close()
        if not task.cancelled():
            task.exception()

    async def disconnect_clients(self) -> None:
        writers = tuple(self._accepted_writers)
        for writer in writers:
            writer.close()
        if writers:
            await asyncio.gather(
                *(writer.wait_closed() for writer in writers),
                return_exceptions=True,
            )
        current = asyncio.current_task()
        tasks = [task for task in self._connection_tasks if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._accepted_writers.difference_update(writers)

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if self._closing:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()
            self._accepted_writers.discard(writer)
            return
        connection = _Connection(reader=reader, writer=writer)
        self._connections.add(connection)
        try:
            await self._connection_loop(connection)
        except (
            MQTTProtocolError,
            asyncio.IncompleteReadError,
            ConnectionError,
            UnicodeError,
        ):
            pass
        finally:
            self._connections.discard(connection)
            self._accepted_writers.discard(writer)
            identity = connection.identity
            connection.writer.close()
            with suppress(ConnectionError, OSError):
                await connection.writer.wait_closed()
            if identity is not None:
                await self._delegate.mqtt_disconnected(identity)

    async def _connection_loop(self, connection: _Connection) -> None:
        first_byte, payload = await self._read_packet(connection)
        if first_byte != 0x10:
            raise MQTTProtocolError("CONNECT must be the first packet")
        identity = await self._handle_connect(connection, payload)
        if identity is None:
            return
        connection.identity = identity
        await self._delegate.mqtt_connected(identity)

        while not self._closing and not connection.writer.is_closing():
            first_byte, payload = await self._read_packet(connection)
            packet_type = first_byte >> 4
            flags = first_byte & 0x0F
            if packet_type == 3:
                await self._handle_publish(connection, flags, payload)
            elif packet_type == 4:
                if flags != 0 or not _valid_acknowledgement(payload):
                    raise MQTTProtocolError("invalid PUBACK")
            elif packet_type == 8:
                if flags != 2:
                    raise MQTTProtocolError("invalid SUBSCRIBE flags")
                await self._handle_subscribe(connection, payload)
            elif packet_type == 10:
                if flags != 2:
                    raise MQTTProtocolError("invalid UNSUBSCRIBE flags")
                await self._handle_unsubscribe(connection, payload)
            elif packet_type == 12:
                if flags != 0 or payload:
                    raise MQTTProtocolError("invalid PINGREQ")
                await self._send_packet(connection, 0xD0, b"")
            elif packet_type == 14:
                if flags != 0 or not _valid_disconnect(payload):
                    raise MQTTProtocolError("invalid DISCONNECT")
                return
            else:
                raise MQTTProtocolError("unsupported MQTT packet type")

    async def _handle_connect(
        self,
        connection: _Connection,
        payload: bytes,
    ) -> BrokerIdentity | None:
        protocol_name, position = _decode_utf8(payload, 0)
        if position + 4 > len(payload):
            raise MQTTProtocolError("truncated CONNECT header")
        protocol_level = payload[position]
        connect_flags = payload[position + 1]
        position += 4  # protocol level, flags, two-byte keepalive

        if protocol_name != "MQTT" or protocol_level != 5:
            # The mock intentionally implements only the public MQTT v5 wire.
            connection.writer.close()
            return None
        if connect_flags & 0x01:
            raise MQTTProtocolError("reserved CONNECT flag is set")

        position = _skip_properties(payload, position)
        client_id, position = _decode_utf8(payload, position)
        will_flag = bool(connect_flags & 0x04)
        if will_flag:
            position = _skip_properties(payload, position)
            _, position = _decode_utf8(payload, position)
            _, position = _decode_binary(payload, position)

        username: str | None = None
        password: str | None = None
        if connect_flags & 0x80:
            username, position = _decode_utf8(payload, position)
        if connect_flags & 0x40:
            password_bytes, position = _decode_binary(payload, position)
            password = password_bytes.decode("utf-8")
        if position != len(payload):
            raise MQTTProtocolError("unexpected CONNECT payload bytes")

        if self._delegate.mqtt_drop_connect():
            connection.writer.close()
            return None

        identity = await self._delegate.mqtt_authenticate(
            client_id=client_id,
            username=username,
            password=password,
        )
        if identity is None:
            await self._send_packet(connection, 0x20, b"\x00\x86\x00")
            return None

        if self._delegate.mqtt_acknowledgement_response("connack") == "malformed":
            await self._send_malformed_acknowledgement(connection, 0x20)
            return None

        for existing in tuple(self._connections):
            if (
                existing is not connection
                and existing.identity is not None
                and existing.identity.client_id == identity.client_id
            ):
                existing.writer.close()
        await self._send_packet(connection, 0x20, b"\x00\x00\x00")
        return identity

    async def _handle_subscribe(self, connection: _Connection, payload: bytes) -> None:
        if len(payload) < 3:
            raise MQTTProtocolError("truncated SUBSCRIBE")
        packet_id = int.from_bytes(payload[:2], "big")
        if packet_id == 0:
            raise MQTTProtocolError("zero SUBSCRIBE packet identifier")
        position = _skip_properties(payload, 2)
        return_codes = bytearray()
        accepted: list[tuple[str, int]] = []
        identity = _require_identity(connection)
        response = self._delegate.mqtt_acknowledgement_response("suback")
        while position < len(payload):
            topic_filter, position = _decode_utf8(payload, position)
            if position >= len(payload):
                raise MQTTProtocolError("missing requested subscription QoS")
            options = payload[position]
            position += 1
            requested_qos = options & 0x03
            invalid_options = bool(options & 0xC0) or ((options >> 4) & 0x03) == 0x03
            if requested_qos > 1 or invalid_options or not _valid_topic_filter(topic_filter):
                return_codes.append(0x80)
            elif isinstance(response, int):
                return_codes.append(response)
            elif response == "malformed":
                return_codes.append(requested_qos)
            elif self._delegate.mqtt_can_subscribe(identity, topic_filter):
                accepted.append((topic_filter, requested_qos))
                return_codes.append(requested_qos)
            else:
                return_codes.append(0x87)
        if not return_codes:
            raise MQTTProtocolError("empty SUBSCRIBE")
        if response == "malformed":
            await self._send_malformed_acknowledgement(connection, 0x90, packet_id)
            return
        for topic_filter, requested_qos in accepted:
            connection.subscriptions[topic_filter] = requested_qos
        await self._send_packet(
            connection,
            0x90,
            packet_id.to_bytes(2, "big") + b"\x00" + bytes(return_codes),
        )

    async def _handle_unsubscribe(self, connection: _Connection, payload: bytes) -> None:
        if len(payload) < 4:
            raise MQTTProtocolError("truncated UNSUBSCRIBE")
        packet_id = int.from_bytes(payload[:2], "big")
        if packet_id == 0:
            raise MQTTProtocolError("zero UNSUBSCRIBE packet identifier")
        position = _skip_properties(payload, 2)
        removed = False
        reason_codes = bytearray()
        identity = _require_identity(connection)
        response = self._delegate.mqtt_acknowledgement_response("unsuback")
        while position < len(payload):
            topic_filter, position = _decode_utf8(payload, position)
            removed = True
            if isinstance(response, int):
                reason_codes.append(response)
            elif response == "malformed":
                reason_codes.append(0x00)
            elif self._delegate.mqtt_can_subscribe(identity, topic_filter):
                connection.subscriptions.pop(topic_filter, None)
                reason_codes.append(0x00)
            else:
                reason_codes.append(0x87)
        if not removed:
            raise MQTTProtocolError("empty UNSUBSCRIBE")
        if response == "malformed":
            await self._send_malformed_acknowledgement(connection, 0xB0, packet_id)
            return
        await self._send_packet(
            connection,
            0xB0,
            packet_id.to_bytes(2, "big") + b"\x00" + bytes(reason_codes),
        )

    async def _handle_publish(
        self,
        connection: _Connection,
        flags: int,
        payload: bytes,
    ) -> None:
        qos = (flags >> 1) & 0x03
        if qos > 1:
            raise MQTTProtocolError("QoS 2 is unsupported")
        topic, position = _decode_utf8(payload, 0)
        if not topic or "+" in topic or "#" in topic:
            raise MQTTProtocolError("invalid publication topic")
        packet_id: int | None = None
        if qos == 1:
            if position + 2 > len(payload):
                raise MQTTProtocolError("missing PUBLISH packet identifier")
            packet_id = int.from_bytes(payload[position : position + 2], "big")
            position += 2
            if packet_id == 0:
                raise MQTTProtocolError("zero PUBLISH packet identifier")
        position = _skip_properties(payload, position)
        message = payload[position:]
        identity = _require_identity(connection)
        if packet_id is not None:
            response = self._delegate.mqtt_acknowledgement_response("puback")
            if isinstance(response, int):
                await self._send_packet(
                    connection,
                    0x40,
                    packet_id.to_bytes(2, "big") + bytes((response, 0x00)),
                )
                return
            if response == "malformed":
                await self._send_malformed_acknowledgement(connection, 0x40, packet_id)
                return
        if not self._delegate.mqtt_can_publish(identity, topic):
            if packet_id is None:
                connection.writer.close()
            else:
                await self._send_packet(
                    connection,
                    0x40,
                    packet_id.to_bytes(2, "big") + b"\x87\x00",
                )
            return

        decision = await self._delegate.mqtt_published(identity, topic, message)
        disconnect = self._delegate.mqtt_publication_disconnect() if packet_id is not None else None
        if disconnect == "before_puback":
            if decision.forward:
                recipients = await self._fan_out(topic, decision.payload, qos)
                await self._delegate.mqtt_forwarded(topic, recipients)
            connection.writer.close()
            self._delegate.mqtt_publication_disconnected(identity, "before_puback")
            return
        if packet_id is not None and not connection.writer.is_closing():
            await self._send_packet(connection, 0x40, packet_id.to_bytes(2, "big"))
        if decision.forward:
            recipients = await self._fan_out(topic, decision.payload, qos)
            await self._delegate.mqtt_forwarded(topic, recipients)
        if disconnect == "after_puback":
            connection.writer.close()
            self._delegate.mqtt_publication_disconnected(identity, "after_puback")

    async def _fan_out(self, topic: str, payload: bytes, publication_qos: int) -> int:
        deliveries: list[tuple[_Connection, int]] = []
        for connection in tuple(self._connections):
            if connection.identity is None or connection.writer.is_closing():
                continue
            matching_qos = [
                qos
                for topic_filter, qos in connection.subscriptions.items()
                if _topic_matches(topic_filter, topic)
                and self._delegate.mqtt_can_subscribe(connection.identity, topic_filter)
            ]
            if matching_qos:
                deliveries.append((connection, min(publication_qos, max(matching_qos))))

        delivered = 0
        for connection, qos in deliveries:
            variable_header = _encode_utf8(topic)
            first_byte = 0x30
            if qos == 1:
                first_byte |= 0x02
                variable_header += connection.packet_id().to_bytes(2, "big")
            variable_header += b"\x00"
            try:
                await self._send_packet(connection, first_byte, variable_header + payload)
            except (ConnectionError, OSError):
                connection.writer.close()
            else:
                delivered += 1
        return delivered

    async def _read_packet(self, connection: _Connection) -> tuple[int, bytes]:
        first_byte = (await connection.reader.readexactly(1))[0]
        qos = (first_byte >> 1) & 0x03
        if (
            connection.identity is not None
            and first_byte >> 4 == 3
            and qos == 0
            and self._delegate.mqtt_qos0_publication_disconnect()
        ):
            identity = _require_identity(connection)
            connection.writer.close()
            self._delegate.mqtt_publication_disconnected(
                identity,
                "qos0_before_completion",
            )
            raise ConnectionError("synthetic QoS 0 publication interruption")
        remaining_length = 0
        multiplier = 1
        for index in range(4):
            encoded = (await connection.reader.readexactly(1))[0]
            remaining_length += (encoded & 0x7F) * multiplier
            if not encoded & 0x80:
                break
            multiplier *= 128
            if index == 3:
                raise MQTTProtocolError("invalid remaining-length encoding")
        if remaining_length > self._maximum_packet_size:
            raise MQTTProtocolError("MQTT packet exceeds the mock limit")
        return first_byte, await connection.reader.readexactly(remaining_length)

    async def _send_packet(
        self,
        connection: _Connection,
        first_byte: int,
        payload: bytes,
    ) -> None:
        packet = bytes((first_byte,)) + _encode_remaining_length(len(payload)) + payload
        async with connection.write_lock:
            connection.writer.write(packet)
            await connection.writer.drain()

    async def _send_malformed_acknowledgement(
        self,
        connection: _Connection,
        first_byte: int,
        packet_id: int | None = None,
    ) -> None:
        """Send a truncated MQTT v5 property length and close the connection."""

        prefix = b"" if packet_id is None else packet_id.to_bytes(2, "big")
        if first_byte == 0x20:
            prefix = b"\x00\x00"
        elif first_byte == 0x40:
            prefix += b"\x00"
        await self._send_packet(connection, first_byte, prefix + b"\x80")
        connection.writer.close()


def _require_identity(connection: _Connection) -> BrokerIdentity:
    if connection.identity is None:
        raise MQTTProtocolError("connection is not authenticated")
    return connection.identity


def _decode_utf8(data: bytes, position: int) -> tuple[str, int]:
    value, position = _decode_binary(data, position)
    if b"\x00" in value:
        raise MQTTProtocolError("MQTT UTF-8 field contains a null byte")
    return value.decode("utf-8"), position


def _decode_binary(data: bytes, position: int) -> tuple[bytes, int]:
    if position + 2 > len(data):
        raise MQTTProtocolError("truncated two-byte length")
    length = int.from_bytes(data[position : position + 2], "big")
    position += 2
    end = position + length
    if end > len(data):
        raise MQTTProtocolError("truncated length-prefixed value")
    return data[position:end], end


def _decode_variable_integer(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    multiplier = 1
    for index in range(4):
        if position >= len(data):
            raise MQTTProtocolError("truncated variable-byte integer")
        encoded = data[position]
        position += 1
        value += (encoded & 0x7F) * multiplier
        if not encoded & 0x80:
            return value, position
        multiplier *= 128
        if index == 3:
            break
    raise MQTTProtocolError("invalid variable-byte integer")


def _skip_properties(data: bytes, position: int) -> int:
    length, position = _decode_variable_integer(data, position)
    end = position + length
    if end > len(data):
        raise MQTTProtocolError("truncated MQTT properties")
    return end


def _valid_acknowledgement(payload: bytes) -> bool:
    if len(payload) < 2 or int.from_bytes(payload[:2], "big") == 0:
        return False
    if len(payload) in {2, 3}:
        return True
    try:
        return _skip_properties(payload, 3) == len(payload)
    except MQTTProtocolError:
        return False


def _valid_disconnect(payload: bytes) -> bool:
    if len(payload) <= 1:
        return True
    try:
        return _skip_properties(payload, 1) == len(payload)
    except MQTTProtocolError:
        return False


def _encode_utf8(value: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > 65_535:
        raise MQTTProtocolError("MQTT UTF-8 field is too long")
    return len(encoded).to_bytes(2, "big") + encoded


def _encode_remaining_length(length: int) -> bytes:
    if length < 0 or length > 268_435_455:
        raise MQTTProtocolError("invalid MQTT remaining length")
    encoded = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length:
            digit |= 0x80
        encoded.append(digit)
        if not length:
            return bytes(encoded)


def _valid_topic_filter(topic_filter: str) -> bool:
    if not topic_filter or "\x00" in topic_filter:
        return False
    levels = topic_filter.split("/")
    for index, level in enumerate(levels):
        if "#" in level and (level != "#" or index != len(levels) - 1):
            return False
        if "+" in level and level != "+":
            return False
    return True


def _topic_matches(topic_filter: str, topic: str) -> bool:
    filter_levels = topic_filter.split("/")
    topic_levels = topic.split("/")
    for index, filter_level in enumerate(filter_levels):
        if filter_level == "#":
            return True
        if index >= len(topic_levels):
            return False
        if filter_level != "+" and filter_level != topic_levels[index]:
            return False
    return len(filter_levels) == len(topic_levels)


__all__: list[str] = []
