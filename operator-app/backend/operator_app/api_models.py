# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Browser-facing API models for the local operator application."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PrepareTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: UUID
    integration: Annotated[str, Field(min_length=2, max_length=128)]
    command: Annotated[str, Field(min_length=1, max_length=128)]
    payload: dict[str, object]


class PreparedTaskResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    preparation_token: str
    expires_at: datetime
    target_key: str
    target_label: str
    command: str
    mode: str
    payload: dict[str, object]
    warning: str


class ConfirmTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preparation_token: Annotated[str, Field(min_length=32, max_length=256)]
    confirmed: Literal[True]


class DiscardPreparedTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preparation_token: Annotated[str, Field(min_length=32, max_length=256)]


class RetireBrowserViewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskConfirmationResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str | None
    target_key: str
    command: str
    mode: str
    status: str
    detail: str
    completed_at: datetime


class SafeConfigurationView(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: str
    read_only: bool
    tasking_enabled: bool
    integrations: tuple[str, ...]
    categories: tuple[str, ...]
    stale_after_seconds: float
    maximum_entities: int
    commands: tuple[dict[str, object], ...]
    basemap_url_template: str | None
    basemap_attribution: str


__all__ = [
    "ConfirmTaskRequest",
    "DiscardPreparedTaskRequest",
    "PrepareTaskRequest",
    "PreparedTaskResponse",
    "RetireBrowserViewRequest",
    "SafeConfigurationView",
    "TaskConfirmationResponse",
]
