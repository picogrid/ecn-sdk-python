# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Typed JSON and protobuf conversion for the supported public wire messages."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from google.protobuf.message import DecodeError, Message
from pydantic import ValidationError as PydanticValidationError

from ..exceptions import ProtocolError, ResourceLimitError, ValidationError
from ..models import (
    Affiliation,
    AngularVelocity,
    DisplayMetadata,
    Entity,
    EntityCategory,
    EntityEvent,
    EntityMetadata,
    EntityStatus,
    Location,
    LocationEvent,
    Velocity,
    WireFormat,
)
from ..models._base import utc_datetime
from .generated import common_pb2 as _common_pb2
from .generated import entity_pb2 as _entity_pb2
from .json_codec import decode_json, encode_json
from .topics import EntityTopic, LocationTopic, parse_entity_topic, parse_location_topic

# Protoc creates these attributes dynamically through the protobuf builder.
common_pb2: Any = _common_pb2
entity_pb2: Any = _entity_pb2

_CATEGORY_TO_PROTO = {
    EntityCategory.DEVICE: common_pb2.ENTITY_CATEGORY_DEVICE,
    EntityCategory.DETECTION: common_pb2.ENTITY_CATEGORY_DETECTION,
    EntityCategory.TRACK: common_pb2.ENTITY_CATEGORY_TRACK,
    EntityCategory.SYSTEM: common_pb2.ENTITY_CATEGORY_SYSTEM,
    EntityCategory.SENSOR: common_pb2.ENTITY_CATEGORY_SENSOR,
    EntityCategory.ALERT: common_pb2.ENTITY_CATEGORY_ALERT,
}
_CATEGORY_FROM_PROTO = {value: key for key, value in _CATEGORY_TO_PROTO.items()}
_STATUS_TO_PROTO = {
    EntityStatus.ACTIVE: common_pb2.ENTITY_STATUS_ACTIVE,
    EntityStatus.INACTIVE: common_pb2.ENTITY_STATUS_INACTIVE,
    EntityStatus.UNKNOWN: common_pb2.ENTITY_STATUS_UNKNOWN,
}
_STATUS_FROM_PROTO = {value: key for key, value in _STATUS_TO_PROTO.items()}
_AFFILIATION_TO_PROTO = {
    Affiliation.FRIEND: common_pb2.AFFILIATION_FRIEND,
    Affiliation.HOSTILE: common_pb2.AFFILIATION_HOSTILE,
    Affiliation.NEUTRAL: common_pb2.AFFILIATION_NEUTRAL,
    Affiliation.UNKNOWN: common_pb2.AFFILIATION_UNKNOWN,
    Affiliation.SUSPECT: common_pb2.AFFILIATION_SUSPECT,
}
_AFFILIATION_FROM_PROTO = {value: key for key, value in _AFFILIATION_TO_PROTO.items()}
_MAX_PROTOBUF_GROUP_DEPTH = 64


def _check_payload_size(payload: bytes, max_size: int, *, operation: str) -> None:
    if len(payload) > max_size:
        raise ResourceLimitError(
            "protocol payload exceeds maximum_payload_size",
            operation=operation,
            details={"payload_size": len(payload), "maximum_payload_size": max_size},
        )


def _serialize(message: Message, max_size: int, *, operation: str) -> bytes:
    payload = message.SerializeToString(deterministic=True)
    _check_payload_size(payload, max_size, operation=operation)
    return payload


def _timestamp_to_datetime(timestamp: Any) -> Any:
    try:
        return timestamp.ToDatetime(tzinfo=UTC)
    except (OverflowError, ValueError):
        raise ProtocolError("protobuf timestamp is invalid", operation="decode_protobuf") from None


