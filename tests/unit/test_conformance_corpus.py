# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from picogrid_ecn_client._protocol.json_codec import decode_json, encode_json
from picogrid_ecn_client._protocol.topics import (
    build_entity_topic,
    build_location_topic,
    build_task_request_topic,
    build_task_response_topic,
    parse_entity_topic,
    parse_location_topic,
    parse_task_topic,
)
from picogrid_ecn_client.exceptions import ProtocolError
from picogrid_ecn_client.models.common import DeliveryPolicy
from picogrid_ecn_client.streams import EventStream

_FIXTURES = Path(__file__).parents[1] / "fixtures"
_MANIFEST_PATH = _FIXTURES / "conformance" / "manifest.json"
_MAX_PAYLOAD_SIZE = 1 << 20


def _manifest() -> dict[str, object]:
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def _vectors(name: str) -> list[dict[str, object]]:
    document = json.loads((_FIXTURES / "conformance" / name).read_text(encoding="utf-8"))
    return document["vectors"]


def test_conformance_manifest_file_hashes_match() -> None:
    files = _manifest()["files"]
    assert isinstance(files, dict)
    for relative, metadata in files.items():
        path = _FIXTURES / relative
        assert path.is_file(), relative
        assert isinstance(metadata, dict)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == metadata["sha256"]


def test_conformance_manifest_covers_exact_corpus() -> None:
    files = _manifest()["files"]
    assert isinstance(files, dict)
    corpus = {
        path.relative_to(_FIXTURES).as_posix()
        for directory in (_FIXTURES / "protocol", _FIXTURES / "conformance")
        for path in directory.rglob("*")
        if path.is_file() and path != _MANIFEST_PATH
    }
    corpus.add("wgs84_ecef.json")
    assert corpus == set(files)


def test_conformance_canonical_encoding_hashes_match() -> None:
    files = _manifest()["files"]
    assert isinstance(files, dict)
    for relative, metadata in files.items():
        assert isinstance(metadata, dict)
        expected = metadata.get("canonical_sha256")
        if expected is None:
            continue
        payload = (_FIXTURES / relative).read_bytes()
        encoded = encode_json(decode_json(payload, _MAX_PAYLOAD_SIZE), _MAX_PAYLOAD_SIZE)
        assert hashlib.sha256(encoded).hexdigest() == expected


def test_conformance_malformed_payload_is_rejected() -> None:
    payload = (_FIXTURES / "protocol" / "malformed_payload.json").read_bytes()
    with pytest.raises(ProtocolError):
        decode_json(payload, _MAX_PAYLOAD_SIZE)


def test_conformance_topic_grammar_vectors() -> None:
    for vector in _vectors("topic_grammar.json"):
        kind = vector["kind"]
        integration = vector["integration"]
        entity_id = UUID(vector["entity_id"])
        expected_topic = vector["topic"]

        if kind == "entity_event":
            topic = build_entity_topic(integration, entity_id, vector["category"])
            parsed = parse_entity_topic(topic)
            assert parsed.integration == integration
            assert parsed.entity_id == entity_id
            assert parsed.suffix == vector["category"].lower()
            assert parsed.protobuf is False
        elif kind == "location":
            topic = build_location_topic(integration, entity_id)
            parsed = parse_location_topic(topic)
            assert parsed.integration == integration
            assert parsed.entity_id == entity_id
            assert parsed.protobuf is False
        else:
            command = vector["command"]
            if kind == "task_request":
                topic = build_task_request_topic(integration, entity_id, command)
            else:
                assert kind == "task_response"
                topic = build_task_response_topic(integration, entity_id, command)
            parsed = parse_task_topic(topic)
            assert parsed.integration == integration
            assert parsed.entity_id == entity_id
            assert parsed.command == command
            assert parsed.response is (kind == "task_response")
            assert parsed.route_terminal_id is None

        assert topic == expected_topic


@pytest.mark.asyncio
async def test_conformance_drop_policy_vectors() -> None:
    for vector in _vectors("drop_policy.json"):
        stream = EventStream[str](
            buffer_size=vector["buffer_size"],
            delivery_policy=DeliveryPolicy(vector["delivery_policy"]),
        )
        delivered = []
        for step in vector["steps"]:
            if step["op"] == "publish":
                for event_id in step["events"]:
                    stream._put_nowait(event_id)
            else:
                assert step["op"] == "consume"
                for _ in range(step["count"]):
                    delivered.append(await anext(stream))
        while stream._queue.qsize():
            delivered.append(await anext(stream))

        expected = vector["expected"]
        assert delivered == expected["delivered"], vector["id"]
        assert stream.dropped_count == expected["dropped"], vector["id"]
