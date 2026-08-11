# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Typed public task context, acknowledgment, registration, and result models."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field, JsonValue, field_validator

from ._base import PublicModel, utc_datetime
from .common import TaskMode, TaskStatus

ResultT = TypeVar("ResultT", bound=BaseModel)
"""Pydantic model type carried by a typed task result."""


class TaskRequestContext(PublicModel):
    """Describe the local context supplied to a registered task handler.

    The source is either the literal compatibility value ``local`` or the canonical
    UUID of the source terminal.
    """

    task_id: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            description="Task correlation identifier, from 1 through 128 characters.",
        ),
    ]
    target_entity_id: UUID = Field(description="UUID of the entity targeted by the task.")
    command: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            description="Registered command name, from 1 through 128 characters.",
        ),
    ]
    source: Literal["local"] | UUID = Field(
        description=("Literal 'local' compatibility source or canonical source-terminal UUID.")
    )
    mode: TaskMode = Field(description="Completion behavior requested by the sender.")
    received_at: datetime = Field(
        description="Timezone-aware UTC time when the task request was received locally."
    )

    _normalize_received_at = field_validator("received_at")(utc_datetime)


class TaskAcknowledgement(PublicModel):
    """Report whether a task request was accepted.

    The optional message provides a bounded reader-facing acknowledgment detail.
    """

    task_id: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            description="Task correlation identifier, from 1 through 128 characters.",
        ),
    ]
    accepted: bool = Field(description="Whether the receiver accepted the task request.")
    message: (
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=512,
                description="Optional acknowledgment detail, from 1 through 512 characters.",
            ),
        ]
        | None
    ) = Field(
        default=None,
        description="Optional acknowledgment detail, from 1 through 512 characters.",
    )
    acknowledged_at: datetime = Field(
        description="Timezone-aware UTC time when the acknowledgment was received locally."
    )

    _normalize_acknowledged_at = field_validator("acknowledged_at")(utc_datetime)


class TaskResult(PublicModel, Generic[ResultT]):
    """Carry a typed or JSON-object task result and its reported status.

    Unknown future wire states map to ``TaskStatus.UNKNOWN``. Completion time is
    recorded locally because the wire result has no timestamp.
    """

    task_id: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            description="Task correlation identifier, from 1 through 128 characters.",
        ),
    ]
    status: TaskStatus = Field(description="Reported task result state.")
    data: ResultT | dict[str, JsonValue] | None = Field(
        default=None,
        description="Optional typed result model or JSON object returned by the task.",
    )
    error_message: (
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=1024,
                description=(
                    "Optional flat error detail, from 1 through 1024 characters. "
                    "Values are stripped of surrounding whitespace before "
                    "validation, so a blank string is rejected."
                ),
            ),
        ]
        | None
    ) = Field(
        default=None,
        description=(
            "Optional flat error detail, from 1 through 1024 characters. "
            "Values are stripped of surrounding whitespace before validation, "
            "so a blank string is rejected."
        ),
    )
    completed_at: datetime = Field(
        description="Timezone-aware UTC time when the result was received locally."
    )

    _normalize_completed_at = field_validator("completed_at")(utc_datetime)


class TaskRegistration(PublicModel):
    """Describe one local task-handler registration.

    Request and optional result schemas are JSON Schema objects derived from the
    registered Pydantic model types.
    """

    registration_id: UUID = Field(
        description="Client-generated UUID identifying the local registration."
    )
    entity_id: UUID = Field(description="UUID of the entity that handles the command.")
    command: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            description="Registered command name, from 1 through 128 characters.",
        ),
    ]
    request_schema: dict[str, JsonValue] = Field(
        description="JSON Schema object for the registered request model."
    )
    result_schema: dict[str, JsonValue] | None = Field(
        default=None,
        description="Optional JSON Schema object for the registered result model.",
    )
    registered_at: datetime = Field(
        description="Timezone-aware UTC time when the handler was registered locally."
    )

    _normalize_registered_at = field_validator("registered_at")(utc_datetime)
