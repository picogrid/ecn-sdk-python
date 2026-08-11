# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Enums and common public value models."""

from __future__ import annotations

from enum import StrEnum


class Affiliation(StrEnum):
    """Classify an entity's relationship to the reporting party.

    ``FRIEND`` identifies friendly entities, ``HOSTILE`` identifies hostile entities,
    ``NEUTRAL`` identifies neutral entities, ``SUSPECT`` marks uncertain potentially
    hostile entities, and ``UNKNOWN`` represents an unspecified or unrecognized value.
    """

    FRIEND = "FRIEND"
    HOSTILE = "HOSTILE"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"
    SUSPECT = "SUSPECT"


class CheckStatus(StrEnum):
    """Report the outcome of one preflight check.

    ``PASS`` and ``FAIL`` report completed outcomes, ``SKIPPED`` marks a check that
    was not run, and ``UNKNOWN`` indicates that the outcome cannot be determined.
    """

    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class ClientState(StrEnum):
    """Describe the client's current lifecycle state.

    ``CREATED`` precedes startup; ``STARTING`` covers initialization; ``READY`` means
    the client is usable and MQTT is connected; ``RECONNECTING`` covers restoration
    of a lost connection; ``CLOSING`` and ``CLOSED`` describe shutdown; and ``FAILED``
    records an unrecoverable lifecycle failure.
    """

    CREATED = "created"
    STARTING = "starting"
    READY = "ready"
    RECONNECTING = "reconnecting"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


class ConnectionFailureCode(StrEnum):
    """Classify one secret-safe connection or recovery failure."""

    CONFIGURATION_INVALID = "configuration_invalid"
    CREDENTIALS_UNAVAILABLE = "credentials_unavailable"
    AUTHENTICATION_REJECTED = "authentication_rejected"
    DNS_UNAVAILABLE = "dns_unavailable"
    TCP_UNAVAILABLE = "tcp_unavailable"
    TLS_UNAVAILABLE = "tls_unavailable"
    TLS_PEER_VERIFICATION_FAILED = "tls_peer_verification_failed"
    BROKER_UNAVAILABLE = "broker_unavailable"
    SERVER_BUSY = "server_busy"
    CONNECTION_LOST = "connection_lost"
    CONNECTION_REJECTED = "connection_rejected"
    CONNECTION_AUTHORIZATION_DENIED = "connection_authorization_denied"
    CONNECTION_RESOURCE_LIMIT = "connection_resource_limit"
    SUBSCRIPTION_DENIED = "subscription_denied"
    SUBSCRIPTION_RESOURCE_LIMIT = "subscription_resource_limit"
    PROTOCOL_FAILURE = "protocol_failure"
    SERVER_REFERENCE_REQUIRES_REVIEW = "server_reference_requires_review"
    RETRY_EXHAUSTED = "retry_exhausted"


class ConnectionFailureOperation(StrEnum):
    """Identify the bounded connection phase associated with a failure."""

    CONFIGURE = "configure"
    RESOLVE_CREDENTIALS = "resolve_credentials"
    DNS = "dns"
    TCP = "tcp"
    TLS = "tls"
    CONNECT = "connect"
    RESTORE_SUBSCRIPTION = "restore_subscription"
    RECEIVE = "receive"


class ConnectionRetryState(StrEnum):
    """Describe whether and why the sole reconnect supervisor may run again."""

    INACTIVE = "inactive"
    CONNECTING = "connecting"
    SCHEDULED = "scheduled"
    WAITING_FOR_CREDENTIALS = "waiting_for_credentials"
    TERMINAL = "terminal"


class DeliveryPhase(StrEnum):
    """Record the strongest safe delivery fact established for one mutation."""

    NOT_SENT = "not_sent"
    LOCAL_SEND_COMPLETED = "local_send_completed"
    LOCAL_SEND_UNCERTAIN = "local_send_uncertain"
    BROKER_ACKNOWLEDGMENT_PENDING = "broker_acknowledgment_pending"
    BROKER_ACCEPTED = "broker_accepted"
    RESPONSE_PENDING = "response_pending"
    COMPLETED = "completed"


class DeliveryPolicy(StrEnum):
    """Select how a bounded event stream handles buffered events.

    ``FIFO`` preserves queued events in arrival order, while ``LATEST`` keeps the most
    recent event when the watcher buffer is full.
    """

    FIFO = "fifo"
    LATEST = "latest"


class EntityCategory(StrEnum):
    """Classify the domain represented by an entity.

    Members identify devices, detections, tracks, systems, sensors, alerts, geometric
    objects, or the ``OTHER`` fallback category.
    """

    DEVICE = "DEVICE"
    DETECTION = "DETECTION"
    TRACK = "TRACK"
    SYSTEM = "SYSTEM"
    SENSOR = "SENSOR"
    ALERT = "ALERT"
    GEOMETRIC = "GEOMETRIC"
    OTHER = "OTHER"


class EntityStatus(StrEnum):
    """Describe whether an entity is currently active.

    ``ACTIVE`` and ``INACTIVE`` express known states; ``UNKNOWN`` represents an
    unspecified or unrecognized inbound value.
    """

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    UNKNOWN = "UNKNOWN"


class PreflightCheckName(StrEnum):
    """Identify the operation evaluated by a preflight check.

    Members cover configuration, DNS, TCP, TLS, authentication, MQTT, subscription,
    and publish-authorization checks. Publish authorization may be unknown during a
    read-only preflight.
    """

    CONFIGURATION = "configuration"
    DNS = "dns"
    TCP = "tcp"
    TLS = "tls"
    AUTHENTICATION = "authentication"
    MQTT = "mqtt"
    SUBSCRIPTION = "subscription"
    PUBLISH_AUTHORIZATION = "publish_authorization"


class PublicationKind(StrEnum):
    """Identify the kind of locally completed publication.

    ``ENTITY`` represents an entity publication and ``LOCATION`` represents a
    geolocation publication.
    """

    ENTITY = "entity"
    LOCATION = "location"


class TaskMode(StrEnum):
    """Select the requested task completion behavior.

    ``COMPLETE`` waits for a result, ``ACKNOWLEDGMENT`` waits only for task
    acknowledgment, and ``FIRE_AND_FORGET`` returns after local MQTT completion.
    """

    COMPLETE = "complete"
    ACKNOWLEDGMENT = "acknowledgment"
    FIRE_AND_FORGET = "fire_and_forget"


class TaskStatus(StrEnum):
    """Describe a task result's reported state.

    ``SUCCESS``, ``FAILED``, and ``PENDING`` preserve known wire states. ``UNKNOWN``
    safely represents future inbound state strings.
    """

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"


class WireFormat(StrEnum):
    """Select the entity and location payload encoding.

    ``JSON`` uses JSON payloads and ``PROTOBUF`` uses protobuf payloads; both operate
    over MQTT v5.
    """

    JSON = "json"
    PROTOBUF = "protobuf"
