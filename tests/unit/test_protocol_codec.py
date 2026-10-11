# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from google.protobuf.descriptor import FileDescriptor
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorProto

from picogrid_ecn_client import decode_entity_event_protobuf
from picogrid_ecn_client._geodesy import ecef_to_ned_velocity, geodetic_to_ecef
from picogrid_ecn_client._protocol import (
    build_entity_protobuf_topic,
    build_entity_topic,
    build_location_protobuf_topic,
    build_location_topic,
    codec,
    decode_entity_json,
    decode_entity_protobuf,
    decode_json,
    decode_location_json,
    decode_location_protobuf,
    encode_entity_json,
    encode_entity_protobuf,
    encode_json,
    encode_location_json,
    encode_location_protobuf,
)
from picogrid_ecn_client._protocol.generated import common_pb2, entity_pb2
from picogrid_ecn_client.exceptions import ProtocolError, ResourceLimitError, ValidationError
from picogrid_ecn_client.models import (
    Affiliation,
    AngularVelocity,
    DisplayMetadata,
    Entity,
    EntityCategory,
    EntityEvent,
    EntityMetadata,
    EntityStatus,
    Location,
    Velocity,
)

ENTITY_ID = UUID("12345678-1234-5678-9234-567812345678")
RECORDED_AT = datetime(2026, 8, 5, 12, 30, tzinfo=UTC)
MAX_SIZE = 64 * 1024


def _location() -> Location:
    return Location(
        latitude=34.5,
        longitude=-117.25,
        altitude=123.5,
        bearing=181.25,
        accuracy=2.5,
        source="synthetic-test",
        recorded_at=RECORDED_AT,
        velocity=Velocity(north=1.0, east=2.0, down=-0.5),
        angular_velocity=AngularVelocity(roll=0.1, pitch=0.2, yaw=0.3),
        confidence=0.75,
    )


def _entity() -> Entity:
    return Entity(
        id=ENTITY_ID,
        category=EntityCategory.TRACK,
        integration="vendor-a",
        recorded_at=RECORDED_AT,
        type="UAV",
        name="Synthetic track",
        status=EntityStatus.ACTIVE,
        affiliation=Affiliation.FRIEND,
        metadata=EntityMetadata(properties={"quality": "test"}),
        position=_location(),
    )


def test_json_codec_is_deterministic_and_rejects_ambiguous_input() -> None:
    assert encode_json({"b": 2, "a": 1}, MAX_SIZE) == b'{"a":1,"b":2}'
    assert decode_json(b'{"a":1,"b":2}', MAX_SIZE) == {"a": 1, "b": 2}

    with pytest.raises(ProtocolError):
        decode_json(b'{"a":1,"a":2}', MAX_SIZE)
    with pytest.raises(ProtocolError):
        decode_json(b'{"value":NaN}', MAX_SIZE)
    with pytest.raises(ProtocolError):
        decode_json(b"[]", MAX_SIZE)
    with pytest.raises(ValidationError):
        encode_json({"value": float("inf")}, MAX_SIZE)


def test_json_codec_enforces_size_in_both_directions() -> None:
    with pytest.raises(ResourceLimitError):
        encode_json({"value": "too-large"}, 8)
    with pytest.raises(ResourceLimitError):
        decode_json(b'{"value":"too-large"}', 8)


def test_json_codec_translates_excessive_nesting_to_public_errors() -> None:
    deeply_nested_payload = b'{"value":' + (b"[" * 10_000) + b"0" + (b"]" * 10_000) + b"}"
    with pytest.raises(ProtocolError, match="malformed JSON"):
        decode_json(deeply_nested_payload, MAX_SIZE)

    deeply_nested_value: object = 0
    for _ in range(10_000):
        deeply_nested_value = [deeply_nested_value]
    with pytest.raises(ValidationError, match="unsupported value"):
        encode_json({"value": deeply_nested_value}, MAX_SIZE)


def test_json_codec_nesting_bound_is_exact_for_lists_and_tuples() -> None:
    # The document object counts as one level, so 63 arrays reach the 64-level bound.
    bounded_payload = b'{"value":' + (b"[" * 63) + b"0" + (b"]" * 63) + b"}"
    assert isinstance(decode_json(bounded_payload, MAX_SIZE), dict)
    unbounded_payload = b'{"value":' + (b"[" * 64) + b"0" + (b"]" * 64) + b"}"
    with pytest.raises(ProtocolError, match="malformed JSON"):
        decode_json(unbounded_payload, MAX_SIZE)

    # json.dumps serializes tuples as arrays, so tuple nesting counts against
    # the same bound on the encode side.
    bounded_value: object = 0
    for _ in range(63):
        bounded_value = (bounded_value,)
    assert encode_json({"value": bounded_value}, MAX_SIZE)
    unbounded_value: object = (bounded_value,)
    with pytest.raises(ValidationError, match="unsupported value"):
        encode_json({"value": unbounded_value}, MAX_SIZE)


