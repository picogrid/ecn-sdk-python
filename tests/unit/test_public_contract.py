# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError

import picogrid_ecn_client as public
from picogrid_ecn_client._entity_locations import EntityLocationService, RestoreFailureCallback
from picogrid_ecn_client._redaction import REDACTED, redact_text

_MessageCallback = Callable[[str, bytes], Awaitable[None]]


class _FakeEntityTransport:
    def __init__(self) -> None:
        self.callbacks: dict[object, tuple[str, _MessageCallback]] = {}

    async def subscribe(
        self,
        topic_filter: str,
        callback: _MessageCallback,
        *,
        on_restore_failure: RestoreFailureCallback | None = None,
    ) -> object:
        del on_restore_failure
        handle = object()
        self.callbacks[handle] = (topic_filter, callback)
        return handle

    async def unsubscribe(self, handle: object) -> None:
        self.callbacks.pop(handle, None)

    async def publish(self, topic: str, payload: bytes, qos: int) -> None:
        del topic, payload, qos

    async def deliver_entity(self, topic: str, payload: bytes) -> None:
        callbacks = {
            callback
            for topic_filter, callback in self.callbacks.values()
            if topic_filter.startswith("entity/")
        }
        assert callbacks
        for callback in callbacks:
            await callback(topic, payload)


def test_public_api_snapshot() -> None:
    snapshot = Path(__file__).parents[1] / "fixtures" / "public_api.txt"
    with snapshot.open(encoding="utf-8") as snapshot_file:
        expected = tuple(line.strip() for line in snapshot_file if line.strip())
    assert tuple(sorted(public.__all__)) == expected


def test_public_json_schema_snapshot() -> None:
    snapshot = Path(__file__).parents[1] / "fixtures" / "schema_hashes.json"
    expected = json.loads(snapshot.read_text(encoding="utf-8"))
    actual = {}
    for name in public.__all__:
        schema = getattr(getattr(public, name), "model_json_schema", None)
        if callable(schema):
            encoded = json.dumps(schema(), sort_keys=True, separators=(",", ":")).encode()
            actual[name] = hashlib.sha256(encoded).hexdigest()
    assert actual == expected


def test_no_raw_protocol_escape_hatches() -> None:
    prohibited = {
        "publish",
        "raw_http_client",
        "raw_mqtt_client",
        "request",
        "subscribe",
    }
    assert prohibited.isdisjoint(vars(public.ECNClient))


def test_timestamps_require_timezone_and_normalize_to_utc() -> None:
    with pytest.raises(PydanticValidationError, match="timezone"):
        public.Location(
            latitude=0,
            longitude=0,
            recorded_at=datetime(2026, 1, 1),
        )

    offset = timezone(-timedelta(hours=5))
    location = public.Location(
        latitude=1,
        longitude=2,
        recorded_at=datetime(2026, 1, 1, tzinfo=offset),
    )
    assert location.recorded_at.tzinfo is UTC


def test_entity_identity_uses_uuid() -> None:
    entity_id = uuid4()
    entity = public.Entity(
        id=entity_id,
        category=public.EntityCategory.TRACK,
        integration="synthetic-radar",
        recorded_at=datetime.now(UTC),
        type="synthetic_track",
    )
    assert entity.identity.id == entity_id


def test_public_models_reject_unknown_fields() -> None:
    with pytest.raises(PydanticValidationError, match="Extra inputs"):
        public.Location(
            latitude=0,
            longitude=0,
            recorded_at=datetime.now(UTC),
            hidden_contract=True,
        )


def test_redaction_removes_common_secret_forms() -> None:
    jwt = "abcdefgh.ijklmnop.qrstuvwx"
    pem = "-----BEGIN " + "PRIVATE KEY-----\nnot-real\n-----END PRIVATE KEY-----"
    authorization = "Authorization" + ": Bearer "
    credential_uri = "https://" + "user:" + "pass@" + "example.invalid"
    text = redact_text(f"{authorization}{jwt}; private_key={pem}; {credential_uri}")
    assert REDACTED in text
    assert jwt not in text
    assert "not-real" not in text
    assert "user:pass" not in text


def test_exception_details_are_redacted() -> None:
    secret = "example-secret-value"
    error = public.AuthenticationError(
        f"password={secret}",
        details={"authorization": f"Bearer {secret}"},
        secrets=(secret,),
    )
    assert secret not in str(error)
    assert secret not in error.details["authorization"]


@pytest.mark.asyncio
async def test_adversarial_inbound_topic_text_never_reaches_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = _FakeEntityTransport()
    service = EntityLocationService(
        transport,
        integration_name="demo",
        default_buffer_size=1,
        maximum_payload_size=1024,
        decode_entity=lambda topic, payload: None,  # type: ignore[arg-type]
        decode_location=lambda topic, payload: None,  # type: ignore[arg-type]
        encode_entity=lambda entity: ("unused", b"", 0),
        encode_location=lambda entity_id, integration, location: ("unused", b"", 0),
    )
    stream = await service.watch_entities(
        categories=frozenset({public.EntityCategory.TRACK}),
        integrations=frozenset({"demo"}),
        buffer_size=1,
        delivery_policy=public.DeliveryPolicy.FIFO,
    )
    marker = "canary11marker"
    canary = "raw-log-" + marker + "-credential"
    topic = f"entity/demo/{canary}/track"
    caplog.set_level(logging.DEBUG, logger="picogrid_ecn_client")

    await transport.deliver_entity(topic, b"{}")

    records = [record for record in caplog.records if record.name.startswith("picogrid_ecn_client")]
    assert records
    assert stream.decode_error_count == 1
    for record in records:
        assert canary not in record.getMessage()
        assert marker not in record.getMessage()
        assert canary not in repr(record.args)
        assert marker not in repr(record.args)
    await stream.aclose()


def test_supplied_secrets_are_redacted_across_exception_fields() -> None:
    message_secret = "message-" + "canary12marker" + "-credential"
    details_secret = "details-" + "canary13marker" + "-credential"
    error = public.ECNClientError(
        f"operation failed for {message_secret}",
        details={"context": f"remote value {details_secret}"},
        secrets=(message_secret, details_secret),
    )

    rendered = (error.message, str(error), repr(error), repr(error.details))
    for secret in (message_secret, details_secret):
        assert all(secret not in surface for surface in rendered)
    assert REDACTED in error.message
    assert REDACTED in error.details["context"]
