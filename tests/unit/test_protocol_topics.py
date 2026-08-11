# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest

from picogrid_ecn_client._protocol import (
    ENTITY_JSON_SUBSCRIPTION,
    ENTITY_PROTOBUF_SUBSCRIPTION,
    LOCATION_JSON_SUBSCRIPTION,
    LOCATION_PROTOBUF_SUBSCRIPTION,
    build_entity_protobuf_topic,
    build_entity_subscription_filters,
    build_entity_topic,
    build_location_protobuf_topic,
    build_location_subscription_filters,
    build_location_topic,
    build_task_request_topic,
    build_task_response_topic,
    parse_entity_topic,
    parse_location_topic,
    parse_protocol_topic,
    parse_task_topic,
    validate_publish_topic,
    validate_subscription_filter,
)
from picogrid_ecn_client._protocol.topics import UNSUPPORTED_TOPIC_FAMILY, TopicFamily
from picogrid_ecn_client.exceptions import ProtocolError, ValidationError
from picogrid_ecn_client.models import EntityCategory

ENTITY_ID = UUID("12345678-1234-5678-9234-567812345678")
LETTERED_ENTITY_ID = UUID("abcdefab-cdef-4abc-8def-abcdefabcdef")
TERMINAL_ID = UUID("11111111-1111-4111-8111-111111111111")
_SUPPORTED_TOPIC_FAMILIES = frozenset(family.value for family in TopicFamily)
_TOPIC_FAMILY_LABELS = _SUPPORTED_TOPIC_FAMILIES | {UNSUPPORTED_TOPIC_FAMILY}
_JWT_MARKER = "canary1marker"
_PEM_MARKER = "canary2marker"
_BEARER_MARKER = "canary3marker"
_PASSWORD_MARKER = "canary4marker"
_CLIENT_SECRET_MARKER = "canary5marker"
_AWS_MARKER = "CANARY6MARKER"
_HOST_MARKER = "canary7marker"
_URL_MARKER = "canary8marker"
_OPAQUE_MARKER = "canary9marker"
_UNICODE_MARKER = "canary10marker"
_ADVERSARIAL_TOPIC_SEGMENTS = (
    (
        ".".join(
            (
                _JWT_MARKER + "A" * 24,
                "B" * 12 + _JWT_MARKER + "C" * 12,
                "D" * 24 + _JWT_MARKER,
            )
        ),
        _JWT_MARKER,
        ("BBBBBBBBBBBB", "DDDDDDDDDDDD"),
    ),
    (
        "-----BEGIN " + "RSA PRIVATE KEY-----\n" + _PEM_MARKER + "\n-----END RSA PRIVATE KEY-----",
        _PEM_MARKER,
        ("RSA PRIVATE", "END RSA"),
    ),
    (
        "Authorization" + ": Bearer " + _BEARER_MARKER + "-credential",
        _BEARER_MARKER,
        ("Bearer", "credential"),
    ),
    (
        "password=" + _PASSWORD_MARKER + "-credential",
        _PASSWORD_MARKER,
        ("password", "credential"),
    ),
    (
        "client_secret=" + _CLIENT_SECRET_MARKER + "-credential",
        _CLIENT_SECRET_MARKER,
        ("client_secret", "credential"),
    ),
    (
        "aws_access_key=" + "AKIA" + _AWS_MARKER + "XYZ",
        _AWS_MARKER,
        ("AKIA", "MARKERXYZ"),
    ),
    (
        _HOST_MARKER + "-broker" + ".invalid",
        _HOST_MARKER,
        ("-broker", ".invalid"),
    ),
    (
        "https://" + _URL_MARKER + "-user:" + _URL_MARKER + "-pass@broker" + ".invalid",
        _URL_MARKER,
        ("-user:", "-pass@"),
    ),
    (
        "A9z_" * 48 + _OPAQUE_MARKER + "Q7x-" * 48,
        _OPAQUE_MARKER,
        ("A9z_A9z_", "Q7x-Q7x-"),
    ),
    (
        "前置\x1f" + _UNICODE_MARKER + "\x7f終端",
        _UNICODE_MARKER,
        ("前置", "終端"),
    ),
)