def _location_to_message(location: Location) -> Any:
    message = common_pb2.LocationMessage(
        latitude=location.latitude,
        longitude=location.longitude,
    )
    message.recorded_at.FromDatetime(location.recorded_at)
    if location.altitude is not None:
        message.altitude = location.altitude
    if location.bearing is not None:
        message.bearing = location.bearing
    if location.accuracy is not None:
        message.accuracy = location.accuracy
    if location.source is not None:
        message.source = location.source
    if location.velocity is not None:
        message.velocity.extend(
            [location.velocity.north, location.velocity.east, location.velocity.down]
        )
    if location.angular_velocity is not None:
        message.angular_velocity.extend(
            [
                location.angular_velocity.roll,
                location.angular_velocity.pitch,
                location.angular_velocity.yaw,
            ]
        )
    if location.confidence is not None:
        message.confidence = location.confidence
    return message


def _location_from_message(
    message: Any,
    *,
    fallback_recorded_at: datetime | None = None,
) -> Location:
    if message.HasField("recorded_at"):
        recorded_at = _timestamp_to_datetime(message.recorded_at)
    elif fallback_recorded_at is not None:
        # The pinned entity-event decoder uses its envelope timestamp when an
        # embedded location omits its own timestamp.
        recorded_at = fallback_recorded_at
    else:
        raise ProtocolError(
            "protobuf location is missing recorded_at",
            operation="decode_protobuf",
        )
    velocity: Velocity | None = None
    if message.velocity:
        if len(message.velocity) != 3:
            raise ProtocolError(
                "protobuf velocity must contain exactly three values",
                operation="decode_protobuf",
            )
        velocity = Velocity(
            north=message.velocity[0],
            east=message.velocity[1],
            down=message.velocity[2],
        )
    angular_velocity: AngularVelocity | None = None
    if message.angular_velocity:
        if len(message.angular_velocity) != 3:
            raise ProtocolError(
                "protobuf angular_velocity must contain exactly three values",
                operation="decode_protobuf",
            )
        angular_velocity = AngularVelocity(
            roll=message.angular_velocity[0],
            pitch=message.angular_velocity[1],
            yaw=message.angular_velocity[2],
        )
    try:
        return Location(
            latitude=message.latitude,
            longitude=message.longitude,
            recorded_at=recorded_at,
            altitude=message.altitude if message.HasField("altitude") else None,
            bearing=message.bearing if message.HasField("bearing") else None,
            accuracy=message.accuracy if message.HasField("accuracy") else None,
            source=message.source if message.HasField("source") else None,
            velocity=velocity,
            angular_velocity=angular_velocity,
            confidence=message.confidence if message.HasField("confidence") else None,
        )
    except PydanticValidationError:
        raise ValidationError(
            "protobuf location contains invalid public fields",
            operation="decode_protobuf",
        ) from None


def _topic_category(topic: EntityTopic) -> EntityCategory:
    try:
        return EntityCategory(topic.suffix.upper())
    except ValueError:
        # Category suffixes are an extensible broker-side taxonomy. Preserve
        # forward compatibility in the typed model without exposing a raw enum.
        return EntityCategory.OTHER


def _validate_entity_topic(entity: Entity, topic: EntityTopic) -> None:
    if entity.integration != topic.integration:
        raise ProtocolError(
            "entity integration does not match its MQTT topic",
            operation="decode_entity",
        )
    if topic.entity_id is not None and entity.id != topic.entity_id:
        raise ProtocolError("entity ID does not match its MQTT topic", operation="decode_entity")
    if entity.category != _topic_category(topic):
        raise ProtocolError(
            "entity category does not match its MQTT topic",
            operation="decode_entity",
        )


def _metadata_to_wire(metadata: EntityMetadata) -> dict[str, Any]:
    result = dict(metadata.properties)
    if metadata.display is not None:
        result["display"] = metadata.display.model_dump(mode="json", exclude_none=True)
    return result


def _metadata_from_wire(value: object) -> EntityMetadata:
    if value is None:
        return EntityMetadata()
    if not isinstance(value, dict):
        raise ValidationError("entity metadata must be a JSON object", operation="decode_entity")
    try:
        properties = dict(value)
        raw_display = properties.pop("display", None)
        display: DisplayMetadata | None = None
        if isinstance(raw_display, dict):
            display = DisplayMetadata.model_validate(
                {
                    key: item
                    for key, item in raw_display.items()
                    if key in DisplayMetadata.model_fields
                }
            )
        elif raw_display is not None:
            properties["display"] = raw_display
        return EntityMetadata(display=display, properties=properties)
    except PydanticValidationError:
        raise ValidationError(
            "entity metadata does not match the public metadata schema",
            operation="decode_entity",
        ) from None