def test_entity_json_round_trip_checks_topic_identity() -> None:
    entity = _entity()
    topic = build_entity_topic(entity.integration, entity.id, entity.category)
    payload = encode_entity_json(entity, MAX_SIZE)
    wire = decode_json(payload, MAX_SIZE)
    assert wire["integration_name"] == "vendor-a"
    assert wire["id"] == str(ENTITY_ID)
    assert wire["type_"] == "UAV"
    assert wire["status"] == "active"
    assert "integration" not in wire
    assert "position" not in wire
    assert wire["metadata"] == {"quality": "test"}
    assert wire["location"]["velocity"] == [1.0, 2.0, -0.5]

    event = decode_entity_json(topic, payload, MAX_SIZE)
    assert event.entity == entity
    assert event.location == entity.position

    wrong_topic = build_entity_topic("another-vendor", entity.id, entity.category)
    with pytest.raises(ProtocolError):
        decode_entity_json(wrong_topic, encode_entity_json(entity, MAX_SIZE), MAX_SIZE)

    whitespace_integration = decode_json(payload, MAX_SIZE)
    whitespace_integration["integration_name"] = " vendor-a "
    with pytest.raises(ProtocolError, match="integration does not match"):
        decode_entity_json(topic, encode_json(whitespace_integration, MAX_SIZE), MAX_SIZE)


def test_entity_json_accepts_pinned_sparse_model_dump_shape() -> None:
    topic = build_entity_topic("vendor-a", ENTITY_ID, EntityCategory.TRACK)
    payload = encode_json(
        {
            "id": str(ENTITY_ID),
            "fingerprint": "synthetic-fingerprint",
            "integration_name": "vendor-a",
            "recorded_at": RECORDED_AT.isoformat(),
            "category": "track",
            "name": None,
            "type_": None,
            "type": "unconfirmed-alias-must-be-ignored",
            "status": None,
            "affiliation": None,
            "organization_id": None,
            "metadata": None,
            "parent_id": None,
            "_otel": {"traceparent": "synthetic-context"},
        },
        MAX_SIZE,
    )

    event = decode_entity_json(topic, payload, MAX_SIZE)

    assert event.entity.type == "UNKNOWN"
    assert event.entity.status is EntityStatus.UNKNOWN
    assert event.entity.affiliation is Affiliation.UNKNOWN
    assert event.entity.metadata == EntityMetadata()


def test_entity_json_treats_metadata_as_flat_and_ignores_future_display_fields() -> None:
    topic = build_entity_topic("vendor-a", ENTITY_ID, EntityCategory.TRACK)
    wire = decode_json(encode_entity_json(_entity(), MAX_SIZE), MAX_SIZE)
    wire["metadata"] = {
        "properties": {"vendor": 1},
        "display": {"label": "Synthetic", "future_display_field": True},
    }

    event = decode_entity_json(topic, encode_json(wire, MAX_SIZE), MAX_SIZE)

    assert event.entity.metadata.display == DisplayMetadata(label="Synthetic")
    assert event.entity.metadata.properties == {"properties": {"vendor": 1}}

    wire["metadata"] = {"display": "vendor-owned"}
    event = decode_entity_json(topic, encode_json(wire, MAX_SIZE), MAX_SIZE)
    assert event.entity.metadata.display is None
    assert event.entity.metadata.properties == {"display": "vendor-owned"}


def test_entity_json_discards_invalid_optional_embedded_location() -> None:
    topic = build_entity_topic("vendor-a", ENTITY_ID, EntityCategory.TRACK)
    wire = decode_json(encode_entity_json(_entity(), MAX_SIZE), MAX_SIZE)
    wire["location"]["latitude"] = 1000

    event = decode_entity_json(topic, encode_json(wire, MAX_SIZE), MAX_SIZE)

    assert event.entity.id == ENTITY_ID
    assert event.entity.position is None
    assert event.location is None


def test_entity_json_ignores_unknown_fields_and_tolerates_future_non_category_enums() -> None:
    topic = build_entity_topic("vendor-a", ENTITY_ID, EntityCategory.TRACK)
    payload = encode_json(
        {
            "id": str(ENTITY_ID),
            "integration_name": "vendor-a",
            "recorded_at": RECORDED_AT.isoformat(),
            "category": "TRACK",
            "status": "FUTURE_STATUS",
            "affiliation": "FUTURE_AFFILIATION",
            "type_": "synthetic",
            "future_entity_field": {"ignored": True},
            "location": {
                "latitude": 34.5,
                "longitude": -117.25,
                "recorded_at": RECORDED_AT.isoformat(),
                "future_location_field": "ignored",
            },
        },
        MAX_SIZE,
    )

    event = decode_entity_json(topic, payload, MAX_SIZE)

    assert event.entity.category is EntityCategory.TRACK
    assert event.entity.status is EntityStatus.UNKNOWN
    assert event.entity.affiliation is Affiliation.UNKNOWN
    assert event.location is not None
    assert event.location.latitude == 34.5


@pytest.mark.parametrize("field", ["status", "affiliation"])
@pytest.mark.parametrize("value", [1, True, {}, []])
def test_entity_json_rejects_non_string_enum_values(field: str, value: object) -> None:
    entity = _entity()
    topic = build_entity_topic(entity.integration, entity.id, entity.category)
    wire = decode_json(encode_entity_json(entity, MAX_SIZE), MAX_SIZE)
    wire[field] = value

    with pytest.raises(ValidationError, match="must be a string or null"):
        decode_entity_json(topic, encode_json(wire, MAX_SIZE), MAX_SIZE)