def _assert_no_leak(
    error: ProtocolError | ValidationError,
    canary: str,
    marker: str,
    distinctive_substrings: tuple[str, ...],
) -> None:
    surfaces = [
        error.message,
        error.code,
        error.operation or "",
        *(repr(key) for key in error.details),
        *(repr(value) for value in error.details.values()),
        repr(error.args),
        str(error),
        repr(error),
        "".join(traceback.format_exception(type(error), error, error.__traceback__)),
    ]
    for surface in surfaces:
        assert canary not in surface
        assert marker not in surface
        for substring in distinctive_substrings:
            assert substring not in surface


def test_fixed_topic_builders_and_parsers_round_trip() -> None:
    entity_json = build_entity_topic("vendor-a", ENTITY_ID, EntityCategory.TRACK)
    entity_pb = build_entity_protobuf_topic("vendor-a", EntityCategory.DETECTION)
    location_json = build_location_topic("vendor-a", ENTITY_ID)
    location_pb = build_location_protobuf_topic("vendor-a", ENTITY_ID)
    task = build_task_request_topic("vendor-a", ENTITY_ID, "slew")
    response = build_task_response_topic("vendor-a", ENTITY_ID, "slew")

    assert entity_json == f"entity/vendor-a/{ENTITY_ID}/track"
    assert parse_entity_topic(entity_json).entity_id == ENTITY_ID
    assert parse_entity_topic(entity_pb).protobuf is True
    assert parse_location_topic(location_json).protobuf is False
    assert parse_location_topic(location_pb).entity_id == ENTITY_ID
    assert parse_task_topic(task).response is False
    assert parse_task_topic(response).response is True
    assert parse_protocol_topic(response) == parse_task_topic(response)


def test_topic_builders_emit_canonical_lowercase_uuid_text() -> None:
    canonical = str(LETTERED_ENTITY_ID)
    assert (
        build_entity_topic("vendor-a", LETTERED_ENTITY_ID, EntityCategory.TRACK).split("/")[2]
        == canonical
    )
    assert build_location_topic("vendor-a", LETTERED_ENTITY_ID).split("/")[2] == canonical
    assert build_location_protobuf_topic("vendor-a", LETTERED_ENTITY_ID).split("/")[2] == canonical
    assert (
        build_task_request_topic("vendor-a", LETTERED_ENTITY_ID, "slew").split("/")[2] == canonical
    )
    assert (
        build_task_response_topic("vendor-a", LETTERED_ENTITY_ID, "slew").split("/")[2] == canonical
    )


def test_terminal_addressed_task_topics_are_publish_only_and_round_trip() -> None:
    request = build_task_request_topic(
        "vendor-a",
        ENTITY_ID,
        "slew",
        target_terminal_id=TERMINAL_ID,
    )
    response = build_task_response_topic(
        "vendor-a",
        ENTITY_ID,
        "slew",
        route_terminal_id=TERMINAL_ID,
    )

    assert request == f"{TERMINAL_ID}/task/vendor-a/{ENTITY_ID}/slew"
    assert response == f"{TERMINAL_ID}/task/vendor-a/{ENTITY_ID}/slew/response"
    assert parse_task_topic(request).route_terminal_id == TERMINAL_ID
    assert parse_task_topic(response).response is True
    assert validate_publish_topic(request) == request
    assert validate_publish_topic(response) == response
    for topic_filter in (request, response):
        with pytest.raises(ValidationError, match="routing infrastructure"):
            validate_subscription_filter(topic_filter)


@pytest.mark.parametrize(
    "integration",
    ["x", "-leading", "trailing-", "contains.dot", "geolocation"],
)
def test_topic_builders_reject_integrations_outside_pinned_grammar(
    integration: str,
) -> None:
    with pytest.raises(ValidationError, match="pinned"):
        build_entity_topic(integration, ENTITY_ID, EntityCategory.TRACK)


@pytest.mark.parametrize(
    "topic",
    [
        f"entity/vendor-a/{str(LETTERED_ENTITY_ID).upper()}/track",
        f"entity_location/vendor-a/{str(LETTERED_ENTITY_ID).upper()}",
        f"entity_location_pb/vendor-a/{str(LETTERED_ENTITY_ID).upper()}",
        f"task/vendor-a/{str(LETTERED_ENTITY_ID).upper()}/slew",
        f"task/vendor-a/{str(LETTERED_ENTITY_ID).upper()}/slew/response",
    ],
)
def test_topic_parsers_reject_noncanonical_uuid_text(topic: str) -> None:
    with pytest.raises(ProtocolError):
        parse_protocol_topic(topic)


