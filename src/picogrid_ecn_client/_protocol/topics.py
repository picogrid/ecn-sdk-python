# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Private builders and parsers for the supported public MQTT topic subset."""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from ..exceptions import ProtocolError, ValidationError
from ..models import EntityCategory

ENTITY_JSON_SUBSCRIPTION = "entity/+/+/+"
ENTITY_PROTOBUF_SUBSCRIPTION = "entity_pb/+/+"
LOCATION_JSON_SUBSCRIPTION = "entity_location/+/+"
LOCATION_PROTOBUF_SUBSCRIPTION = "entity_location_pb/+/+"

UNSUPPORTED_TOPIC_FAMILY = "unsupported"
"""Fixed classification used when a topic matches no supported family."""

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_INTEGRATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,126}[A-Za-z0-9]$")
_PROTOBUF_ENTITY_CATEGORIES = frozenset(
    {
        EntityCategory.DEVICE,
        EntityCategory.DETECTION,
        EntityCategory.TRACK,
        EntityCategory.SYSTEM,
        EntityCategory.SENSOR,
        EntityCategory.ALERT,
        EntityCategory.GEOMETRIC,
    }
)


class TopicFamily(StrEnum):
    """The complete MQTT topic-family allowlist supported by the SDK."""

    ENTITY_JSON = "entity"
    ENTITY_PROTOBUF = "entity_pb"
    LOCATION_JSON = "entity_location"
    LOCATION_PROTOBUF = "entity_location_pb"
    TASK = "task"


@dataclass(frozen=True, slots=True)
class EntityTopic:
    integration: str
    suffix: str
    entity_id: UUID | None
    protobuf: bool


@dataclass(frozen=True, slots=True)
class LocationTopic:
    integration: str
    entity_id: UUID
    protobuf: bool


@dataclass(frozen=True, slots=True)
class TaskTopic:
    integration: str
    entity_id: UUID
    command: str
    response: bool
    route_terminal_id: UUID | None = None


ParsedTopic = EntityTopic | LocationTopic | TaskTopic


def _validated_segment(value: str, *, label: str) -> str:
    if not isinstance(value, str) or _SEGMENT.fullmatch(value) is None:
        raise ValidationError(
            f"{label} must be a non-empty MQTT-safe identifier of at most 128 characters",
            operation="build_topic",
        )
    return value


def _validated_integration(value: str) -> str:
    if (
        not isinstance(value, str)
        or _INTEGRATION.fullmatch(value) is None
        or value.casefold() == "geolocation"
    ):
        raise ValidationError(
            "integration must match the pinned 2-128 character identifier grammar",
            operation="build_topic",
        )
    return value


def _category_suffix(category: EntityCategory | str) -> str:
    value = category.value if isinstance(category, EntityCategory) else category
    return _validated_segment(value.lower(), label="entity category")


def build_entity_topic(
    integration: str,
    entity_id: UUID,
    category: EntityCategory | str,
) -> str:
    """Build one exact JSON entity event topic."""

    return f"entity/{_validated_integration(integration)}/{entity_id}/{_category_suffix(category)}"


def build_entity_protobuf_topic(
    integration: str,
    category: EntityCategory,
) -> str:
    """Build one exact protobuf entity event topic (the ID is in the payload)."""

    if category not in _PROTOBUF_ENTITY_CATEGORIES:
        raise ValidationError(
            "entity category is not publishable on the supported protobuf wire",
            operation="build_topic",
        )
    return f"entity_pb/{_validated_integration(integration)}/{_category_suffix(category)}"


def build_entity_protobuf_decode_topic(
    integration: str,
    category: EntityCategory,
) -> str:
    """Build a private decode-only topic, including the ``OTHER`` fallback."""

    return f"entity_pb/{_validated_integration(integration)}/{_category_suffix(category)}"


def build_location_topic(integration: str, entity_id: UUID) -> str:
    """Build one exact JSON entity-location topic."""

    return f"entity_location/{_validated_integration(integration)}/{entity_id}"


def build_location_protobuf_topic(integration: str, entity_id: UUID) -> str:
    """Build one exact protobuf entity-location topic."""

    return f"entity_location_pb/{_validated_integration(integration)}/{entity_id}"


def build_entity_subscription_filters(
    categories: Collection[EntityCategory],
    integrations: Collection[str],
) -> tuple[str, ...]:
    """Build the narrowest fixed-depth entity filters represented by caller filters."""

    integration_segments = sorted({_validated_integration(value) for value in integrations}) or [
        "+"
    ]
    category_segments = (
        ["+"]
        if not categories or EntityCategory.OTHER in categories
        else sorted({_category_suffix(value) for value in categories})
    )
    filters = {
        f"entity/{integration}/+/{category}"
        for integration in integration_segments
        for category in category_segments
    }
    if not categories or EntityCategory.OTHER in categories:
        protobuf_segments = {"+"}
    else:
        protobuf_segments = {
            _category_suffix(value) for value in categories if value in _PROTOBUF_ENTITY_CATEGORIES
        }
    filters.update(
        f"entity_pb/{integration}/{category}"
        for integration in integration_segments
        for category in protobuf_segments
    )
    return tuple(sorted(filters))