def test_entity_json_maps_future_topic_category_to_other() -> None:
    topic = f"entity/vendor-a/{ENTITY_ID}/future-category"
    payload = encode_json(
        {
            "id": str(ENTITY_ID),
            "integration_name": "vendor-a",
            "recorded_at": RECORDED_AT.isoformat(),
            "category": "FUTURE-CATEGORY",
            "type_": "synthetic",
        },
        MAX_SIZE,
    )

    with pytest.raises(ProtocolError, match="category does not match"):
        decode_entity_json(
            build_entity_topic("vendor-a", ENTITY_ID, EntityCategory.TRACK),
            payload,
            MAX_SIZE,
        )
    event = decode_entity_json(topic, payload, MAX_SIZE)

    assert event.entity.category is EntityCategory.OTHER

    mismatched = decode_json(payload, MAX_SIZE)
    mismatched["category"] = "FUTURE_CATEGORY"
    with pytest.raises(ProtocolError, match="category does not match"):
        decode_entity_json(topic, encode_json(mismatched, MAX_SIZE), MAX_SIZE)


def test_location_json_requires_confirmed_wrapper_and_ignores_unknown_fields() -> None:
    location = _location()
    topic = build_location_topic("vendor-a", ENTITY_ID)
    encoded = encode_location_json(location, MAX_SIZE)
    encoded_wire = decode_json(encoded, MAX_SIZE)
    assert set(encoded_wire) == {"location"}
    assert encoded_wire["location"]["angular_velocity"] == pytest.approx([0.1, 0.2, 0.3])
    decoded = decode_location_json(topic, encoded, MAX_SIZE)
    future_wire = decode_json(encoded, MAX_SIZE)
    future_wire["location"]["future_location_field"] = True
    future_wire["future_envelope_field"] = True
    with_unknown_fields = decode_location_json(
        topic,
        encode_json(future_wire, MAX_SIZE),
        MAX_SIZE,
    )
    assert decoded.location == location
    assert with_unknown_fields == decoded

    with pytest.raises(ValidationError, match="required location field"):
        decode_location_json(
            topic, encode_json(location.model_dump(mode="json"), MAX_SIZE), MAX_SIZE
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("latitude", True),
        ("longitude", "-117.25"),
        ("altitude", False),
        ("bearing", "181.25"),
        ("accuracy", True),
        ("confidence", "0.75"),
        ("velocity", {"north": 1, "east": 2, "down": 3}),
        ("angular_velocity", {"roll": 1, "pitch": 2, "yaw": 3}),
    ],
)
def test_location_json_rejects_coerced_or_non_wire_numeric_shapes(
    field: str,
    value: object,
) -> None:
    topic = build_location_topic("vendor-a", ENTITY_ID)
    wire = decode_json(encode_location_json(_location(), MAX_SIZE), MAX_SIZE)
    wire["location"][field] = value

    with pytest.raises(ValidationError):
        decode_location_json(topic, encode_json(wire, MAX_SIZE), MAX_SIZE)


@pytest.mark.parametrize("timestamp", [0, "0", "2026-08-05"])
def test_json_wire_requires_iso_datetime_strings(timestamp: object) -> None:
    entity = _entity()
    entity_topic = build_entity_topic(entity.integration, entity.id, entity.category)
    entity_wire = decode_json(encode_entity_json(entity, MAX_SIZE), MAX_SIZE)
    entity_wire["recorded_at"] = timestamp
    with pytest.raises(ValidationError, match="recorded_at"):
        decode_entity_json(entity_topic, encode_json(entity_wire, MAX_SIZE), MAX_SIZE)

    location_topic = build_location_topic(entity.integration, entity.id)
    location_wire = decode_json(encode_location_json(_location(), MAX_SIZE), MAX_SIZE)
    location_wire["location"]["recorded_at"] = timestamp
    with pytest.raises(ValidationError, match="recorded_at"):
        decode_location_json(location_topic, encode_json(location_wire, MAX_SIZE), MAX_SIZE)


@pytest.mark.parametrize("field", ["latitude", "longitude", "altitude", "confidence"])
def test_public_location_model_rejects_boolean_numbers(field: str) -> None:
    values = _location().model_dump()
    values[field] = True
    with pytest.raises(ValueError, match="integer or float"):
        Location.model_validate(values)


def test_entity_json_rejects_unconfirmed_aliases_and_envelopes() -> None:
    entity = _entity()
    topic = build_entity_topic(entity.integration, entity.id, entity.category)
    aliases = entity.model_dump(mode="json")
    with pytest.raises(ValidationError, match="required wire fields"):
        decode_entity_json(topic, encode_json(aliases, MAX_SIZE), MAX_SIZE)
    with pytest.raises(ValidationError, match="required wire fields"):
        decode_entity_json(
            topic,
            encode_json(
                {"entity": decode_json(encode_entity_json(entity, MAX_SIZE), MAX_SIZE)}, MAX_SIZE
            ),
            MAX_SIZE,
        )


def test_entity_json_requires_canonical_uuid_text() -> None:
    entity = _entity().model_copy(update={"id": UUID("abcdefab-cdef-4abc-8def-abcdefabcdef")})
    topic = build_entity_topic(entity.integration, entity.id, entity.category)
    wire = decode_json(encode_entity_json(entity, MAX_SIZE), MAX_SIZE)
    wire["id"] = str(entity.id).upper()
    with pytest.raises(ValidationError, match="canonical UUID"):
        decode_entity_json(topic, encode_json(wire, MAX_SIZE), MAX_SIZE)


