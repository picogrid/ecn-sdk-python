# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from picogrid_ecn_client import EntityCategory, TaskStatus
from picogrid_ecn_client._protocol import (
    decode_entity_json,
    decode_json,
    decode_location_json,
)
from picogrid_ecn_client.exceptions import ProtocolError

FIXTURES = Path(__file__).parents[1] / "fixtures" / "protocol"
MAXIMUM_SIZE = 64 * 1024
ENTITY_ID_PREFIX = "10000000-0000-4000-8000-"


def _payload(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.mark.contract
@pytest.mark.parametrize(
    ("name", "entity_id", "suffix", "category"),
    [
        (
            "track_event.json",
            f"{ENTITY_ID_PREFIX}000000000001",
            "track",
            EntityCategory.TRACK,
        ),
        (
            "detection_event.json",
            f"{ENTITY_ID_PREFIX}000000000002",
            "detection",
            EntityCategory.DETECTION,
        ),
    ],
)
def test_synthetic_entity_fixtures_decode(
    name: str,
    entity_id: str,
    suffix: str,
    category: EntityCategory,
) -> None:
    event = decode_entity_json(
        f"entity/synthetic-vendor/{entity_id}/{suffix}",
        _payload(name),
        MAXIMUM_SIZE,
    )
    assert event.entity.category is category
    assert event.entity.integration == "synthetic-vendor"
    assert str(event.entity.id) == entity_id


@pytest.mark.contract
def test_synthetic_location_fixture_decodes_motion() -> None:
    event = decode_location_json(
        f"entity_location/synthetic-vendor/{ENTITY_ID_PREFIX}000000000003",
        _payload("location_update.json"),
        MAXIMUM_SIZE,
    )
    assert event.location.angular_velocity is not None
    assert event.location.angular_velocity.yaw == pytest.approx(0.3)


@pytest.mark.contract
def test_synthetic_task_fixtures_have_bounded_correlated_envelopes() -> None:
    request = decode_json(_payload("task_request.json"), MAXIMUM_SIZE)
    acknowledgment = decode_json(_payload("task_acknowledgment.json"), MAXIMUM_SIZE)
    success = decode_json(_payload("task_success.json"), MAXIMUM_SIZE)
    failure = decode_json(_payload("task_failure.json"), MAXIMUM_SIZE)

    assert request == {
        "source": "local",
        "task_id": "synthetic-task-001",
        "_response_mode": "complete",
        "payload": {"value": 21},
    }
    assert acknowledgment == {
        "status": TaskStatus.SUCCESS.value,
        "source": "local",
        "task_id": "synthetic-task-002",
        "payload": {"ack": True, "message": "Task started"},
        "_response_type": "ack",
    }
    assert success == {
        "status": TaskStatus.SUCCESS.value,
        "source": "local",
        "task_id": request["task_id"],
        "payload": {"doubled": 42},
        "_response_type": "full",
    }
    assert failure == {
        "status": TaskStatus.FAILED.value,
        "source": "local",
        "task_id": "synthetic-task-003",
        "payload": {"retryable": False},
        "error_message": "synthetic task failed",
        "_response_type": "full",
    }


@pytest.mark.contract
def test_malformed_fixture_is_rejected() -> None:
    with pytest.raises(ProtocolError):
        decode_json(_payload("malformed_payload.json"), MAXIMUM_SIZE)


def test_fixture_set_is_complete_and_synthetic() -> None:
    expected = {
        "detection_event.json",
        "location_update.json",
        "malformed_payload.json",
        "task_acknowledgment.json",
        "task_failure.json",
        "task_request.json",
        "task_success.json",
        "track_event.json",
    }
    assert {path.name for path in FIXTURES.iterdir()} == expected
    joined = b"\n".join(path.read_bytes() for path in FIXTURES.iterdir())
    assert b"synthetic" in joined
    for prohibited in (b"picogrid_edge_sdk", b"BEGIN PRIVATE KEY", b"Authorization: Bearer"):
        assert prohibited not in joined