def build_location_subscription_filters(
    entity_ids: Collection[UUID],
    integrations: Collection[str],
) -> tuple[str, ...]:
    """Build the narrowest fixed-depth location filters represented by caller filters."""

    integration_segments = sorted({_validated_integration(value) for value in integrations}) or [
        "+"
    ]
    entity_segments = sorted({str(value) for value in entity_ids}) or ["+"]
    return tuple(
        sorted(
            f"{family}/{integration}/{entity_id}"
            for family in ("entity_location", "entity_location_pb")
            for integration in integration_segments
            for entity_id in entity_segments
        )
    )


def build_task_request_topic(
    integration: str,
    entity_id: UUID,
    command: str,
    *,
    target_terminal_id: UUID | None = None,
) -> str:
    """Build one exact local or terminal-addressed task request topic."""

    local_topic = (
        f"task/{_validated_integration(integration)}/{entity_id}/"
        f"{_validated_segment(command, label='command')}"
    )
    return f"{target_terminal_id}/{local_topic}" if target_terminal_id is not None else local_topic


def build_task_response_topic(
    integration: str,
    entity_id: UUID,
    command: str,
    *,
    route_terminal_id: UUID | None = None,
) -> str:
    """Build one exact local or terminal-routed task response topic."""

    local_topic = f"{build_task_request_topic(integration, entity_id, command)}/response"
    return f"{route_terminal_id}/{local_topic}" if route_terminal_id is not None else local_topic


def _topic_family_label(topic: str) -> str:
    """Classify a topic as one fixed family label, never echoing untrusted text.

    Inbound topics are attacker-controlled wire input, so the returned value is
    always one of this module's own constants: a member of :class:`TopicFamily`
    or ``UNSUPPORTED_TOPIC_FAMILY``. Constants are chosen by exact match, so the
    result never carries text sliced from ``topic``.
    """

    try:
        return TopicFamily(topic.partition("/")[0]).value
    except ValueError:
        pass
    if _looks_like_routed_task(topic):
        return TopicFamily.TASK.value
    return UNSUPPORTED_TOPIC_FAMILY


def _protocol_error(topic: str) -> ProtocolError:
    return ProtocolError(
        "MQTT topic is outside the supported public protocol subset",
        operation="parse_topic",
        details={"topic_family": _topic_family_label(topic)},
    )


def _parse_uuid(value: str, topic: str) -> UUID:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError):
        raise _protocol_error(topic) from None
    if str(parsed) != value:
        raise _protocol_error(topic)
    return parsed


def _parse_segment(value: str, topic: str) -> str:
    if _SEGMENT.fullmatch(value) is None:
        raise _protocol_error(topic)
    return value


def _parse_integration(value: str, topic: str) -> str:
    if _INTEGRATION.fullmatch(value) is None or value.casefold() == "geolocation":
        raise _protocol_error(topic)
    return value


def parse_entity_topic(topic: str) -> EntityTopic:
    """Parse only the exact JSON/protobuf entity topic forms."""

    parts = topic.split("/")
    if len(parts) == 4 and parts[0] == TopicFamily.ENTITY_JSON:
        return EntityTopic(
            integration=_parse_integration(parts[1], topic),
            entity_id=_parse_uuid(parts[2], topic),
            suffix=_parse_segment(parts[3], topic),
            protobuf=False,
        )
    if len(parts) == 3 and parts[0] == TopicFamily.ENTITY_PROTOBUF:
        return EntityTopic(
            integration=_parse_integration(parts[1], topic),
            entity_id=None,
            suffix=_parse_segment(parts[2], topic),
            protobuf=True,
        )
    raise _protocol_error(topic)


def parse_location_topic(topic: str) -> LocationTopic:
    """Parse only the exact JSON/protobuf entity-location topic forms."""

    parts = topic.split("/")
    if len(parts) != 3 or parts[0] not in {
        TopicFamily.LOCATION_JSON,
        TopicFamily.LOCATION_PROTOBUF,
    }:
        raise _protocol_error(topic)
    return LocationTopic(
        integration=_parse_integration(parts[1], topic),
        entity_id=_parse_uuid(parts[2], topic),
        protobuf=parts[0] == TopicFamily.LOCATION_PROTOBUF,
    )