def test_entity_protobuf_round_trip_uses_only_public_subset() -> None:
    entity = _entity()
    topic = build_entity_protobuf_topic(entity.integration, entity.category)
    payload = encode_entity_protobuf(entity, MAX_SIZE)
    wire_message = entity_pb2.EntityEventMessage.FromString(payload)
    event = decode_entity_protobuf(topic, payload, MAX_SIZE)

    assert wire_message.entity.id == ENTITY_ID.bytes
    assert (
        wire_message.entity.recorded_at.seconds,
        wire_message.entity.recorded_at.nanos,
    ) == (wire_message.recorded_at.seconds, wire_message.recorded_at.nanos)
    assert not wire_message.entity.HasField("category")
    assert event.entity.id == entity.id
    assert event.entity.integration == entity.integration
    assert event.entity.category == EntityCategory.TRACK
    assert event.entity.type == "UAV"
    assert event.entity.metadata == entity.metadata
    assert event.entity.position is not None
    assert event.entity.position.latitude == pytest.approx(entity.position.latitude)
    assert event.entity.position.velocity is not None
    assert event.entity.position.velocity.down == pytest.approx(-0.5)
    assert set(common_pb2.DESCRIPTOR.message_types_by_name) == {
        "EntityMessage",
        "LocationMessage",
    }
    assert set(entity_pb2.DESCRIPTOR.message_types_by_name) == {
        "EntityEventMessage",
        "EntityLocationMessage",
    }
    assert common_pb2.DESCRIPTOR.package == "picogrid.open.v1"
    assert entity_pb2.DESCRIPTOR.package == "picogrid.open.v1"


def _normalized_descriptor(descriptor: FileDescriptor) -> dict[str, object]:
    file_descriptor = FileDescriptorProto()
    descriptor.CopyToProto(file_descriptor)
    return {
        "enums": {
            enum.name: tuple((value.name, value.number) for value in enum.value)
            for enum in file_descriptor.enum_type
        },
        "messages": {
            message.name: {
                "fields": tuple(
                    (
                        field.name,
                        field.number,
                        FieldDescriptorProto.Type.Name(field.type),
                        field.type_name.rsplit(".", 1)[-1] or None,
                        FieldDescriptorProto.Label.Name(field.label),
                        (
                            message.oneof_decl[field.oneof_index].name
                            if field.HasField("oneof_index")
                            else None
                        ),
                        field.proto3_optional,
                    )
                    for field in message.field
                ),
                "reserved_ranges": tuple((item.start, item.end) for item in message.reserved_range),
            }
            for message in file_descriptor.message_type
        },
    }


