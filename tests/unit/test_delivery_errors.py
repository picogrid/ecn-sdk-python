# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from uuid import UUID

import pytest

from picogrid_ecn_client.exceptions import (
    ConnectionError,
    DeliveryError,
    OutcomeUnknownError,
)
from picogrid_ecn_client.models import DeliveryPhase


def test_delivery_error_is_not_a_connection_error() -> None:
    operation_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    error = DeliveryError(
        "publication did not start",
        delivery_phase=DeliveryPhase.NOT_SENT,
        operation="entities.publish",
        operation_id=operation_id,
    )

    assert isinstance(error, DeliveryError)
    assert not isinstance(error, ConnectionError)
    assert error.operation_id == operation_id
    assert error.task_id is None
    assert error.details == {
        "delivery_phase": "not_sent",
        "operation_id": str(operation_id),
    }


@pytest.mark.parametrize(
    "phase",
    [
        DeliveryPhase.LOCAL_SEND_UNCERTAIN,
        DeliveryPhase.BROKER_ACKNOWLEDGMENT_PENDING,
        DeliveryPhase.RESPONSE_PENDING,
    ],
)
def test_outcome_unknown_accepts_only_uncertain_phases(phase: DeliveryPhase) -> None:
    error = OutcomeUnknownError(
        "task outcome is unknown",
        delivery_phase=phase,
        operation="tasks.send",
        task_id="task-safe-id",
    )

    assert error.delivery_phase is phase
    assert error.task_id == "task-safe-id"
    assert error.details["task_id"] == "task-safe-id"


@pytest.mark.parametrize(
    "phase",
    [
        DeliveryPhase.NOT_SENT,
        DeliveryPhase.LOCAL_SEND_COMPLETED,
        DeliveryPhase.BROKER_ACCEPTED,
        DeliveryPhase.COMPLETED,
    ],
)
def test_outcome_unknown_rejects_definite_phases(phase: DeliveryPhase) -> None:
    with pytest.raises(ValueError, match="uncertain delivery phase"):
        OutcomeUnknownError(
            "invalid phase",
            delivery_phase=phase,
            operation="tasks.send",
        )


def test_delivery_error_redacts_message_and_details() -> None:
    secret = "delivery-secret-canary"
    error = OutcomeUnknownError(
        f"response lost for {secret}",
        delivery_phase=DeliveryPhase.RESPONSE_PENDING,
        operation="tasks.send",
        task_id="task-safe-id",
        details={"unsafe": secret},
        secrets=(secret,),
    )

    rendered = f"{error!r} {error} {error.details}"
    assert secret not in rendered
    assert "[REDACTED]" in rendered