@pytest.mark.parametrize(
    "topic_filter",
    [
        ENTITY_JSON_SUBSCRIPTION,
        ENTITY_PROTOBUF_SUBSCRIPTION,
        LOCATION_JSON_SUBSCRIPTION,
        LOCATION_PROTOBUF_SUBSCRIPTION,
        f"task/vendor-a/{ENTITY_ID}/slew",
        f"task/vendor-a/{ENTITY_ID}/slew/response",
    ],
)
def test_only_fixed_subscriptions_are_allowed(topic_filter: str) -> None:
    assert validate_subscription_filter(topic_filter) == topic_filter
    assert "#" not in topic_filter


def test_lazy_subscription_builders_use_narrowest_caller_filters() -> None:
    assert build_entity_subscription_filters(
        {EntityCategory.TRACK, EntityCategory.DETECTION},
        {"vendor-a"},
    ) == (
        "entity/vendor-a/+/detection",
        "entity/vendor-a/+/track",
        "entity_pb/vendor-a/detection",
        "entity_pb/vendor-a/track",
    )
    assert build_location_subscription_filters({ENTITY_ID}, {"vendor-a"}) == (
        f"entity_location/vendor-a/{ENTITY_ID}",
        f"entity_location_pb/vendor-a/{ENTITY_ID}",
    )
    assert build_entity_subscription_filters(set(), set()) == (
        ENTITY_JSON_SUBSCRIPTION,
        ENTITY_PROTOBUF_SUBSCRIPTION,
    )
    assert build_location_subscription_filters(set(), set()) == (
        LOCATION_JSON_SUBSCRIPTION,
        LOCATION_PROTOBUF_SUBSCRIPTION,
    )
    assert build_location_subscription_filters(set(), {"terminal-geolocation"}) == (
        "entity_location/terminal-geolocation/+",
        "entity_location_pb/terminal-geolocation/+",
    )
    assert build_entity_subscription_filters({EntityCategory.GEOMETRIC}, {"vendor-a"}) == (
        "entity/vendor-a/+/geometric",
        "entity_pb/vendor-a/geometric",
    )
    assert build_entity_subscription_filters({EntityCategory.OTHER}, {"vendor-a"}) == (
        "entity/vendor-a/+/+",
        "entity_pb/vendor-a/+",
    )


def test_protobuf_geometric_topic_is_supported_without_a_numeric_category_field() -> None:
    assert (
        build_entity_protobuf_topic("vendor-a", EntityCategory.GEOMETRIC)
        == "entity_pb/vendor-a/geometric"
    )
    with pytest.raises(ValidationError, match="not publishable"):
        build_entity_protobuf_topic("vendor-a", EntityCategory.OTHER)


@pytest.mark.parametrize(
    "topic_filter",
    [
        "#",
        "+/entity/#",
        "entity/#",
        "entity/+/+/#",
        "task/#",
        "task/vendor-a/#",
        "private/entity/#",
        "administration/#",
    ],
)
def test_broad_or_internal_subscriptions_are_rejected(topic_filter: str) -> None:
    with pytest.raises(ValidationError):
        validate_subscription_filter(topic_filter)


@pytest.mark.parametrize(
    "topic_filter",
    [
        "entity/geolocation/+/track",
        "entity/a/+/track",
        "entity/vendor.with.dot/+/track",
        "entity_pb/geolocation/track",
        f"entity_location/a/{ENTITY_ID}",
        f"entity_location_pb/vendor.with.dot/{ENTITY_ID}",
    ],
)
def test_subscription_filters_reject_literal_integrations_outside_pinned_grammar(
    topic_filter: str,
) -> None:
    with pytest.raises(ValidationError):
        validate_subscription_filter(topic_filter)


@pytest.mark.parametrize(
    "topic",
    [
        "entity/vendor-a/not-a-uuid/track",
        f"entity/vendor/a/{ENTITY_ID}/track",
        f"task/vendor-a/{ENTITY_ID}/bad/command",
        f"peer/entity/vendor-a/{ENTITY_ID}/track",
        "peer/task/response/vendor-a/slew",
    ],
)
def test_noncanonical_or_unretained_topics_are_rejected(topic: str) -> None:
    with pytest.raises(ProtocolError):
        parse_protocol_topic(topic)
    with pytest.raises(ValidationError):
        validate_publish_topic(topic)