def test_public_protobuf_descriptor_matches_canonical_snapshot() -> None:
    assert _normalized_descriptor(common_pb2.DESCRIPTOR) == {
        "enums": {
            "EntityCategory": (
                ("ENTITY_CATEGORY_UNSPECIFIED", 0),
                ("ENTITY_CATEGORY_DEVICE", 1),
                ("ENTITY_CATEGORY_DETECTION", 2),
                ("ENTITY_CATEGORY_TRACK", 3),
                ("ENTITY_CATEGORY_SYSTEM", 4),
                ("ENTITY_CATEGORY_SENSOR", 5),
                ("ENTITY_CATEGORY_ALERT", 6),
                ("ENTITY_CATEGORY_WEATHER", 7),
                ("ENTITY_CATEGORY_GEOMETRIC", 8),
                ("ENTITY_CATEGORY_ZONE", 9),
                ("ENTITY_CATEGORY_VEHICLE", 10),
                ("ENTITY_CATEGORY_UXV", 11),
            ),
            "EntityStatus": (
                ("ENTITY_STATUS_UNSPECIFIED", 0),
                ("ENTITY_STATUS_ACTIVE", 1),
                ("ENTITY_STATUS_INACTIVE", 2),
                ("ENTITY_STATUS_UNKNOWN", 3),
            ),
            "Affiliation": (
                ("AFFILIATION_UNSPECIFIED", 0),
                ("AFFILIATION_FRIEND", 1),
                ("AFFILIATION_HOSTILE", 2),
                ("AFFILIATION_NEUTRAL", 3),
                ("AFFILIATION_UNKNOWN", 4),
                ("AFFILIATION_SUSPECT", 5),
                ("AFFILIATION_PENDING", 6),
                ("AFFILIATION_ASSUMED_FRIEND", 7),
                ("AFFILIATION_EXERCISE_PENDING", 8),
                ("AFFILIATION_EXERCISE_UNKNOWN", 9),
                ("AFFILIATION_EXERCISE_FRIEND", 10),
                ("AFFILIATION_EXERCISE_NEUTRAL", 11),
                ("AFFILIATION_EXERCISE_ASSUMED_FRIEND", 12),
                ("AFFILIATION_JOKER", 13),
                ("AFFILIATION_FAKER", 14),
                ("AFFILIATION_NONE_SPECIFIED", 15),
            ),
            "GeometryType": (
                ("GEOMETRY_TYPE_UNSPECIFIED", 0),
                ("GEOMETRY_TYPE_POINT", 1),
                ("GEOMETRY_TYPE_LINE_STRING", 2),
                ("GEOMETRY_TYPE_POLYGON", 3),
            ),
            "CRS": (
                ("CRS_UNSPECIFIED", 0),
                ("CRS_ECEF", 1),
                ("CRS_LLA", 2),
            ),
        },
        "messages": {
            "LocationMessage": {
                "fields": (
                    ("latitude", 1, "TYPE_DOUBLE", None, "LABEL_OPTIONAL", None, False),
                    ("longitude", 2, "TYPE_DOUBLE", None, "LABEL_OPTIONAL", None, False),
                    ("altitude", 3, "TYPE_FLOAT", None, "LABEL_OPTIONAL", "_altitude", True),
                    ("bearing", 4, "TYPE_FLOAT", None, "LABEL_OPTIONAL", "_bearing", True),
                    ("accuracy", 5, "TYPE_FLOAT", None, "LABEL_OPTIONAL", "_accuracy", True),
                    ("source", 6, "TYPE_STRING", None, "LABEL_OPTIONAL", "_source", True),
                    (
                        "recorded_at",
                        7,
                        "TYPE_MESSAGE",
                        "Timestamp",
                        "LABEL_OPTIONAL",
                        None,
                        False,
                    ),
                    ("velocity", 8, "TYPE_FLOAT", None, "LABEL_REPEATED", None, False),
                    (
                        "angular_velocity",
                        9,
                        "TYPE_FLOAT",
                        None,
                        "LABEL_REPEATED",
                        None,
                        False,
                    ),
                    ("confidence", 10, "TYPE_FLOAT", None, "LABEL_OPTIONAL", "_confidence", True),
                    ("radius", 11, "TYPE_FLOAT", None, "LABEL_OPTIONAL", "_radius", True),
                    ("covariance", 12, "TYPE_FLOAT", None, "LABEL_REPEATED", None, False),
                    ("orientation", 13, "TYPE_FLOAT", None, "LABEL_REPEATED", None, False),
                    ("acceleration", 14, "TYPE_FLOAT", None, "LABEL_REPEATED", None, False),
                    ("crs", 15, "TYPE_ENUM", "CRS", "LABEL_OPTIONAL", "_crs", True),
                    (
                        "geometry_type",
                        16,
                        "TYPE_ENUM",
                        "GeometryType",
                        "LABEL_OPTIONAL",
                        "_geometry_type",
                        True,
                    ),
                    ("coordinates", 17, "TYPE_DOUBLE", None, "LABEL_REPEATED", None, False),
                ),
                "reserved_ranges": (),
            },
            "EntityMessage": {
                "fields": (
                    ("id", 1, "TYPE_BYTES", None, "LABEL_OPTIONAL", None, False),
                    (
                        "recorded_at",
                        2,
                        "TYPE_MESSAGE",
                        "Timestamp",
                        "LABEL_OPTIONAL",
                        None,
                        False,
                    ),
                    ("name", 3, "TYPE_STRING", None, "LABEL_OPTIONAL", "_name", True),
                    ("type_name", 4, "TYPE_STRING", None, "LABEL_OPTIONAL", "_type_name", True),
                    (
                        "category",
                        5,
                        "TYPE_ENUM",
                        "EntityCategory",
                        "LABEL_OPTIONAL",
                        "_category",
                        True,
                    ),
                    (
                        "status",
                        6,
                        "TYPE_ENUM",
                        "EntityStatus",
                        "LABEL_OPTIONAL",
                        "_status",
                        True,
                    ),
                    (
                        "affiliation",
                        7,
                        "TYPE_ENUM",
                        "Affiliation",
                        "LABEL_OPTIONAL",
                        "_affiliation",
                        True,
                    ),
                    ("parent_id", 8, "TYPE_BYTES", None, "LABEL_OPTIONAL", "_parent_id", True),
                    (
                        "metadata_json",
                        9,
                        "TYPE_BYTES",
                        None,
                        "LABEL_OPTIONAL",
                        "_metadata_json",
                        True,
                    ),
                    (
                        "classification_json",
                        10,
                        "TYPE_BYTES",
                        None,
                        "LABEL_OPTIONAL",
                        "_classification_json",
                        True,
                    ),
                ),
                "reserved_ranges": ((11, 12), (12, 13)),
            },
        },
    }
    assert _normalized_descriptor(entity_pb2.DESCRIPTOR) == {
        "enums": {},
        "messages": {
            "EntityEventMessage": {
                "fields": (
                    (
                        "recorded_at",
                        1,
                        "TYPE_MESSAGE",
                        "Timestamp",
                        "LABEL_OPTIONAL",
                        None,
                        False,
                    ),
                    (
                        "entity",
                        2,
                        "TYPE_MESSAGE",
                        "EntityMessage",
                        "LABEL_OPTIONAL",
                        None,
                        False,
                    ),
                    (
                        "location",
                        3,
                        "TYPE_MESSAGE",
                        "LocationMessage",
                        "LABEL_OPTIONAL",
                        None,
                        False,
                    ),
                ),
                "reserved_ranges": (),
            },
            "EntityLocationMessage": {
                "fields": (
                    ("entity_id", 1, "TYPE_BYTES", None, "LABEL_OPTIONAL", None, False),
                    (
                        "location",
                        2,
                        "TYPE_MESSAGE",
                        "LocationMessage",
                        "LABEL_OPTIONAL",
                        None,
                        False,
                    ),
                ),
                "reserved_ranges": (),
            },
        },
    }


