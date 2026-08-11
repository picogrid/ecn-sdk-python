# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""MQTT preflight, connection, and operation receipt models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from ._base import IntegrationName, PublicModel, utc_datetime
from .common import (
    CheckStatus,
    ClientState,
    ConnectionFailureCode,
    ConnectionFailureOperation,
    ConnectionRetryState,
    DeliveryPhase,
    EntityCategory,
    PreflightCheckName,
    PublicationKind,
    WireFormat,
)


class SubscriptionProbeKind(StrEnum):
    """Identify the topic family for a subscription authorization probe.

    ``ENTITY`` probes one bounded entity topic filter, while ``LOCATION`` probes one
    bounded geolocation topic filter.
    """

    ENTITY = "entity"
    LOCATION = "location"


class SubscriptionProbe(PublicModel):
    """Describe one caller-requested MQTT subscription authorization probe.

    The selected fields must form one bounded entity or geolocation topic filter
    before network use.
    """

    kind: SubscriptionProbeKind = Field(description="Topic family to probe: entity or geolocation.")
    integration: IntegrationName = Field(
        description="Exact integration topic segment included in the bounded probe."
    )
    wire_format: WireFormat = Field(
        default=WireFormat.JSON,
        description="Payload encoding whose topic shape is probed; defaults to JSON.",
    )
    entity_id: UUID | None = Field(
        default=None,
        description=(
            "Exact entity UUID; required for location, optional for JSON entity, and "
            "forbidden for protobuf entity probes."
        ),
    )
    category: EntityCategory | None = Field(
        default=None,
        description=(
            "Publishable entity category; required for entity probes and forbidden "
            "for location probes."
        ),
    )

    @model_validator(mode="after")
    def validate_shape(self) -> SubscriptionProbe:
        if self.kind is SubscriptionProbeKind.ENTITY:
            if self.category is None:
                raise ValueError("entity subscription probes require category")
            if self.category is EntityCategory.OTHER:
                raise ValueError("entity subscription probes require a publishable category")
            if self.wire_format is WireFormat.PROTOBUF and self.entity_id is not None:
                raise ValueError("protobuf entity topics do not contain an entity ID")
        elif self.entity_id is None:
            raise ValueError("location subscription probes require entity_id")
        elif self.category is not None:
            raise ValueError("location subscription probes do not use category")
        return self


class ConnectionStatus(PublicModel):
    """Summarize the client's lifecycle and MQTT connection state.

    A true ``ready`` value is valid only with the ``READY`` lifecycle state, an
    active MQTT connection, and restoration of every still-required subscription.
    """

    state: ClientState = Field(description="Current client lifecycle state.")
    ready: bool = Field(description="Whether the client is in READY state with MQTT connected.")
    mqtt_connected: bool = Field(
        description="Whether the client currently has an MQTT v5 connection."
    )
    changed_at: datetime = Field(
        description="Timezone-aware UTC time when this status snapshot last changed."
    )
    connection_generation: Annotated[
        int,
        Field(
            ge=0,
            description=("Number of connections that reached strict readiness, starting at zero."),
        ),
    ] = 0
    consecutive_attempt_count: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Attempts in the current recovery episode; reset after stable readiness "
                "or a deliberate new episode."
            ),
        ),
    ] = 0
    last_connected_at: datetime | None = Field(
        default=None,
        description="UTC time when the most recent MQTT v5 CONNACK succeeded.",
    )
    last_disconnected_at: datetime | None = Field(
        default=None,
        description="UTC time when the most recent MQTT transport connection ended.",
    )
    next_retry_at: datetime | None = Field(
        default=None,
        description="UTC estimate for the next scheduled retry, when one is scheduled.",
    )
    last_failure_code: ConnectionFailureCode | None = Field(
        default=None,
        description="Stable secret-safe classification of the latest recovery failure.",
    )
    last_failure_operation: ConnectionFailureOperation | None = Field(
        default=None,
        description="Stable connection phase associated with the latest recovery failure.",
    )
    retry_state: ConnectionRetryState = Field(
        default=ConnectionRetryState.INACTIVE,
        description="Current state of the sole reconnect supervisor.",
    )

    _normalize_changed_at = field_validator("changed_at")(utc_datetime)

    @field_validator("last_connected_at", "last_disconnected_at", "next_retry_at")
    @classmethod
    def normalize_optional_timestamp(cls, value: datetime | None) -> datetime | None:
        """Normalize an optional status timestamp to UTC."""

        return None if value is None else utc_datetime(value)

    @model_validator(mode="after")
    def validate_ready_state(self) -> ConnectionStatus:
        if self.ready and (self.state is not ClientState.READY or not self.mqtt_connected):
            raise ValueError("ready status requires READY state and MQTT connectivity")
        if self.ready and self.retry_state is not ConnectionRetryState.INACTIVE:
            raise ValueError("ready status requires an inactive retry state")
        timed_retry_states = {
            ConnectionRetryState.SCHEDULED,
            ConnectionRetryState.WAITING_FOR_CREDENTIALS,
        }
        if self.retry_state in timed_retry_states and self.next_retry_at is None:
            raise ValueError("timed retry status requires next_retry_at")
        if self.retry_state not in timed_retry_states and self.next_retry_at is not None:
            raise ValueError("next_retry_at requires a timed retry status")
        if (self.state is ClientState.FAILED) != (
            self.retry_state is ConnectionRetryState.TERMINAL
        ):
            raise ValueError("FAILED client state and terminal retry state must agree")
        return self