def _location_to_wire_json(location: Location) -> dict[str, Any]:
    wire = location.model_dump(
        mode="json",
        exclude={"velocity", "angular_velocity"},
        exclude_none=True,
    )
    if location.velocity is not None:
        wire["velocity"] = [
            location.velocity.north,
            location.velocity.east,
            location.velocity.down,
        ]
    if location.angular_velocity is not None:
        wire["angular_velocity"] = [
            location.angular_velocity.roll,
            location.angular_velocity.pitch,
            location.angular_velocity.yaw,
        ]
    return wire


def _three_vector(value: object, *, label: str) -> tuple[float, float, float] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 3:
        raise ValidationError(
            f"wire {label} must contain exactly three values",
            operation="decode_location",
        )
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        raise ValidationError(
            f"wire {label} values must be numbers",
            operation="decode_location",
        )
    return (float(value[0]), float(value[1]), float(value[2]))


def _wire_recorded_at(value: object, *, operation: str) -> datetime:
    if not isinstance(value, str) or not value or "T" not in value:
        raise ValidationError(
            "wire recorded_at must be a non-empty ISO-8601 string",
            operation=operation,
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValidationError(
            "wire recorded_at must be a valid ISO-8601 timestamp",
            operation=operation,
        ) from None
    try:
        return utc_datetime(parsed)
    except ValueError:
        raise ValidationError(
            "wire recorded_at must include a timezone",
            operation=operation,
        ) from None


def _location_from_wire_json(value: object) -> Location:
    if not isinstance(value, dict):
        raise ValidationError("location must be a JSON object", operation="decode_location")
    data = {key: item for key, item in value.items() if key in Location.model_fields}
    for field in ("latitude", "longitude"):
        if field not in data:
            raise ValidationError(
                f"wire location is missing required {field}",
                operation="decode_location",
            )
    for field in ("latitude", "longitude", "altitude", "bearing", "accuracy", "confidence"):
        item = data.get(field)
        if item is not None and (isinstance(item, bool) or not isinstance(item, (int, float))):
            raise ValidationError(
                f"wire location {field} must be a number",
                operation="decode_location",
            )
    data["recorded_at"] = _wire_recorded_at(
        data.get("recorded_at"),
        operation="decode_location",
    )
    velocity_value = data.get("velocity")
    if velocity_value is not None:
        velocity = _three_vector(data.pop("velocity"), label="velocity")
        assert velocity is not None
        data["velocity"] = Velocity(north=velocity[0], east=velocity[1], down=velocity[2])
    angular_value = data.get("angular_velocity")
    if angular_value is not None:
        angular = _three_vector(data.pop("angular_velocity"), label="angular_velocity")
        assert angular is not None
        data["angular_velocity"] = AngularVelocity(
            roll=angular[0],
            pitch=angular[1],
            yaw=angular[2],
        )
    try:
        return Location.model_validate(data)
    except PydanticValidationError:
        raise ValidationError(
            "JSON payload does not match the public location schema",
            operation="decode_location",
        ) from None


def _entity_to_wire_json(entity: Entity, location: Location | None) -> dict[str, Any]:
    wire: dict[str, Any] = {
        "id": str(entity.id),
        "integration_name": entity.integration,
        "recorded_at": entity.recorded_at.isoformat().replace("+00:00", "Z"),
        "type_": entity.type,
        "category": entity.category.value,
        "status": entity.status.value.lower(),
        "affiliation": entity.affiliation.value,
        "metadata": _metadata_to_wire(entity.metadata),
    }
    if entity.fingerprint is not None:
        wire["fingerprint"] = entity.fingerprint
    if entity.name is not None:
        wire["name"] = entity.name
    if entity.parent_id is not None:
        wire["parent_id"] = str(entity.parent_id)
    if location is not None:
        wire["location"] = _location_to_wire_json(location)
    return wire


def _entity_publication_parts(
    value: Entity | EntityEvent,
    *,
    operation: str,
) -> tuple[Entity, Location | None, datetime]:
    if isinstance(value, EntityEvent):
        entity = value.entity
        if value.timestamp != entity.recorded_at:
            raise ValidationError(
                "entity event timestamp must match entity recorded_at",
                operation=operation,
            )
        if (
            value.location is not None
            and entity.position is not None
            and value.location != entity.position
        ):
            raise ValidationError(
                "entity event location must match entity position when both are present",
                operation=operation,
            )
        location = value.location or entity.position
        timestamp = value.timestamp
    else:
        entity = value
        location = value.position
        timestamp = value.recorded_at
    if location is not None and location.recorded_at != timestamp:
        raise ValidationError(
            "entity location recorded_at must match the entity event timestamp",
            operation=operation,
        )
    return entity, location, timestamp


def _entity_from_wire_json(value: dict[str, Any], topic: EntityTopic) -> Entity:
    required = {"id", "integration_name", "recorded_at", "category"}
    missing = sorted(required.difference(value))
    if missing:
        raise ValidationError(
            "JSON entity is missing required wire fields",
            operation="decode_entity",
            details={"fields": missing},
        )
    raw_id = value["id"]
    try:
        parsed_id = UUID(raw_id) if isinstance(raw_id, str) else None
    except ValueError:
        raise ValidationError(
            "JSON entity ID must be a canonical UUID",
            operation="decode_entity",
        ) from None
    if parsed_id is None or str(parsed_id) != raw_id:
        raise ValidationError(
            "JSON entity ID must be a canonical UUID",
            operation="decode_entity",
        )
    raw_integration = value["integration_name"]
    if not isinstance(raw_integration, str) or raw_integration != topic.integration:
        raise ProtocolError(
            "entity integration does not match its MQTT topic",
            operation="decode_entity",
        )
    recorded_at = _wire_recorded_at(
        value["recorded_at"],
        operation="decode_entity",
    )
    raw_category = value["category"]
    if not isinstance(raw_category, str) or not raw_category or len(raw_category) > 128:
        raise ValidationError(
            "JSON entity category must be a bounded non-empty string",
            operation="decode_entity",
        )
    if raw_category.casefold() != topic.suffix.casefold():
        raise ProtocolError(
            "entity category does not match its MQTT topic",
            operation="decode_entity",
        )
    data = dict(value)
    location_value = data.pop("location", None)
    metadata_value = data.pop("metadata", None)
    # This pinned SDK field is deliberately outside the public entity model.
    data.pop("organization_id", None)
    data["integration"] = data.pop("integration_name")
    data["recorded_at"] = recorded_at
    type_value = data.pop("type_", None)
    data["type"] = "UNKNOWN" if type_value is None else type_value
    data["metadata"] = _metadata_from_wire(metadata_value)
    try:
        data["position"] = (
            _location_from_wire_json(location_value) if location_value is not None else None
        )
    except (ValidationError, PydanticValidationError):
        # The pinned consumers deliver an otherwise valid entity when its
        # optional embedded location is malformed. Dedicated location events
        # remain strict and reject the same invalid location.
        data["position"] = None
    data["category"] = _json_entity_category(raw_category)
    data["status"] = _json_entity_status(data.get("status"))
    data["affiliation"] = _json_affiliation(data.get("affiliation"))
    data = {key: item for key, item in data.items() if key in Entity.model_fields}
    try:
        return Entity.model_validate(data)
    except PydanticValidationError:
        raise ValidationError(
            "JSON payload does not match the public entity schema",
            operation="decode_entity",
        ) from None


def _json_entity_category(value: object) -> EntityCategory:
    if isinstance(value, str):
        try:
            return EntityCategory(value.upper())
        except ValueError:
            pass
    return EntityCategory.OTHER


def _json_entity_status(value: object) -> EntityStatus:
    if value is None:
        return EntityStatus.UNKNOWN
    if isinstance(value, str):
        try:
            return EntityStatus(value.upper())
        except ValueError:
            return EntityStatus.UNKNOWN
    raise ValidationError(
        "JSON entity status must be a string or null",
        operation="decode_entity",
    )


def _json_affiliation(value: object) -> Affiliation:
    if value is None:
        return Affiliation.UNKNOWN
    if isinstance(value, str):
        try:
            return Affiliation(value.upper())
        except ValueError:
            return Affiliation.UNKNOWN
    raise ValidationError(
        "JSON entity affiliation must be a string or null",
        operation="decode_entity",
    )


def encode_entity_json(entity: Entity | EntityEvent, max_size: int) -> bytes:
    """Encode one entity in the demonstrated MQTT JSON compatibility shape."""

    public_entity, location, _timestamp = _entity_publication_parts(
        entity,
        operation="encode_entity",
    )
    if public_entity.category is EntityCategory.OTHER:
        raise ValidationError(
            "OTHER is an inbound fallback and cannot be published as an entity category",
            operation="encode_entity",
        )
    return encode_json(_entity_to_wire_json(public_entity, location), max_size)


def decode_entity_json(topic: str, payload: bytes, max_size: int) -> EntityEvent:
    """Decode one flat entity JSON object from the confirmed topic family."""

    parsed_topic = parse_entity_topic(topic)
    if parsed_topic.protobuf:
        raise ProtocolError("protobuf entity topic requires protobuf decoding")
    data = decode_json(payload, max_size)
    # The pinned MQTT adapter may inject this reserved tracing carrier beside
    # the entity fields. It is transport context, not public entity metadata.
    data.pop("_otel", None)
    try:
        entity = _entity_from_wire_json(data, parsed_topic)
        event = EntityEvent(
            timestamp=entity.recorded_at,
            entity=entity,
            location=entity.position,
        )
    except PydanticValidationError:
        raise ValidationError(
            "JSON payload does not match the public entity schema",
            operation="decode_entity",
        ) from None
    _validate_entity_topic(event.entity, parsed_topic)
    return event


def _read_wire_varint(payload: memoryview, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(payload):
            raise ProtocolError(
                "truncated entity protobuf payload",
                operation="decode_protobuf",
            )
        byte = payload[offset]
        offset += 1
        if shift == 63 and byte > 1:
            break
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
    raise ProtocolError("invalid entity protobuf varint", operation="decode_protobuf")


def _skip_wire_group(
    payload: memoryview,
    offset: int,
    field_number: int,
    *,
    depth: int,
) -> int:
    while offset < len(payload):
        tag, offset = _read_wire_varint(payload, offset)
        nested_field = tag >> 3
        wire_type = tag & 0x07
        if nested_field == 0:
            raise ProtocolError(
                "invalid entity protobuf field number",
                operation="decode_protobuf",
            )
        if wire_type == 4:
            if nested_field != field_number:
                raise ProtocolError(
                    "mismatched entity protobuf group",
                    operation="decode_protobuf",
                )
            return offset
        offset = _skip_wire_value(payload, offset, nested_field, wire_type, depth=depth)
    raise ProtocolError("truncated entity protobuf group", operation="decode_protobuf")


def _skip_wire_value(
    payload: memoryview,
    offset: int,
    field_number: int,
    wire_type: int,
    *,
    depth: int = 0,
) -> int:
    if wire_type == 0:
        return _read_wire_varint(payload, offset)[1]
    if wire_type == 1:
        end = offset + 8
    elif wire_type == 2:
        length, offset = _read_wire_varint(payload, offset)
        end = offset + length
    elif wire_type == 3:
        if depth >= _MAX_PROTOBUF_GROUP_DEPTH:
            raise ProtocolError(
                "entity protobuf group nesting exceeds the supported limit",
                operation="decode_protobuf",
            )
        return _skip_wire_group(payload, offset, field_number, depth=depth + 1)
    elif wire_type == 5:
        end = offset + 4
    else:
        raise ProtocolError(
            "invalid entity protobuf wire type",
            operation="decode_protobuf",
        )
    if end > len(payload):
        raise ProtocolError("truncated entity protobuf payload", operation="decode_protobuf")
    return end


def _iter_wire_fields(payload: memoryview) -> Iterator[tuple[int, int, int, int]]:
    offset = 0
    while offset < len(payload):
        tag, offset = _read_wire_varint(payload, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if field_number == 0 or wire_type == 4:
            raise ProtocolError(
                "invalid entity protobuf field",
                operation="decode_protobuf",
            )
        value_offset = offset
        offset = _skip_wire_value(payload, offset, field_number, wire_type)
        yield field_number, wire_type, value_offset, offset


def _length_delimited_value(
    payload: memoryview,
    value_offset: int,
    field_end: int,
) -> memoryview:
    length, start = _read_wire_varint(payload, value_offset)
    if start + length != field_end:
        raise ProtocolError("invalid entity protobuf length", operation="decode_protobuf")
    return payload[start:field_end]


def extract_entity_identity(
    topic: EntityTopic | str,
    payload: bytes,
    max_size: int,
) -> tuple[str, UUID]:
    """Extract a strict entity identity without constructing the full event model."""

    _check_payload_size(payload, max_size, operation="decode_entity")
    parsed_topic = parse_entity_topic(topic) if isinstance(topic, str) else topic
    topic_category = _topic_category(parsed_topic)
    if parsed_topic.entity_id is not None:
        return parsed_topic.integration, parsed_topic.entity_id

    entity_id: bytes | None = None
    category: int | None = None
    raw_payload = memoryview(payload)
    for field_number, wire_type, value_offset, field_end in _iter_wire_fields(raw_payload):
        if field_number != 2 or wire_type != 2:
            continue
        entity_payload = _length_delimited_value(raw_payload, value_offset, field_end)
        for nested_number, nested_type, nested_offset, nested_end in _iter_wire_fields(
            entity_payload
        ):
            if nested_number == 1 and nested_type == 2:
                entity_id = bytes(
                    _length_delimited_value(entity_payload, nested_offset, nested_end)
                )
            elif nested_number == 5 and nested_type == 0:
                category = _read_wire_varint(entity_payload, nested_offset)[0]
    if entity_id is None or len(entity_id) != 16:
        raise ProtocolError("entity protobuf ID must contain 16 bytes", operation="decode_protobuf")
    decoded_category = (
        _CATEGORY_FROM_PROTO.get(category, EntityCategory.OTHER)
        if category is not None
        else topic_category
    )
    if decoded_category != topic_category:
        raise ProtocolError(
            "protobuf entity category does not match its MQTT topic",
            operation="decode_entity",
        )
    return parsed_topic.integration, UUID(bytes=entity_id)


_extract_entity_identity = extract_entity_identity


def encode_location_json(location: Location, max_size: int) -> bytes:
    """Encode one wrapped location in the demonstrated MQTT JSON shape."""

    return encode_json({"location": _location_to_wire_json(location)}, max_size)


def decode_location_json(topic: str, payload: bytes, max_size: int) -> LocationEvent:
    """Decode one confirmed wrapped location object using identity from its topic."""

    parsed_topic = parse_location_topic(topic)
    if parsed_topic.protobuf:
        raise ProtocolError("protobuf location topic requires protobuf decoding")
    data = decode_json(payload, max_size)
    if "location" not in data:
        raise ValidationError(
            "JSON location envelope is missing the required location field",
            operation="decode_location",
        )
    location = _location_from_wire_json(data["location"])
    return LocationEvent(
        entity_id=parsed_topic.entity_id,
        integration=parsed_topic.integration,
        timestamp=location.recorded_at,
        location=location,
    )


def encode_entity_protobuf(entity: Entity | EntityEvent, max_size: int) -> bytes:
    """Encode only the independently generated public EntityEventMessage subset."""

    public_entity, location, timestamp = _entity_publication_parts(
        entity,
        operation="encode_protobuf",
    )
    if public_entity.fingerprint is not None:
        raise ValidationError(
            "entity fingerprint is not represented by the supported protobuf wire",
            operation="encode_protobuf",
        )
    if public_entity.category is EntityCategory.OTHER:
        raise ValidationError(
            "OTHER is an inbound fallback and cannot be published as an entity category",
            operation="encode_protobuf",
        )
    message = entity_pb2.EntityEventMessage()
    message.recorded_at.FromDatetime(timestamp)
    message.entity.id = public_entity.id.bytes
    message.entity.type_name = public_entity.type
    message.entity.status = _STATUS_TO_PROTO[public_entity.status]
    message.entity.affiliation = _AFFILIATION_TO_PROTO[public_entity.affiliation]
    if public_entity.name is not None:
        message.entity.name = public_entity.name
    if public_entity.parent_id is not None:
        message.entity.parent_id = public_entity.parent_id.bytes
    message.entity.metadata_json = encode_json(_metadata_to_wire(public_entity.metadata), max_size)
    if location is not None:
        message.location.CopyFrom(_location_to_message(location))
    return _serialize(message, max_size, operation="encode_protobuf")


def _decode_metadata(payload: bytes, max_size: int) -> EntityMetadata:
    if not payload:
        return EntityMetadata()
    raw = decode_json(payload, max_size)
    return _metadata_from_wire(raw)


def decode_entity_protobuf(topic: str, payload: bytes, max_size: int) -> EntityEvent:
    """Decode only the committed public EntityEventMessage protobuf subset."""

    parsed_topic = parse_entity_topic(topic)
    if not parsed_topic.protobuf:
        raise ProtocolError("JSON entity topic requires JSON decoding")
    _check_payload_size(payload, max_size, operation="decode_protobuf")
    message = entity_pb2.EntityEventMessage()
    try:
        message.ParseFromString(payload)
    except DecodeError:
        raise ProtocolError(
            "malformed entity protobuf payload", operation="decode_protobuf"
        ) from None
    if not message.HasField("recorded_at") or not message.HasField("entity"):
        raise ProtocolError(
            "entity protobuf is missing required public fields",
            operation="decode_protobuf",
        )
    if len(message.entity.id) != 16:
        raise ProtocolError("entity protobuf ID must contain 16 bytes", operation="decode_protobuf")
    topic_category = _topic_category(parsed_topic)
    category = topic_category
    if message.entity.HasField("category"):
        decoded_category = _CATEGORY_FROM_PROTO.get(message.entity.category)
        category = decoded_category if decoded_category is not None else EntityCategory.OTHER
    if category != topic_category:
        raise ProtocolError(
            "protobuf entity category does not match its MQTT topic",
            operation="decode_entity",
        )
    recorded_at = _timestamp_to_datetime(message.recorded_at)
    try:
        location = (
            _location_from_message(message.location, fallback_recorded_at=recorded_at)
            if message.HasField("location")
            else None
        )
    except (ProtocolError, ValidationError, PydanticValidationError):
        # Match the pinned fast decoder's partial-delivery behavior for the
        # optional embedded location without weakening standalone decoding.
        location = None
    parent_id: UUID | None = None
    if message.entity.HasField("parent_id"):
        if len(message.entity.parent_id) != 16:
            raise ProtocolError(
                "protobuf parent entity ID must contain 16 bytes",
                operation="decode_protobuf",
            )
        parent_id = UUID(bytes=message.entity.parent_id)
    status = EntityStatus.UNKNOWN
    if message.entity.HasField("status"):
        decoded_status = _STATUS_FROM_PROTO.get(message.entity.status)
        status = decoded_status if decoded_status is not None else EntityStatus.UNKNOWN
    affiliation = Affiliation.UNKNOWN
    if message.entity.HasField("affiliation"):
        decoded_affiliation = _AFFILIATION_FROM_PROTO.get(message.entity.affiliation)
        affiliation = (
            decoded_affiliation if decoded_affiliation is not None else Affiliation.UNKNOWN
        )
    try:
        entity = Entity(
            id=UUID(bytes=message.entity.id),
            category=category,
            integration=parsed_topic.integration,
            recorded_at=recorded_at,
            type=message.entity.type_name if message.entity.HasField("type_name") else "UNKNOWN",
            name=message.entity.name if message.entity.HasField("name") else None,
            status=status,
            affiliation=affiliation,
            parent_id=parent_id,
            metadata=_decode_metadata(message.entity.metadata_json, max_size),
            position=location,
        )
    except (PydanticValidationError, ValueError):
        raise ValidationError(
            "protobuf entity contains invalid public fields",
            operation="decode_protobuf",
        ) from None
    return EntityEvent(timestamp=recorded_at, entity=entity, location=location)


def encode_location_protobuf(location: Location, max_size: int) -> bytes:
    """Encode only the independently generated public EntityLocationMessage subset."""

    message = entity_pb2.EntityLocationMessage()
    message.location.CopyFrom(_location_to_message(location))
    return _serialize(message, max_size, operation="encode_protobuf")


def decode_location_protobuf(topic: str, payload: bytes, max_size: int) -> LocationEvent:
    """Decode only the committed public EntityLocationMessage protobuf subset."""

    parsed_topic = parse_location_topic(topic)
    if not parsed_topic.protobuf:
        raise ProtocolError("JSON location topic requires JSON decoding")
    _check_payload_size(payload, max_size, operation="decode_protobuf")
    message = entity_pb2.EntityLocationMessage()
    try:
        message.ParseFromString(payload)
    except DecodeError:
        raise ProtocolError(
            "malformed location protobuf payload", operation="decode_protobuf"
        ) from None
    if not message.HasField("location"):
        raise ProtocolError(
            "location protobuf is missing the public location field",
            operation="decode_protobuf",
        )
    location = _location_from_message(message.location)
    return LocationEvent(
        entity_id=parsed_topic.entity_id,
        integration=parsed_topic.integration,
        timestamp=location.recorded_at,
        location=location,
    )


def encode_entity_payload(
    entity: Entity | EntityEvent,
    wire_format: WireFormat,
    max_size: int,
) -> bytes:
    """Encode an entity through the selected supported wire family."""

    if wire_format == WireFormat.JSON:
        return encode_entity_json(entity, max_size)
    if wire_format == WireFormat.PROTOBUF:
        return encode_entity_protobuf(entity, max_size)
    raise ValidationError("unsupported entity wire format", operation="encode_entity")


def decode_entity_payload(topic: str, payload: bytes, max_size: int) -> EntityEvent:
    """Choose the entity decoder strictly from its supported topic family."""

    parsed = parse_entity_topic(topic)
    if parsed.protobuf:
        return decode_entity_protobuf(topic, payload, max_size)
    return decode_entity_json(topic, payload, max_size)


def encode_location_payload(
    location: Location,
    wire_format: WireFormat,
    max_size: int,
) -> bytes:
    """Encode a location through the selected supported wire family."""

    if wire_format == WireFormat.JSON:
        return encode_location_json(location, max_size)
    if wire_format == WireFormat.PROTOBUF:
        return encode_location_protobuf(location, max_size)
    raise ValidationError("unsupported location wire format", operation="encode_location")


def decode_location_payload(topic: str, payload: bytes, max_size: int) -> LocationEvent:
    """Choose the location decoder strictly from its supported topic family."""

    parsed: LocationTopic = parse_location_topic(topic)
    if parsed.protobuf:
        return decode_location_protobuf(topic, payload, max_size)
    return decode_location_json(topic, payload, max_size)


__all__ = [
    "decode_entity_json",
    "decode_entity_payload",
    "decode_entity_protobuf",
    "decode_location_json",
    "decode_location_payload",
    "decode_location_protobuf",
    "encode_entity_json",
    "encode_entity_payload",
    "encode_entity_protobuf",
    "encode_location_json",
    "encode_location_payload",
    "encode_location_protobuf",
]
