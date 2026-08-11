# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Fixed-purpose MQTT v5 simulator for the confirmed public wire subset."""

from __future__ import annotations

import argparse
import asyncio
import copy
import ipaddress
import math
import signal
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from pydantic import SecretStr

from .._network import normalize_host
from .._protocol import (
    EntityTopic,
    LocationTopic,
    TaskTopic,
    decode_entity_payload,
    decode_location_payload,
    parse_protocol_topic,
)
from ..auth import BearerTokenAuth, NoAuth, TLSConfig
from ..config import ECNConfig, ReviewedContainerNetwork
from ..exceptions import ECNClientError
from ._mqtt import BrokerIdentity, MQTTBroker, PublishDecision

FULL_ACCESS_TOKEN = "mock-full-access"
"""Synthetic token with every mock-only ACL label for offline tests."""
READ_ONLY_TOKEN = "mock-read-only"
"""Synthetic token with mock-only entity and location read labels."""
NO_ACCESS_TOKEN = "mock-no-access"
"""Synthetic token with no mock-only ACL labels."""

_ENTITY_READ = "entity.read"
_ENTITY_WRITE = "entity.write"
_LOCATION_READ = "location.read"
_LOCATION_WRITE = "location.write"
_TASK_RECEIVE = "task.receive"
_TASK_SEND = "task.send"
_ALL_OPERATIONS = frozenset(
    {
        _ENTITY_READ,
        _ENTITY_WRITE,
        _LOCATION_READ,
        _LOCATION_WRITE,
        _TASK_RECEIVE,
        _TASK_SEND,
    }
)
_READ_OPERATIONS = frozenset({_ENTITY_READ, _LOCATION_READ})
_MALFORMED_PAYLOAD = b'{"malformed":'
_FILTER_UUID = "00000000-0000-4000-8000-000000000000"
_ACKNOWLEDGEMENTS = frozenset({"suback", "puback", "unsuback"})
_PROTOCOL_RESPONSES = frozenset({"connack", "suback", "puback", "unsuback"})
_PUBLISH_DISCONNECT_PHASES = frozenset({"qos0_before_completion", "before_puback", "after_puback"})
_MAX_FAULT_COUNT = 100
_AcknowledgementKind = Literal["connack", "suback", "puback", "unsuback"]
_AcknowledgementResponse = int | Literal["malformed"]
_PublishDisconnectPhase = Literal[
    "qos0_before_completion",
    "before_puback",
    "after_puback",
]


