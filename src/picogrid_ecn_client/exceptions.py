# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Unified public exception hierarchy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

from ._redaction import redact_text
from .models.common import DeliveryPhase

if TYPE_CHECKING:
    from .models.clock import ClockReport


class ECNClientError(Exception):
    """Base exception for all failures crossing the public client boundary."""

    default_code: ClassVar[str] = "ecn_client_error"
    """Stable fallback code used when an instance does not provide one."""

    def __init__(
        self,
        message: object,
        *,
        code: str | None = None,
        operation: str | None = None,
        status_code: int | None = None,
        details: Mapping[str, object] | None = None,
        secrets: tuple[str, ...] = (),
    ) -> None:
        """Initialize a redacted public client error.

        Args:
            message: Failure message to redact and expose.
            code: Optional stable error code overriding :attr:`default_code`.
            operation: Optional client operation that failed.
            status_code: Optional status value associated with the failure.
            details: Optional detail values copied and redacted into strings.
            secrets: Additional exact values to remove from public text.
        """

        self.message: str
        """Redacted human-readable failure message."""
        self.code: str
        """Stable machine-readable failure code."""
        self.operation: str | None
        """Client operation that failed, when identified."""
        self.status_code: int | None
        """Status value associated with the failure, when available."""
        self.details: dict[str, str]
        """Secret-safe string details associated with the failure."""
        self.message = redact_text(message, secrets=secrets)
        self.code = code or self.default_code
        self.operation = operation
        self.status_code = status_code
        self.details = {
            key: redact_text(value, secrets=secrets) for key, value in (details or {}).items()
        }
        super().__init__(self.message)


class ConfigurationError(ECNClientError):
    """Raised when client configuration or profile input is invalid."""

    default_code: ClassVar[str] = "configuration_error"


class AuthenticationError(ECNClientError):
    """Raised when MQTT credentials cannot be resolved or accepted."""

    default_code: ClassVar[str] = "authentication_error"


class AuthorizationError(ECNClientError):
    """Raised when the MQTT broker denies an operation."""

    default_code: ClassVar[str] = "authorization_error"


class ConnectionError(ECNClientError):
    """Raised when an MQTT connection or transport operation fails."""

    default_code: ClassVar[str] = "connection_error"


class DeliveryError(ECNClientError):
    """Report a definite or uncertain mutation delivery phase without implying retry safety."""

    default_code: ClassVar[str] = "delivery_error"

    def __init__(
        self,
        message: object,
        *,
        delivery_phase: DeliveryPhase,
        operation: str,
        task_id: str | None = None,
        operation_id: UUID | None = None,
        code: str | None = None,
        status_code: int | None = None,
        details: Mapping[str, object] | None = None,
        secrets: tuple[str, ...] = (),
    ) -> None:
        """Initialize one secret-safe mutation delivery failure.

        Args:
            message: Failure message to redact and expose.
            delivery_phase: Strongest safe delivery fact established.
            operation: Stable public operation name.
            task_id: Generated task correlation identifier, when applicable.
            operation_id: Generated publication operation identifier, when applicable.
            code: Optional stable error-code override.
            status_code: Optional status value associated with the failure.
            details: Optional additional secret-safe structured details.
            secrets: Additional exact values to remove from public text.

        Raises:
            ValueError: If a supplied task identifier is empty or exceeds its bound.
        """

        if task_id is not None and not 1 <= len(task_id) <= 128:
            raise ValueError("task_id must contain from 1 through 128 characters")
        self.delivery_phase = delivery_phase
        """Strongest safe delivery fact established before failure."""
        self.task_id = task_id
        """Generated task correlation identifier, when applicable."""
        self.operation_id = operation_id
        """Generated publication operation identifier, when applicable."""
        safe_details = dict(details or {})
        safe_details["delivery_phase"] = delivery_phase.value
        if task_id is not None:
            safe_details["task_id"] = task_id
        if operation_id is not None:
            safe_details["operation_id"] = str(operation_id)
        super().__init__(
            message,
            code=code,
            operation=operation,
            status_code=status_code,
            details=safe_details,
            secrets=secrets,
        )


