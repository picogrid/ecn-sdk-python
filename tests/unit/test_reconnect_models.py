# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from picogrid_ecn_client import (
    ClientState,
    ConnectionFailureCode,
    ConnectionFailureOperation,
    ConnectionRetryState,
    ConnectionStatus,
)

NOW = datetime(2026, 8, 9, 14, 0, tzinfo=UTC)


def test_connection_status_accepts_redacted_retry_diagnostics() -> None:
    status = ConnectionStatus(
        state=ClientState.RECONNECTING,
        ready=False,
        mqtt_connected=False,
        changed_at=NOW,
        connection_generation=2,
        consecutive_attempt_count=3,
        last_connected_at=NOW,
        last_disconnected_at=NOW,
        next_retry_at=NOW,
        last_failure_code=ConnectionFailureCode.BROKER_UNAVAILABLE,
        last_failure_operation=ConnectionFailureOperation.CONNECT,
        retry_state=ConnectionRetryState.SCHEDULED,
    )

    assert status.connection_generation == 2
    assert status.next_retry_at == NOW


def test_connection_status_accepts_timed_credential_backoff() -> None:
    status = ConnectionStatus(
        state=ClientState.RECONNECTING,
        ready=False,
        mqtt_connected=False,
        changed_at=NOW,
        next_retry_at=NOW,
        last_failure_code=ConnectionFailureCode.AUTHENTICATION_REJECTED,
        last_failure_operation=ConnectionFailureOperation.CONNECT,
        retry_state=ConnectionRetryState.WAITING_FOR_CREDENTIALS,
    )

    assert status.retry_state is ConnectionRetryState.WAITING_FOR_CREDENTIALS
    assert status.next_retry_at == NOW


def test_tls_peer_verification_has_a_distinct_secret_safe_code() -> None:
    assert (
        ConnectionFailureCode.TLS_PEER_VERIFICATION_FAILED.value == "tls_peer_verification_failed"
    )


@pytest.mark.parametrize(
    "values",
    [
        {
            "state": ClientState.READY,
            "ready": True,
            "mqtt_connected": True,
            "retry_state": ConnectionRetryState.CONNECTING,
        },
        {
            "state": ClientState.RECONNECTING,
            "ready": False,
            "mqtt_connected": False,
            "retry_state": ConnectionRetryState.SCHEDULED,
        },
        {
            "state": ClientState.READY,
            "ready": False,
            "mqtt_connected": True,
            "next_retry_at": NOW,
            "retry_state": ConnectionRetryState.INACTIVE,
        },
        {
            "state": ClientState.FAILED,
            "ready": False,
            "mqtt_connected": False,
            "retry_state": ConnectionRetryState.INACTIVE,
        },
    ],
)
def test_connection_status_rejects_incoherent_retry_state(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ConnectionStatus.model_validate({"changed_at": NOW} | values)