def test_entity_protobuf_location_uses_event_timestamp_when_omitted() -> None:
    message = entity_pb2.EntityEventMessage()
    message.recorded_at.FromDatetime(RECORDED_AT)
    message.entity.id = ENTITY_ID.bytes
    message.entity.type_name = "UAV"
    message.entity.category = common_pb2.ENTITY_CATEGORY_TRACK
    message.location.latitude = 34.5
    message.location.longitude = -117.25

    event = decode_entity_protobuf(
        build_entity_protobuf_topic("vendor-a", EntityCategory.TRACK),
        message.SerializeToString(),
        MAX_SIZE,
    )

    assert event.location is not None
    assert event.location.recorded_at == RECORDED_AT


def test_entity_protobuf_discards_invalid_optional_embedded_location() -> None:
    message = entity_pb2.EntityEventMessage()
    message.recorded_at.FromDatetime(RECORDED_AT)
    message.entity.id = ENTITY_ID.bytes
    message.location.latitude = 1000
    message.location.longitude = -117.25

    event = decode_entity_protobuf(
        build_entity_protobuf_topic("vendor-a", EntityCategory.TRACK),
        message.SerializeToString(),
        MAX_SIZE,
    )

    assert event.entity.id == ENTITY_ID
    assert event.entity.position is None
    assert event.location is None


def test_entity_protobuf_rejects_malformed_parent_id() -> None:
    message = entity_pb2.EntityEventMessage()
    message.recorded_at.FromDatetime(RECORDED_AT)
    message.entity.id = ENTITY_ID.bytes
    message.entity.parent_id = b"not-a-uuid"

    with pytest.raises(ProtocolError, match="parent entity ID"):
        decode_entity_protobuf(
            build_entity_protobuf_topic("vendor-a", EntityCategory.TRACK),
            message.SerializeToString(),
            MAX_SIZE,
        )


@pytest.mark.parametrize("field", ["status", "affiliation"])
def test_entity_protobuf_tolerates_future_enum_values(field: str) -> None:
    message = entity_pb2.EntityEventMessage()
    message.recorded_at.FromDatetime(RECORDED_AT)
    message.entity.id = ENTITY_ID.bytes
    setattr(message.entity, field, 99)

    event = decode_entity_protobuf(
        build_entity_protobuf_topic("vendor-a", EntityCategory.TRACK),
        message.SerializeToString(),
        MAX_SIZE,
    )

    assert event.entity.category is EntityCategory.TRACK
    assert event.entity.status is EntityStatus.UNKNOWN
    assert event.entity.affiliation is Affiliation.UNKNOWN


def test_entity_protobuf_maps_future_category_and_retains_topic_agreement() -> None:
    message = entity_pb2.EntityEventMessage()
    message.recorded_at.FromDatetime(RECORDED_AT)
    message.entity.id = ENTITY_ID.bytes
    message.entity.category = 99
    payload = message.SerializeToString()

    with pytest.raises(ProtocolError, match="category does not match"):
        decode_entity_protobuf(
            build_entity_protobuf_topic("vendor-a", EntityCategory.TRACK),
            payload,
            MAX_SIZE,
        )

    event = decode_entity_protobuf(
        "entity_pb/vendor-a/future-category",
        payload,
        MAX_SIZE,
    )
    assert event.entity.category is EntityCategory.OTHER

    public_event = decode_entity_event_protobuf(
        payload,
        integration="vendor-a",
        category=EntityCategory.OTHER,
    )
    assert public_event.entity.category is EntityCategory.OTHER


@pytest.mark.parametrize("component", ["seconds", "nanos"])
def test_entity_protobuf_rejects_mismatched_entity_recorded_at(component: str) -> None:
    message = entity_pb2.EntityEventMessage.FromString(encode_entity_protobuf(_entity(), MAX_SIZE))
    setattr(
        message.entity.recorded_at,
        component,
        getattr(message.entity.recorded_at, component) + 1,
    )

    with pytest.raises(
        ProtocolError,
        match="entity protobuf recorded_at does not match its envelope",
    ):
        decode_entity_protobuf(
            build_entity_protobuf_topic("vendor-a", EntityCategory.TRACK),
            message.SerializeToString(),
            MAX_SIZE,
        )


def test_entity_protobuf_accepts_unset_entity_recorded_at() -> None:
    message = entity_pb2.EntityEventMessage.FromString(encode_entity_protobuf(_entity(), MAX_SIZE))
    message.entity.ClearField("recorded_at")

    event = decode_entity_protobuf(
        build_entity_protobuf_topic("vendor-a", EntityCategory.TRACK),
        message.SerializeToString(),
        MAX_SIZE,
    )

    assert event.timestamp == RECORDED_AT


def test_entity_protobuf_round_trips_new_enums() -> None:
    entity = _entity().model_copy(
        update={
            "category": EntityCategory.WEATHER,
            "affiliation": Affiliation.EXERCISE_FRIEND,
        }
    )
    payload = encode_entity_protobuf(entity, MAX_SIZE)
    message = entity_pb2.EntityEventMessage.FromString(payload)

    message.entity.category = common_pb2.ENTITY_CATEGORY_WEATHER
    payload = message.SerializeToString()
    assert message.entity.affiliation == common_pb2.AFFILIATION_EXERCISE_FRIEND
    event = decode_entity_protobuf(
        build_entity_protobuf_topic(entity.integration, entity.category),
        payload,
        MAX_SIZE,
    )
    assert event.entity.category is EntityCategory.WEATHER
    assert event.entity.affiliation is Affiliation.EXERCISE_FRIEND