class PreflightCheck(PublicModel):
    """Report the outcome and duration of one MQTT preflight check.

    The detail is a bounded reader-facing explanation of the observed outcome.
    """

    name: PreflightCheckName = Field(description="Operation evaluated by this check.")
    status: CheckStatus = Field(description="Observed outcome of the check.")
    required: bool = Field(
        default=True,
        description="Whether failure of this check prevents a successful report; defaults to true.",
    )
    duration_ms: Annotated[
        float,
        Field(
            ge=0,
            allow_inf_nan=False,
            description="Non-negative elapsed duration in milliseconds.",
        ),
    ]
    detail: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1024,
            description="Reader-facing outcome detail, from 1 through 1024 characters.",
        ),
    ]


class PreflightReport(PublicModel):
    """Collect the checks and readiness observed by one preflight run.

    ``successful`` summarizes required checks, while ``ready`` records the client
    readiness observed for the report.
    """

    generated_at: datetime = Field(
        description="Timezone-aware UTC time when the report was generated."
    )
    successful: bool = Field(description="Whether every required preflight check passed.")
    ready: bool = Field(description="Whether the client was ready for this report.")
    checks: tuple[PreflightCheck, ...] = Field(
        description="Ordered check results included in the report."
    )

    _normalize_generated_at = field_validator("generated_at")(utc_datetime)


class PublicationReceipt(PublicModel):
    """Confirm the strongest safe MQTT delivery fact for one publication.

    This receipt does not assert downstream processing.
    """

    operation_id: UUID = Field(
        description="Client-generated UUID correlating the local publication operation."
    )
    kind: PublicationKind = Field(
        description="Whether the completed publication carried an entity or location."
    )
    entity_id: UUID = Field(description="UUID of the published entity.")
    qos: Annotated[
        Literal[0, 1],
        Field(description="MQTT QoS used for this publication: zero or one."),
    ]
    delivery_phase: DeliveryPhase = Field(
        description="Strongest safe delivery fact established before returning the receipt."
    )
    accepted_at: datetime = Field(
        description=("UTC local-send completion time for QoS 0 or PUBACK receipt time for QoS 1.")
    )

    _normalize_accepted_at = field_validator("accepted_at")(utc_datetime)

    @model_validator(mode="after")
    def validate_delivery_phase(self) -> PublicationReceipt:
        """Keep receipt semantics honest for the delivered MQTT QoS."""

        expected = (
            DeliveryPhase.LOCAL_SEND_COMPLETED if self.qos == 0 else DeliveryPhase.BROKER_ACCEPTED
        )
        if self.delivery_phase is not expected:
            raise ValueError("publication receipt phase does not agree with QoS")
        return self


class DispatchReceipt(PublicModel):
    """Confirm broker acceptance of a fire-and-forget task dispatch.

    This receipt does not assert task execution or downstream processing.
    """

    task_id: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            description="Task correlation identifier, from 1 through 128 characters.",
        ),
    ]
    qos: Literal[1] = Field(
        default=1,
        description="MQTT QoS used for fire-and-forget task publication; always one.",
    )
    delivery_phase: DeliveryPhase = Field(
        default=DeliveryPhase.BROKER_ACCEPTED,
        description="Broker-accepted delivery phase established by PUBACK.",
    )
    accepted_at: datetime = Field(
        description=(
            "Timezone-aware UTC client-clock observation recorded after the task publish "
            "completed with a non-failure PUBACK."
        )
    )

    _normalize_accepted_at = field_validator("accepted_at")(utc_datetime)

    @model_validator(mode="after")
    def validate_delivery_phase(self) -> DispatchReceipt:
        """Require a fire-and-forget receipt to represent broker acceptance."""

        if self.delivery_phase is not DeliveryPhase.BROKER_ACCEPTED:
            raise ValueError("dispatch receipt requires broker acceptance")
        return self