def _is_loopback_host(host: object) -> bool:
    if not isinstance(host, str) or not host:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_wildcard_bind_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def _loopback_host_argument(
    value: str,
    *,
    allow_external_bind: bool = False,
) -> str:
    try:
        host = normalize_host(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    if not _is_loopback_host(host) and not allow_external_bind:
        raise argparse.ArgumentTypeError("mock host must be localhost or a loopback IP literal")
    return host


def _integration_name_from_client_id(client_id: str) -> str:
    integration_name, separator, suffix = client_id.rpartition("-")
    if (
        separator
        and integration_name
        and len(suffix) == 8
        and all(character in "0123456789abcdef" for character in suffix)
    ):
        return integration_name
    return client_id


@dataclass(slots=True)
class MockEvents:
    """Expose synchronization events for deterministic asynchronous tests."""

    mqtt_connected: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when an MQTT v5 client connection is accepted."""
    mqtt_disconnected: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when an accepted MQTT v5 client disconnects."""
    message_received: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when the mock receives a client publication."""
    message_forwarded: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when the mock forwards a publication to subscribed clients."""
    message_dropped: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when a configured drop suppresses publication forwarding."""
    malformed_message_sent: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when a configured malformed payload is forwarded."""
    authentication_failed: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when synthetic authentication rejects a client."""
    authorization_denied: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when a synthetic mock-only ACL label denies an operation."""
    forced_disconnect: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when deterministic forced disconnection is requested."""
    connection_dropped: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when a transient connection drop consumes one configured attempt."""
    acknowledgement_denied: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when an operation-specific negative acknowledgment is returned."""
    acknowledgement_reason_returned: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when one configured raw MQTT reason code is returned."""
    malformed_protocol_response: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when the mock sends one deliberately malformed MQTT response."""
    publication_disconnected_before_send: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when clients are disconnected before a caller starts publication."""
    publication_disconnected_qos0_before_completion: asyncio.Event = field(
        default_factory=asyncio.Event
    )
    """Set when a QoS 0 PUBLISH is interrupted after its fixed header."""
    publication_disconnected_before_puback: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when a processed QoS 1 publication is disconnected before PUBACK."""
    publication_disconnected_after_puback: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when a QoS 1 publisher is disconnected immediately after PUBACK."""

    def reset(self) -> None:
        """Clear every synchronization event without replacing event objects."""

        self.mqtt_connected.clear()
        self.mqtt_disconnected.clear()
        self.message_received.clear()
        self.message_forwarded.clear()
        self.message_dropped.clear()
        self.malformed_message_sent.clear()
        self.authentication_failed.clear()
        self.authorization_denied.clear()
        self.forced_disconnect.clear()
        self.connection_dropped.clear()
        self.acknowledgement_denied.clear()
        self.acknowledgement_reason_returned.clear()
        self.malformed_protocol_response.clear()
        self.publication_disconnected_before_send.clear()
        self.publication_disconnected_qos0_before_completion.clear()
        self.publication_disconnected_before_puback.clear()
        self.publication_disconnected_after_puback.clear()


@dataclass(slots=True)
class MockScenario:
    """Store mutable deterministic fault controls for one mock instance."""

    delays: dict[str, float] = field(default_factory=dict)
    """Delays in seconds keyed by a supported mock operation."""
    drop_messages_remaining: int = 0
    """Number of upcoming client publications to suppress."""
    malformed_messages_remaining: int = 0
    """Number of upcoming client publications to replace with malformed data."""
    denied_operations: set[str] = field(default_factory=set)
    """Mock-only ACL operation labels currently forced to deny."""
    connect_drops_remaining: int = 0
    """Number of upcoming valid CONNECT attempts to close without CONNACK."""
    authentication_rejections_remaining: int = 0
    """Number of upcoming otherwise-valid authentications to reject."""
    response_faults: dict[str, list[_AcknowledgementResponse]] = field(default_factory=dict)
    """Finite response-fault queues keyed by MQTT acknowledgement name."""
    publication_disconnects: list[_PublishDisconnectPhase] = field(default_factory=list)
    """Finite publication-disconnect queue with explicit QoS phase."""

    def reset(self) -> None:
        """Restore every fault control to its deterministic default."""
        self.delays.clear()
        self.drop_messages_remaining = 0
        self.malformed_messages_remaining = 0
        self.denied_operations.clear()
        self.connect_drops_remaining = 0
        self.authentication_rejections_remaining = 0
        self.response_faults.clear()
        self.publication_disconnects.clear()


class MockECN:
    """Run a deterministic MQTT v5 test broker.

    Synthetic tokens map to mock-only ACL operation labels. They exercise local
    allow and deny behavior and do not describe a server authorization contract.
    External binding and credential-free connections are explicit, default-off
    development and CI facilities; this mock is never a production affordance.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        mqtt_port: int = 0,
        tokens: Mapping[str, Iterable[str]] | None = None,
        allow_external_bind: bool = False,
        allow_unauthenticated: bool = False,
        maximum_packet_size: int = 1024 * 1024,
    ) -> None:
        """Create a stopped offline mock.

        Args:
            host: Hostname or IP literal on which to listen; non-loopback values require
                the development/CI-only ``allow_external_bind`` opt-in.
            mqtt_port: Listener port, or ``0`` to select an available port.
            tokens: Synthetic token-to-mock-ACL mappings, or built-in test mappings.
            allow_external_bind: Whether this development/CI-only mock may bind a
                non-loopback address; defaults to false.
            allow_unauthenticated: Whether this development/CI-only mock accepts
                credential-free MQTT CONNECT packets; defaults to false.
            maximum_packet_size: MQTT packet-size bound in bytes.

        Raises:
            ValueError: If the endpoint, packet bound, token, or mock ACL label is
                invalid.
        """
        host = normalize_host(host)
        if not _is_loopback_host(host) and not allow_external_bind:
            raise ValueError(
                "mock host must be localhost or a loopback IP literal unless "
                "allow_external_bind=True"
            )
        if not 0 <= mqtt_port <= 65_535:
            raise ValueError("mock MQTT port must be between 0 and 65535")
        if maximum_packet_size < 1024:
            raise ValueError("maximum_packet_size must be at least 1024 bytes")

        self._host = host
        self._maximum_packet_size = maximum_packet_size
        self._tokens = _normalize_tokens(tokens)
        self._allow_unauthenticated = allow_unauthenticated
        self.scenario: MockScenario = MockScenario()
        """Mutable deterministic controls for this mock instance."""
        self.events: MockEvents = MockEvents()
        """Synchronization events for this mock instance."""
        self._entities: dict[str, dict[str, Any]] = {}
        self._locations: dict[str, dict[str, Any]] = {}
        self._broker = MQTTBroker(
            host=host,
            port=mqtt_port,
            delegate=self,
            maximum_packet_size=maximum_packet_size,
        )
        self._running = False

    @property
    def host(self) -> str:
        """Return the read-only validated loopback host the listener is bound to.

        Assignment raises `AttributeError`, so the loopback restriction checked
        at construction cannot be replaced before `client_config` reads it.
        """
        return self._host

    @property
    def mqtt_port(self) -> int:
        """Return the bound MQTT listener port."""
        return self._broker.port

    @property
    def is_running(self) -> bool:
        """Return whether the MQTT listener is running."""
        return self._running

    @property
    def active_connection_count(self) -> int:
        """Return the number of currently accepted MQTT client connections."""
        return self._broker.connection_count

    @property
    def active_task_count(self) -> int:
        """Return the number of live asynchronous mock broker tasks."""
        return self._broker.task_count

    @property
    def entity_state(self) -> Mapping[str, Mapping[str, Any]]:
        """Return a defensive snapshot of entities observed by the mock."""

        return copy.deepcopy(self._entities)

    @property
    def location_state(self) -> Mapping[str, Mapping[str, Any]]:
        """Return a defensive snapshot of locations observed by the mock."""

        return copy.deepcopy(self._locations)

    async def start(self) -> None:
        """Start the MQTT listener and wait until it accepts connections.

        Repeated calls are safe while the mock is running.

        Raises:
            OSError: If the configured port is occupied or the operating system
                otherwise refuses the loopback or explicitly allowed external bind.
        """

        if self._running:
            return
        await self._broker.start()
        self._running = True

    async def close(self) -> None:
        """Close the listener and all accepted clients idempotently."""

        self._running = False
        await self._broker.close()

    async def __aenter__(self) -> MockECN:
        """Start and return the mock as an asynchronous context manager."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        """Close the mock when leaving the asynchronous context."""
        await self.close()

    def client_config(
        self,
        integration_name: str,
        token: str = FULL_ACCESS_TOKEN,
        *,
        container_network: str | None = None,
    ) -> ECNConfig:
        """Build a development/CI-only client configuration for this mock.

        Loopback listeners retain the explicitly insecure synthetic bearer-token
        configuration. An explicitly external, unauthenticated listener instead
        requires the caller to name the reviewed container network attestation;
        the helper never infers or invents that attestation.

        Args:
            integration_name: Integration identity for the test client.
            token: Synthetic token understood only by a loopback mock.
            container_network: Caller-supplied reviewed container-network name,
                required for a non-loopback unauthenticated mock.

        Returns:
            A client configuration pinned to the running mock listener.

        Raises:
            RuntimeError: If the mock has not been started.
            ValueError: If an external listener does not allow unauthenticated
                clients or lacks an explicit container-network attestation.
        """

        if not self._running:
            raise RuntimeError("mock ECN must be started before creating client config")
        if _is_loopback_host(self.host):
            return ECNConfig(
                host=self.host,
                integration_name=integration_name,
                mqtt_port=self.mqtt_port,
                auth=BearerTokenAuth(token=SecretStr(token)),
                tls=TLSConfig(enabled=False),
                allow_insecure=True,
            )
        if _is_wildcard_bind_host(self.host):
            raise ValueError(
                "a wildcard bind address cannot produce a client configuration; "
                "supply a concrete reachable address as host"
            )
        if not self._allow_unauthenticated:
            raise ValueError(
                "external client configuration is unsupported with "
                "allow_external_bind=True and allow_unauthenticated=False; "
                "set allow_unauthenticated=True"
            )
        if not container_network:
            raise ValueError(
                "container_network is required when allow_external_bind=True and "
                "allow_unauthenticated=True"
            )
        return ECNConfig(
            host=self.host,
            integration_name=integration_name,
            mqtt_port=self.mqtt_port,
            auth=NoAuth(),
            tls=TLSConfig(enabled=False),
            plaintext_container_network=ReviewedContainerNetwork(name=container_network),
        )

    def set_delay(self, seconds: float, *, operation: str = "all") -> None:
        """Configure a deterministic publication-handling delay.

        Args:
            seconds: Finite non-negative delay in seconds; ``0`` removes it.
            operation: Mock operation to delay: ``all`` or ``mqtt.publish``.

        Raises:
            ValueError: If the delay or operation is invalid.
        """

        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("delay must be a finite non-negative value")
        if operation not in {"all", "mqtt.publish"}:
            raise ValueError("mock delay operation must be 'all' or 'mqtt.publish'")
        if seconds == 0:
            self.scenario.delays.pop(operation, None)
        else:
            self.scenario.delays[operation] = seconds

    def drop_next_messages(self, count: int = 1) -> None:
        """Configure upcoming publications to be suppressed.

        Args:
            count: Non-negative number of upcoming publications to suppress.

        Raises:
            ValueError: If ``count`` is negative.
        """
        if count < 0:
            raise ValueError("drop count must not be negative")
        self.scenario.drop_messages_remaining = count

    def malform_next_messages(self, count: int = 1) -> None:
        """Configure upcoming publications to carry malformed payloads.

        Args:
            count: Non-negative number of upcoming publications to malform.

        Raises:
            ValueError: If ``count`` is negative.
        """
        if count < 0:
            raise ValueError("malformed-message count must not be negative")
        self.scenario.malformed_messages_remaining = count

    def set_authorization_failure(self, operation: str, *, enabled: bool = True) -> None:
        """Toggle a synthetic mock-only ACL denial.

        Args:
            operation: Mock-only ACL operation label: ``entity.read``,
                ``entity.write``, ``location.read``, ``location.write``,
                ``task.receive``, or ``task.send``.
            enabled: Whether the operation is forced to deny.

        Raises:
            ValueError: If ``operation`` is not a supported mock label.
        """

        if operation not in _ALL_OPERATIONS:
            raise ValueError("unknown mock ACL operation")
        if enabled:
            self.scenario.denied_operations.add(operation)
        else:
            self.scenario.denied_operations.discard(operation)

    def drop_next_connections(self, count: int = 1) -> None:
        """Close upcoming valid MQTT CONNECT attempts without sending CONNACK.

        Args:
            count: Number of upcoming connection attempts to drop.

        Raises:
            ValueError: If ``count`` is outside the supported fault bound.
        """

        self.scenario.connect_drops_remaining = _fault_count(count)

    def reject_next_authentications(self, count: int = 1) -> None:
        """Return a negative CONNACK for upcoming otherwise-valid credentials.

        Args:
            count: Number of upcoming authentication attempts to reject.

        Raises:
            ValueError: If ``count`` is outside the supported fault bound.
        """

        self.scenario.authentication_rejections_remaining = _fault_count(count)

    def deny_next_acknowledgements(self, acknowledgement: str, count: int = 1) -> None:
        """Return operation-specific authorization failures for upcoming ACKs.

        Args:
            acknowledgement: MQTT acknowledgement family to deny: ``suback``,
                ``puback``, or ``unsuback``.
            count: Number of upcoming acknowledgements to deny.

        Raises:
            ValueError: If the acknowledgement family or count is unsupported.
        """

        self.return_next_acknowledgement_reason(acknowledgement, 0x87, count)

    def return_next_acknowledgement_reason(
        self,
        acknowledgement: str,
        reason_code: int,
        count: int = 1,
    ) -> None:
        """Return one raw failure-reason byte in upcoming MQTT acknowledgements.

        Args:
            acknowledgement: MQTT acknowledgement family to alter: ``suback``,
                ``puback``, or ``unsuback``.
            reason_code: MQTT v5 failure-reason byte to return.
            count: Number of upcoming acknowledgements to alter.

        Raises:
            ValueError: If the family, reason code, or count is unsupported.
        """

        if acknowledgement not in _ACKNOWLEDGEMENTS:
            raise ValueError("acknowledgement must be suback, puback, or unsuback")
        if (
            isinstance(reason_code, bool)
            or not isinstance(reason_code, int)
            or not 0x80 <= reason_code <= 0xFF
        ):
            raise ValueError("reason_code must be an MQTT failure byte between 0x80 and 0xff")
        self._set_response_fault(acknowledgement, reason_code, count)

    def malform_next_protocol_responses(self, response: str, count: int = 1) -> None:
        """Send an invalid MQTT v5 property length in upcoming responses.

        Args:
            response: MQTT response family to malform: ``connack``, ``suback``,
                ``puback``, or ``unsuback``.
            count: Number of upcoming responses to malform.

        Raises:
            ValueError: If the response family or count is unsupported.
        """

        if response not in _PROTOCOL_RESPONSES:
            raise ValueError("response must be connack, suback, puback, or unsuback")
        self._set_response_fault(response, "malformed", count)

    def disconnect_next_publications(self, phase: str, count: int = 1) -> None:
        """Disconnect upcoming publications at one explicit QoS boundary.

        Args:
            phase: Publication-delivery phase at which to disconnect:
                ``qos0_before_completion``, ``before_puback``, or
                ``after_puback``.
            count: Number of upcoming publications to disconnect.

        Raises:
            ValueError: If the phase or count is unsupported.
        """

        if phase not in _PUBLISH_DISCONNECT_PHASES:
            raise ValueError("phase must be qos0_before_completion, before_puback, or after_puback")
        fault_count = _fault_count(count)
        safe_phase = cast("_PublishDisconnectPhase", phase)
        self.scenario.publication_disconnects[:] = [safe_phase] * fault_count

    async def disconnect_before_publish(self) -> None:
        """Disconnect live clients before the caller begins a publication."""

        await self.disconnect_clients()
        self.events.publication_disconnected_before_send.set()

    def reset_scenario(self) -> None:
        """Reset fault controls and synchronization events in place."""
        self.scenario.reset()
        self.events.reset()

    async def disconnect_clients(self) -> None:
        """Disconnect all live clients while leaving the listener available."""

        self.events.forced_disconnect.set()
        await self._broker.disconnect_clients()

    async def mqtt_authenticate(
        self,
        *,
        client_id: str,
        username: str | None,
        password: str | None,
    ) -> BrokerIdentity | None:
        if self._allow_unauthenticated and client_id and username is None and password is None:
            return BrokerIdentity(
                client_id=client_id,
                integration_name=_integration_name_from_client_id(client_id),
                token=FULL_ACCESS_TOKEN,
                acl_grants=_ALL_OPERATIONS,
            )
        integration_name = username or ""
        if not client_id or not integration_name or password not in self._tokens:
            self.events.authentication_failed.set()
            return None
        if self.scenario.authentication_rejections_remaining:
            self.scenario.authentication_rejections_remaining -= 1
            self.events.authentication_failed.set()
            return None
        assert password is not None
        return BrokerIdentity(
            client_id=client_id,
            integration_name=integration_name,
            token=password,
            acl_grants=self._tokens[password],
        )

    def mqtt_drop_connect(self) -> bool:
        if not self.scenario.connect_drops_remaining:
            return False
        self.scenario.connect_drops_remaining -= 1
        self.events.connection_dropped.set()
        return True

    def mqtt_acknowledgement_response(
        self,
        acknowledgement: _AcknowledgementKind,
    ) -> _AcknowledgementResponse | None:
        faults = self.scenario.response_faults.get(acknowledgement)
        if not faults:
            return None
        fault = faults.pop(0)
        if not faults:
            self.scenario.response_faults.pop(acknowledgement, None)
        if isinstance(fault, int):
            self.events.acknowledgement_reason_returned.set()
        if fault == 0x87:
            self.events.acknowledgement_denied.set()
        elif fault == "malformed":
            self.events.malformed_protocol_response.set()
        return fault

    def mqtt_can_subscribe(self, identity: BrokerIdentity, topic_filter: str) -> bool:
        operation = _subscription_operation(identity.integration_name, topic_filter)
        return self._mqtt_access_allowed(identity, operation)

    def mqtt_can_publish(self, identity: BrokerIdentity, topic: str) -> bool:
        operation = _publication_operation(identity.integration_name, topic)
        return self._mqtt_access_allowed(identity, operation)

    async def mqtt_published(
        self,
        identity: BrokerIdentity,
        topic: str,
        payload: bytes,
    ) -> PublishDecision:
        del identity
        self.events.message_received.set()
        await self._delay("mqtt.publish")
        self._retain_observed_state(topic, payload)

        if self.scenario.drop_messages_remaining:
            self.scenario.drop_messages_remaining -= 1
            self.events.message_dropped.set()
            return PublishDecision(payload=payload, forward=False)
        if self.scenario.malformed_messages_remaining:
            self.scenario.malformed_messages_remaining -= 1
            self.events.malformed_message_sent.set()
            return PublishDecision(payload=_MALFORMED_PAYLOAD)
        return PublishDecision(payload=payload)

    def mqtt_publication_disconnect(self) -> _PublishDisconnectPhase | None:
        if not self.scenario.publication_disconnects:
            return None
        if self.scenario.publication_disconnects[0] == "qos0_before_completion":
            return None
        return self.scenario.publication_disconnects.pop(0)

    def mqtt_qos0_publication_disconnect(self) -> bool:
        if (
            not self.scenario.publication_disconnects
            or self.scenario.publication_disconnects[0] != "qos0_before_completion"
        ):
            return False
        self.scenario.publication_disconnects.pop(0)
        return True

    async def mqtt_connected(self, identity: BrokerIdentity) -> None:
        del identity
        self.events.mqtt_connected.set()

    async def mqtt_disconnected(self, identity: BrokerIdentity) -> None:
        del identity
        self.events.mqtt_disconnected.set()

    async def mqtt_forwarded(self, topic: str, recipient_count: int) -> None:
        del topic, recipient_count
        self.events.message_forwarded.set()

    def mqtt_publication_disconnected(
        self,
        identity: BrokerIdentity,
        phase: _PublishDisconnectPhase,
    ) -> None:
        del identity
        if phase == "qos0_before_completion":
            self.events.publication_disconnected_qos0_before_completion.set()
        elif phase == "before_puback":
            self.events.publication_disconnected_before_puback.set()
        else:
            self.events.publication_disconnected_after_puback.set()

    def _mqtt_access_allowed(
        self,
        identity: BrokerIdentity,
        operation: str | None,
    ) -> bool:
        if (
            operation is None
            or operation in self.scenario.denied_operations
            or operation not in identity.acl_grants
        ):
            self.events.authorization_denied.set()
            return False
        return True

    def _retain_observed_state(self, topic: str, payload: bytes) -> None:
        try:
            parsed_topic = parse_protocol_topic(topic)
        except ECNClientError:
            return
        if isinstance(parsed_topic, EntityTopic):
            try:
                entity_event = decode_entity_payload(topic, payload, self._maximum_packet_size)
            except ECNClientError:
                return
            entity_id = str(entity_event.entity.id)
            self._entities[entity_id] = entity_event.entity.model_dump(mode="json")
            location = entity_event.location or entity_event.entity.position
            if location is not None:
                self._locations[entity_id] = location.model_dump(mode="json")
        elif isinstance(parsed_topic, LocationTopic):
            try:
                location_event = decode_location_payload(topic, payload, self._maximum_packet_size)
            except ECNClientError:
                return
            self._locations[str(location_event.entity_id)] = location_event.location.model_dump(
                mode="json"
            )

    async def _delay(self, operation: str) -> None:
        delay = self.scenario.delays.get(operation, self.scenario.delays.get("all", 0.0))
        if delay:
            await asyncio.sleep(delay)

    def _set_response_fault(
        self,
        response: str,
        fault: _AcknowledgementResponse,
        count: int,
    ) -> None:
        fault_count = _fault_count(count)
        if fault_count:
            self.scenario.response_faults[response] = [fault] * fault_count
        else:
            self.scenario.response_faults.pop(response, None)


def _fault_count(count: int) -> int:
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= _MAX_FAULT_COUNT:
        raise ValueError(f"fault count must be an integer between 0 and {_MAX_FAULT_COUNT}")
    return count


def _normalize_tokens(
    tokens: Mapping[str, Iterable[str]] | None,
) -> dict[str, frozenset[str]]:
    if tokens is None:
        return {
            FULL_ACCESS_TOKEN: _ALL_OPERATIONS,
            READ_ONLY_TOKEN: _READ_OPERATIONS,
            NO_ACCESS_TOKEN: frozenset(),
        }
    normalized: dict[str, frozenset[str]] = {}
    for token, operations in tokens.items():
        if not token:
            raise ValueError("mock token strings must not be empty")
        grants = frozenset(operations)
        if not grants <= _ALL_OPERATIONS:
            raise ValueError("mock token contains an unknown ACL operation")
        normalized[token] = grants
    return normalized


def _subscription_operation(integration_name: str, topic_filter: str) -> str | None:
    if "#" in topic_filter or any(
        "+" in level and level != "+" for level in topic_filter.split("/")
    ):
        return None
    parts = topic_filter.split("/")
    family = parts[0] if parts else ""
    if family == "task":
        if "+" in topic_filter:
            return None
        try:
            parsed = parse_protocol_topic(topic_filter)
        except ECNClientError:
            return None
        if not isinstance(parsed, TaskTopic):
            return None
        if parsed.response:
            return _TASK_SEND
        return _TASK_RECEIVE if parsed.integration == integration_name else None

    candidate = _concrete_filter_topic(parts)
    if candidate is None:
        return None
    try:
        parsed = parse_protocol_topic(candidate)
    except ECNClientError:
        return None
    if isinstance(parsed, EntityTopic):
        return _ENTITY_READ
    if isinstance(parsed, LocationTopic):
        return _LOCATION_READ
    return None


def _concrete_filter_topic(parts: list[str]) -> str | None:
    if not parts:
        return None
    family = parts[0]
    expected_levels = {
        "entity": 4,
        "entity_pb": 3,
        "entity_location": 3,
        "entity_location_pb": 3,
    }
    if len(parts) != expected_levels.get(family):
        return None
    concrete = list(parts)
    for index, level in enumerate(concrete):
        if level != "+":
            continue
        if index == 1:
            concrete[index] = "mock"
        elif (family == "entity" and index == 2) or (
            family.startswith("entity_location") and index == 2
        ):
            concrete[index] = _FILTER_UUID
        else:
            concrete[index] = "track"
    return "/".join(concrete)


def _publication_operation(integration_name: str, topic: str) -> str | None:
    try:
        parsed = parse_protocol_topic(topic)
    except ECNClientError:
        return None
    if isinstance(parsed, EntityTopic) and parsed.integration == integration_name:
        return _ENTITY_WRITE
    if isinstance(parsed, LocationTopic) and parsed.integration == integration_name:
        return _LOCATION_WRITE
    if isinstance(parsed, TaskTopic):
        if parsed.response:
            return _TASK_RECEIVE if parsed.integration == integration_name else None
        return _TASK_SEND
    return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the offline Picogrid MQTT mock")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument(
        "--allow-external-bind",
        action="store_true",
        help="development/CI only: allow --host to bind a non-loopback interface",
    )
    parser.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        help="development/CI only: accept MQTT CONNECT without credentials",
    )
    return parser


async def _serve(arguments: argparse.Namespace) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)

    async with MockECN(
        host=arguments.host,
        mqtt_port=arguments.mqtt_port,
        allow_external_bind=arguments.allow_external_bind,
        allow_unauthenticated=arguments.allow_unauthenticated,
    ) as mock:
        print(f"Mock ECN MQTT v5 listening on {mock.host}:{mock.mqtt_port}", flush=True)
        await stop.wait()


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        arguments.host = _loopback_host_argument(
            arguments.host,
            allow_external_bind=arguments.allow_external_bind,
        )
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))
    with suppress(KeyboardInterrupt):
        asyncio.run(_serve(arguments))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FULL_ACCESS_TOKEN",
    "NO_ACCESS_TOKEN",
    "READ_ONLY_TOKEN",
    "MockECN",
    "MockEvents",
    "MockScenario",
    "main",
]