def test_entity_protobuf_rejects_mismatched_location_timestamp() -> None:
    entity = _entity()
    assert entity.position is not None
    mismatched = entity.position.model_copy(
        update={"recorded_at": datetime(2026, 8, 5, 12, 31, tzinfo=UTC)}
    )
    event = EntityEvent(timestamp=entity.recorded_at, entity=entity, location=mismatched)

    for encoder in (encode_entity_json, encode_entity_protobuf):
        with pytest.raises(ValidationError, match="must match"):
            encoder(event, MAX_SIZE)


def test_entity_publication_rejects_cross_format_event_timestamp_or_location_ambiguity() -> None:
    entity = _entity()
    later = datetime(2026, 8, 5, 12, 31, tzinfo=UTC)
    event = EntityEvent(timestamp=later, entity=entity)
    for encoder in (encode_entity_json, encode_entity_protobuf):
        with pytest.raises(ValidationError, match="event timestamp must match"):
            encoder(event, MAX_SIZE)

    assert entity.position is not None
    conflicting = entity.position.model_copy(update={"latitude": 1.0})
    event = EntityEvent(timestamp=entity.recorded_at, entity=entity, location=conflicting)
    for encoder in (encode_entity_json, encode_entity_protobuf):
        with pytest.raises(ValidationError, match="location must match entity position"):
            encoder(event, MAX_SIZE)


def test_location_protobuf_round_trip_and_malformed_translation() -> None:
    location = _location()
    topic = build_location_protobuf_topic("vendor-a")
    payload = encode_location_protobuf(location, ENTITY_ID, MAX_SIZE)
    message = entity_pb2.EntityLocationMessage.FromString(payload)
    event = decode_location_protobuf(topic, payload, MAX_SIZE)

    assert message.entity_id == ENTITY_ID.bytes
    assert event.entity_id == ENTITY_ID
    assert event.integration == "vendor-a"
    assert event.location.longitude == pytest.approx(location.longitude)

    with pytest.raises(ProtocolError):
        decode_location_protobuf(topic, b"\xff", MAX_SIZE)
    with pytest.raises(ResourceLimitError):
        decode_location_protobuf(topic, b"0123456789", 4)


@pytest.mark.parametrize("entity_id", [b"", b"\x00" * 15])
def test_location_protobuf_rejects_invalid_payload_entity_id(entity_id: bytes) -> None:
    message = entity_pb2.EntityLocationMessage(entity_id=entity_id)
    message.location.CopyFrom(codec._location_to_message(_location()))

    with pytest.raises(
        ProtocolError,
        match="location protobuf entity ID must contain 16 bytes",
    ):
        decode_location_protobuf(
            build_location_protobuf_topic("vendor-a"),
            message.SerializeToString(),
            MAX_SIZE,
        )


def test_location_protobuf_tolerates_extended_fields() -> None:
    message = entity_pb2.EntityLocationMessage(entity_id=ENTITY_ID.bytes)
    message.location.CopyFrom(codec._location_to_message(_location()))
    message.location.radius = 25.0
    message.location.covariance.extend([1.0] * 9)
    message.location.orientation.extend([0.0, 0.0, 0.0, 1.0])
    message.location.acceleration.extend([0.1, 0.2, 0.3])
    message.location.geometry_type = common_pb2.GEOMETRY_TYPE_POINT
    message.location.coordinates.extend([1.0, 2.0, 3.0])

    event = decode_location_protobuf(
        build_location_protobuf_topic("vendor-a"),
        message.SerializeToString(),
        MAX_SIZE,
    )

    assert event.location.latitude == pytest.approx(_location().latitude)
    assert event.location.longitude == pytest.approx(_location().longitude)
    assert not hasattr(event.location, "radius")
    assert not hasattr(event.location, "covariance")
    assert not hasattr(event.location, "orientation")
    assert not hasattr(event.location, "acceleration")
    assert not hasattr(event.location, "geometry_type")
    assert not hasattr(event.location, "coordinates")


def test_location_protobuf_converts_ecef_position_and_vectors_to_geodetic_ned() -> None:
    latitude, longitude, altitude = 34.5, -117.25, 123.5
    x, y, z = geodetic_to_ecef(latitude, longitude, altitude)
    message = entity_pb2.EntityLocationMessage(entity_id=ENTITY_ID.bytes)
    message.location.latitude = x
    message.location.longitude = y
    message.location.altitude = z
    message.location.recorded_at.FromDatetime(RECORDED_AT)
    message.location.crs = common_pb2.CRS_ECEF
    message.location.velocity.extend([1.0, 2.0, -0.5])
    message.location.angular_velocity.extend([0.1, 0.2, -0.05])

    event = decode_location_protobuf(
        build_location_protobuf_topic("vendor-a"),
        message.SerializeToString(),
        MAX_SIZE,
    )

    assert event.location.latitude == pytest.approx(latitude, abs=1e-5)
    assert event.location.longitude == pytest.approx(longitude, abs=1e-5)
    assert event.location.altitude == pytest.approx(altitude, abs=0.1)
    velocity = ecef_to_ned_velocity(latitude, longitude, 1.0, 2.0, -0.5)
    assert event.location.velocity is not None
    assert (
        event.location.velocity.north,
        event.location.velocity.east,
        event.location.velocity.down,
    ) == pytest.approx(velocity)
    angular_velocity = ecef_to_ned_velocity(latitude, longitude, 0.1, 0.2, -0.05)
    assert event.location.angular_velocity is not None
    assert (
        event.location.angular_velocity.roll,
        event.location.angular_velocity.pitch,
        event.location.angular_velocity.yaw,
    ) == pytest.approx(angular_velocity)