def test_builders_reject_mqtt_injection() -> None:
    with pytest.raises(ValidationError):
        build_task_request_topic("vendor/+", ENTITY_ID, "slew")
    with pytest.raises(ValidationError):
        build_task_request_topic("vendor", ENTITY_ID, "slew/#")


@pytest.mark.parametrize(
    ("canary", "marker", "distinctive_substrings"),
    _ADVERSARIAL_TOPIC_SEGMENTS,
)
def test_hostile_topics_never_reach_public_exception_surfaces(
    canary: str,
    marker: str,
    distinctive_substrings: tuple[str, ...],
) -> None:
    routed_terminal = uuid4()
    topics = (
        (canary, UNSUPPORTED_TOPIC_FAMILY),
        (f"{canary}/x/y/z", UNSUPPORTED_TOPIC_FAMILY),
        (f"entity/{canary}/x/y", TopicFamily.ENTITY_JSON.value),
        (f"entity_pb/{canary}/x", TopicFamily.ENTITY_PROTOBUF.value),
        (f"entity_location/{canary}/x", TopicFamily.LOCATION_JSON.value),
        (
            f"entity_location_pb/{canary}/x",
            TopicFamily.LOCATION_PROTOBUF.value,
        ),
        (f"task/{canary}/x/y", TopicFamily.TASK.value),
        (f"{routed_terminal}/task/{canary}/x/y", TopicFamily.TASK.value),
    )
    parsers: tuple[Callable[[str], Any], ...] = (
        parse_protocol_topic,
        parse_entity_topic,
        parse_location_topic,
        parse_task_topic,
    )
    validators = (validate_publish_topic, validate_subscription_filter)

    for topic, expected_family in topics:
        for parser in parsers:
            with pytest.raises(ProtocolError) as caught:
                parser(topic)
            error = caught.value
            _assert_no_leak(error, canary, marker, distinctive_substrings)
            assert error.details["topic_family"] == expected_family
            assert error.details["topic_family"] in _TOPIC_FAMILY_LABELS
            assert error.code == "protocol_error"
            assert error.operation == "parse_topic"

        for validator in validators:
            with pytest.raises(ValidationError) as caught:
                validator(topic)
            _assert_no_leak(
                caught.value,
                canary,
                marker,
                distinctive_substrings,
            )


def test_topic_family_classification_retains_safe_protocol_context() -> None:
    routed_terminal = uuid4()
    malformed_topics = (
        ("entity/bad!/x/y", TopicFamily.ENTITY_JSON.value),
        ("entity_pb/bad!/x", TopicFamily.ENTITY_PROTOBUF.value),
        ("entity_location/bad!/x", TopicFamily.LOCATION_JSON.value),
        ("entity_location_pb/bad!/x", TopicFamily.LOCATION_PROTOBUF.value),
        ("task/bad!/x/y", TopicFamily.TASK.value),
        (f"{routed_terminal}/task/bad!/x/y", TopicFamily.TASK.value),
        ("unknown/bad!/x/y", UNSUPPORTED_TOPIC_FAMILY),
        ("", UNSUPPORTED_TOPIC_FAMILY),
        ("entityX/bad!/x/y", UNSUPPORTED_TOPIC_FAMILY),
        ("ENTITY/bad!/x/y", UNSUPPORTED_TOPIC_FAMILY),
        (f"{routed_terminal}/tasks/bad!/x/y", UNSUPPORTED_TOPIC_FAMILY),
        ("not-a-uuid/task/bad!/x/y", UNSUPPORTED_TOPIC_FAMILY),
    )

    for topic, expected_family in malformed_topics:
        with pytest.raises(ProtocolError) as caught:
            parse_protocol_topic(topic)
        assert caught.value.details["topic_family"] == expected_family


def test_unsupported_topic_classification_is_never_an_input_slice() -> None:
    topic = "A9z_" * 48 + _OPAQUE_MARKER + "/x/y/z"

    with pytest.raises(ProtocolError) as caught:
        parse_protocol_topic(topic)

    family = caught.value.details["topic_family"]
    assert family in _TOPIC_FAMILY_LABELS
    assert family in _SUPPORTED_TOPIC_FAMILIES or family not in topic