def parse_task_topic(topic: str) -> TaskTopic:
    """Parse an exact local or terminal-addressed task request/response topic."""

    parts = topic.split("/")
    route_terminal_id: UUID | None = None
    if parts[0:1] != [TopicFamily.TASK]:
        if len(parts) < 2 or parts[1] != TopicFamily.TASK:
            raise _protocol_error(topic)
        route_terminal_id = _parse_uuid(parts[0], topic)
        parts = parts[1:]
    is_request = len(parts) == 4
    is_response = len(parts) == 5 and parts[4] == "response"
    if parts[0:1] != [TopicFamily.TASK] or not (is_request or is_response):
        raise _protocol_error(topic)
    return TaskTopic(
        integration=_parse_integration(parts[1], topic),
        entity_id=_parse_uuid(parts[2], topic),
        command=_parse_segment(parts[3], topic),
        response=is_response,
        route_terminal_id=route_terminal_id,
    )


def parse_protocol_topic(topic: str) -> ParsedTopic:
    """Parse an exact topic from any supported family, rejecting all others."""

    family = topic.partition("/")[0]
    if family in {TopicFamily.ENTITY_JSON, TopicFamily.ENTITY_PROTOBUF}:
        return parse_entity_topic(topic)
    if family in {TopicFamily.LOCATION_JSON, TopicFamily.LOCATION_PROTOBUF}:
        return parse_location_topic(topic)
    if family == TopicFamily.TASK or _looks_like_routed_task(topic):
        return parse_task_topic(topic)
    raise _protocol_error(topic)


def validate_publish_topic(topic: str) -> str:
    """Return an exact allowed publish topic or raise a public validation error."""

    try:
        parse_protocol_topic(topic)
    except ProtocolError:
        raise ValidationError(
            "publish topic is outside the supported public protocol subset",
            operation="mqtt.publish",
        ) from None
    return topic


def validate_subscription_filter(topic_filter: str) -> str:
    """Allow fixed-depth event filters or one exact task path; reject ``#``."""

    parts = topic_filter.split("/")
    family = parts[0] if parts else ""
    event_shape = (
        (
            family == TopicFamily.ENTITY_JSON
            and len(parts) == 4
            and _valid_integration_filter_segment(parts[1])
            and _valid_uuid_filter_segment(parts[2])
            and _valid_filter_segment(parts[3])
        )
        or (
            family == TopicFamily.ENTITY_PROTOBUF
            and len(parts) == 3
            and _valid_integration_filter_segment(parts[1])
            and _valid_filter_segment(parts[2])
        )
        or (
            family in {TopicFamily.LOCATION_JSON, TopicFamily.LOCATION_PROTOBUF}
            and len(parts) == 3
            and _valid_integration_filter_segment(parts[1])
            and _valid_uuid_filter_segment(parts[2])
        )
    )
    if event_shape:
        return topic_filter
    try:
        parsed_task = parse_task_topic(topic_filter)
    except ProtocolError:
        raise ValidationError(
            "subscription is outside the supported public protocol subset",
            operation="mqtt.subscribe",
        ) from None
    if parsed_task.route_terminal_id is not None:
        raise ValidationError(
            "terminal-prefixed task subscriptions are handled by ECN routing infrastructure",
            operation="mqtt.subscribe",
        )
    return topic_filter


def _looks_like_routed_task(topic: str) -> bool:
    parts = topic.split("/", 2)
    if len(parts) < 2 or parts[1] != TopicFamily.TASK:
        return False
    try:
        parsed = UUID(parts[0])
    except (TypeError, ValueError):
        return False
    return str(parsed) == parts[0]


def _valid_filter_segment(value: str) -> bool:
    return value == "+" or _SEGMENT.fullmatch(value) is not None


def _valid_integration_filter_segment(value: str) -> bool:
    return value == "+" or (
        _INTEGRATION.fullmatch(value) is not None and value.casefold() != "geolocation"
    )


def _valid_uuid_filter_segment(value: str) -> bool:
    if value == "+":
        return True
    try:
        parsed = UUID(value)
    except (TypeError, ValueError):
        return False
    return str(parsed) == value


__all__ = [
    "ENTITY_JSON_SUBSCRIPTION",
    "ENTITY_PROTOBUF_SUBSCRIPTION",
    "LOCATION_JSON_SUBSCRIPTION",
    "LOCATION_PROTOBUF_SUBSCRIPTION",
    "UNSUPPORTED_TOPIC_FAMILY",
    "EntityTopic",
    "LocationTopic",
    "ParsedTopic",
    "TaskTopic",
    "TopicFamily",
    "build_entity_protobuf_topic",
    "build_entity_subscription_filters",
    "build_entity_topic",
    "build_location_protobuf_topic",
    "build_location_subscription_filters",
    "build_location_topic",
    "build_task_request_topic",
    "build_task_response_topic",
    "parse_entity_topic",
    "parse_location_topic",
    "parse_protocol_topic",
    "parse_task_topic",
    "validate_publish_topic",
    "validate_subscription_filter",
]