def test_location_protobuf_rejects_ecef_without_z() -> None:
    message = entity_pb2.EntityLocationMessage(entity_id=ENTITY_ID.bytes)
    message.location.latitude = 1000000.0
    message.location.longitude = 2000000.0
    message.location.recorded_at.FromDatetime(RECORDED_AT)
    message.location.crs = common_pb2.CRS_ECEF

    with pytest.raises(ProtocolError, match="ECEF location is missing its z coordinate"):
        decode_location_protobuf(
            build_location_protobuf_topic("vendor-a"),
            message.SerializeToString(),
            MAX_SIZE,
        )


def test_protobuf_geometric_round_trip_derives_category_from_topic() -> None:
    entity = _entity().model_copy(update={"category": EntityCategory.GEOMETRIC})
    topic = build_entity_protobuf_topic(entity.integration, entity.category)
    payload = encode_entity_protobuf(entity, MAX_SIZE)
    message = entity_pb2.EntityEventMessage.FromString(payload)

    assert not message.entity.HasField("category")
    assert (
        decode_entity_protobuf(topic, payload, MAX_SIZE).entity.category is EntityCategory.GEOMETRIC
    )


@pytest.mark.parametrize("invalid_crs", [common_pb2.CRS_UNSPECIFIED, 99])
def test_location_protobuf_rejects_invalid_present_crs(invalid_crs: int) -> None:
    message = entity_pb2.EntityLocationMessage(entity_id=ENTITY_ID.bytes)
    message.location.latitude = 34.5
    message.location.longitude = -117.25
    message.location.recorded_at.FromDatetime(RECORDED_AT)
    message.location.crs = invalid_crs

    with pytest.raises(ProtocolError, match="invalid crs"):
        decode_location_protobuf(
            build_location_protobuf_topic("vendor-a"),
            message.SerializeToString(),
            MAX_SIZE,
        )


def test_protobuf_rejects_fields_not_in_the_committed_subset() -> None:
    other = _entity().model_copy(update={"category": EntityCategory.OTHER})
    with pytest.raises(ValidationError, match="inbound fallback"):
        encode_entity_protobuf(other, MAX_SIZE)

    fingerprinted = _entity().model_copy(update={"fingerprint": "synthetic-fingerprint"})
    with pytest.raises(ValidationError, match="fingerprint"):
        encode_entity_protobuf(fingerprinted, MAX_SIZE)


def test_json_rejects_decode_only_other_category_for_publication() -> None:
    entity = _entity().model_copy(update={"category": EntityCategory.OTHER})
    with pytest.raises(ValidationError, match="inbound fallback"):
        encode_entity_json(entity, MAX_SIZE)


def test_protobuf_entity_identity_extraction_skips_full_message_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity = _entity().model_copy(update={"category": EntityCategory.TRACK})
    topic = build_entity_protobuf_topic(entity.integration, entity.category)
    payload = encode_entity_protobuf(entity, MAX_SIZE)
    monkeypatch.setattr(
        codec.entity_pb2,
        "EntityEventMessage",
        lambda: pytest.fail("full protobuf message was constructed"),
    )

    assert codec._extract_entity_identity(topic, payload, MAX_SIZE) == (
        entity.integration,
        entity.id,
    )


def test_protobuf_entity_identity_extraction_is_strict() -> None:
    message = entity_pb2.EntityEventMessage()
    message.entity.id = ENTITY_ID.bytes
    message.entity.category = common_pb2.ENTITY_CATEGORY_DETECTION
    payload = message.SerializeToString(deterministic=True)
    topic = build_entity_protobuf_topic("vendor-a", EntityCategory.TRACK)

    with pytest.raises(ProtocolError, match="category does not match"):
        codec._extract_entity_identity(topic, payload, MAX_SIZE)
    with pytest.raises(ProtocolError, match="truncated"):
        codec._extract_entity_identity(topic, b"\x12", MAX_SIZE)

    short_id = entity_pb2.EntityEventMessage()
    short_id.entity.id = b"\x00" * 8
    with pytest.raises(ProtocolError, match="16 bytes"):
        codec._extract_entity_identity(
            topic,
            short_id.SerializeToString(deterministic=True),
            MAX_SIZE,
        )
    with pytest.raises(ResourceLimitError):
        codec._extract_entity_identity(topic, b"\x00" * 32, 8)


def test_protobuf_entity_identity_rejects_excessive_group_nesting() -> None:
    topic = build_entity_protobuf_topic("vendor-a", EntityCategory.TRACK)

    with pytest.raises(ProtocolError, match="group nesting"):
        codec._extract_entity_identity(topic, b"\x0b" * 65, MAX_SIZE)