class OutcomeUnknownError(DeliveryError):
    """Report a mutation that may have reached the broker or downstream handler."""

    default_code: ClassVar[str] = "outcome_unknown"
    _UNKNOWN_PHASES: ClassVar[frozenset[DeliveryPhase]] = frozenset(
        {
            DeliveryPhase.LOCAL_SEND_UNCERTAIN,
            DeliveryPhase.BROKER_ACKNOWLEDGMENT_PENDING,
            DeliveryPhase.RESPONSE_PENDING,
        }
    )

    def __init__(
        self,
        message: object,
        *,
        delivery_phase: DeliveryPhase,
        operation: str,
        task_id: str | None = None,
        operation_id: UUID | None = None,
        code: str | None = None,
        status_code: int | None = None,
        details: Mapping[str, object] | None = None,
        secrets: tuple[str, ...] = (),
    ) -> None:
        """Initialize one delivery failure whose mutation outcome is unknown.

        Args:
            message: Failure message to redact and expose.
            delivery_phase: Uncertain delivery phase established at failure.
            operation: Stable public operation name.
            task_id: Generated task correlation identifier, when applicable.
            operation_id: Generated publication operation identifier, when applicable.
            code: Optional stable error-code override.
            status_code: Optional status value associated with the failure.
            details: Optional additional secret-safe structured details.
            secrets: Additional exact values to remove from public text.

        Raises:
            ValueError: If ``delivery_phase`` is not an uncertainty phase.
        """

        if delivery_phase not in self._UNKNOWN_PHASES:
            raise ValueError("outcome-unknown errors require an uncertain delivery phase")
        super().__init__(
            message,
            delivery_phase=delivery_phase,
            operation=operation,
            task_id=task_id,
            operation_id=operation_id,
            code=code,
            status_code=status_code,
            details=details,
            secrets=secrets,
        )


class TransportBoundaryError(ConnectionError):
    """Raised when an endpoint leaves its attested reviewed-network boundary."""

    default_code: ClassVar[str] = "transport_boundary_error"


class ProtocolError(ECNClientError):
    """Raised when received MQTT payloads violate the public protocol."""

    default_code: ClassVar[str] = "protocol_error"


class ValidationError(ECNClientError):
    """Raised when caller input or decoded public model data is invalid."""

    default_code: ClassVar[str] = "validation_error"


class TimeoutError(ECNClientError):
    """Raised when a bounded client operation exceeds its deadline."""

    default_code: ClassVar[str] = "timeout_error"


class NotReadyError(ECNClientError):
    """Raised when an operation requires a ready, open client or service."""

    default_code: ClassVar[str] = "not_ready"


class ResourceLimitError(ECNClientError):
    """Raised when a configured local or broker resource limit is exceeded."""

    default_code: ClassVar[str] = "resource_limit_error"


class ClockError(ECNClientError):
    """Base error for the optional ECN-relative clock diagnostic."""

    default_code: ClassVar[str] = "clock_error"


class ClockProtocolError(ClockError):
    """An NTP response or local timing measurement was unusable."""

    default_code: ClassVar[str] = "clock_protocol_error"


class ClockToleranceError(ClockError):
    """A valid measurement exceeded a caller-selected absolute offset."""

    default_code: ClassVar[str] = "clock_tolerance_exceeded"

    def __init__(self, report: ClockReport) -> None:
        """Record the measurement that exceeded the caller's tolerance.

        Args:
            report: Completed measurement whose absolute offset plus local
                capture uncertainty exceeded ``max_offset_seconds``.
        """

        self.report = report
        """Measurement that failed the tolerance check."""

        super().__init__(
            "ECN-relative clock offset exceeds the required tolerance",
            operation="clock.require_within",
            details={
                "offset_seconds": report.offset_seconds,
                "local_capture_uncertainty_seconds": report.local_capture_uncertainty_seconds,
                "max_offset_seconds": report.max_offset_seconds,
            },
        )


__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "ClockError",
    "ClockProtocolError",
    "ClockToleranceError",
    "ConfigurationError",
    "ConnectionError",
    "DeliveryError",
    "ECNClientError",
    "NotReadyError",
    "OutcomeUnknownError",
    "ProtocolError",
    "ResourceLimitError",
    "TimeoutError",
    "TransportBoundaryError",
    "ValidationError",
]
