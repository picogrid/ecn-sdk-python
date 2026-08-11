# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Local command allowlist and bounded request-schema validation."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError
from picogrid_ecn_client import TaskMode
from pydantic import BaseModel, ConfigDict, Field, field_validator

_MAX_COMMAND_FILE_BYTES = 128 * 1024
_MAX_TASK_PAYLOAD_BYTES = 16 * 1024
_PROPERTY_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_PROPERTY_TYPES = frozenset({"boolean", "integer", "number", "string"})
_SCHEMA_KEYS = frozenset(
    {
        "$schema",
        "additionalProperties",
        "description",
        "properties",
        "required",
        "title",
        "type",
    }
)
_PROPERTY_KEYS = frozenset(
    {
        "default",
        "description",
        "maximum",
        "maxLength",
        "minimum",
        "minLength",
        "title",
        "type",
    }
)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value} is not allowed")


def _contains_non_finite_number(value: object) -> bool:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            return True
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return False


class CommandPolicyError(RuntimeError):
    """A safe command-policy or caller-payload failure."""


class CommandDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: Annotated[str, Field(min_length=1, max_length=80)]
    description: Annotated[str, Field(min_length=1, max_length=300)]
    allowed_integrations: tuple[Annotated[str, Field(min_length=2, max_length=128)], ...]
    mode: TaskMode = TaskMode.COMPLETE
    request_schema: dict[str, Any]

    @field_validator("mode")
    @classmethod
    def require_response_mode(cls, value: TaskMode) -> TaskMode:
        if value not in {TaskMode.COMPLETE, TaskMode.ACKNOWLEDGMENT}:
            raise ValueError("command mode must be complete or acknowledgment")
        return value

    @field_validator("allowed_integrations")
    @classmethod
    def require_integrations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) > 16 or len(set(value)) != len(value):
            raise ValueError("allowed_integrations must contain 1-16 unique entries")
        return value

    @field_validator("request_schema")
    @classmethod
    def require_bounded_object_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") != "object" or value.get("additionalProperties") is not False:
            raise ValueError("request_schema must be a closed object schema")
        if not set(value).issubset(_SCHEMA_KEYS):
            raise ValueError("request_schema contains unsupported keywords")
        if "$ref" in json.dumps(value, sort_keys=True):
            raise ValueError("request_schema must not contain references")
        properties = value.get("properties", {})
        if not isinstance(properties, dict) or len(properties) > 24:
            raise ValueError("request_schema must contain at most 24 properties")
        for name, definition in properties.items():
            if not isinstance(name, str) or _PROPERTY_NAME.fullmatch(name) is None:
                raise ValueError("request_schema contains an invalid property name")
            if not isinstance(definition, dict) or not set(definition).issubset(_PROPERTY_KEYS):
                raise ValueError("request_schema contains an unsupported property schema")
            if definition.get("type") not in _PROPERTY_TYPES:
                raise ValueError("request_schema properties must use supported scalar types")
            property_type = definition["type"]
            if property_type == "string":
                maximum_length = definition.get("maxLength")
                if (
                    isinstance(maximum_length, bool)
                    or not isinstance(maximum_length, int)
                    or not 1 <= maximum_length <= 2_048
                ):
                    raise ValueError("string properties require maxLength between 1 and 2048")
            if property_type in {"integer", "number"}:
                minimum = definition.get("minimum")
                maximum = definition.get("maximum")
                if (
                    isinstance(minimum, bool)
                    or isinstance(maximum, bool)
                    or not isinstance(minimum, (int, float))
                    or not isinstance(maximum, (int, float))
                    or not math.isfinite(minimum)
                    or not math.isfinite(maximum)
                    or minimum > maximum
                    or abs(minimum) > 1_000_000_000
                    or abs(maximum) > 1_000_000_000
                ):
                    raise ValueError("numeric properties require finite bounded minimum/maximum")
            if "default" in definition:
                if _contains_non_finite_number(definition["default"]):
                    raise ValueError("property defaults must contain only finite numbers")
                if list(Draft202012Validator(definition).iter_errors(definition["default"])):
                    raise ValueError("property default does not match its schema")
        required = value.get("required", [])
        if (
            not isinstance(required, list)
            or len(required) != len(set(required))
            or not all(isinstance(item, str) and item in properties for item in required)
        ):
            raise ValueError("request_schema required fields must name unique properties")
        try:
            Draft202012Validator.check_schema(value)
        except SchemaError as error:
            raise ValueError("request_schema is invalid") from error
        return value


class CommandFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    commands: dict[
        Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")], CommandDefinition
    ]

    @field_validator("commands")
    @classmethod
    def require_commands(cls, value: dict[str, CommandDefinition]) -> dict[str, CommandDefinition]:
        if not value or len(value) > 32:
            raise ValueError("commands must contain 1-32 entries")
        return value


class ValidatedTaskPayload(BaseModel):
    """Public-client request model after local JSON-schema validation."""

    model_config = ConfigDict(extra="allow")


@dataclass(frozen=True, slots=True)
class AllowedCommand:
    name: str
    definition: CommandDefinition
    validator: Draft202012Validator


class CommandCatalog:
    def __init__(self, commands: dict[str, AllowedCommand] | None = None) -> None:
        self._commands = commands or {}

    @classmethod
    def load(cls, path: Path | None) -> CommandCatalog:
        if path is None:
            return cls()
        try:
            if path.stat().st_size > _MAX_COMMAND_FILE_BYTES:
                raise CommandPolicyError("command policy exceeds the size limit")
            raw = json.loads(
                path.read_text(encoding="utf-8"),
                parse_constant=_reject_json_constant,
            )
            parsed = CommandFile.model_validate(raw)
        except CommandPolicyError:
            raise
        except (OSError, json.JSONDecodeError, ValueError):
            raise CommandPolicyError("command policy could not be loaded") from None
        return cls(
            {
                name: AllowedCommand(
                    name=name,
                    definition=definition,
                    validator=Draft202012Validator(definition.request_schema),
                )
                for name, definition in parsed.commands.items()
            }
        )

    def public_inventory(self) -> list[dict[str, object]]:
        return [
            {
                "name": command.name,
                "label": command.definition.label,
                "description": command.definition.description,
                "allowedIntegrations": list(command.definition.allowed_integrations),
                "mode": command.definition.mode.value,
                "requestSchema": command.definition.request_schema,
            }
            for command in sorted(self._commands.values(), key=lambda item: item.name)
        ]

    def validate(
        self,
        *,
        command_name: str,
        integration: str,
        payload: dict[str, object],
    ) -> ValidatedTaskPayload:
        command = self._commands.get(command_name)
        if command is None:
            raise CommandPolicyError("command is not present in the operator allowlist")
        if integration not in command.definition.allowed_integrations:
            raise CommandPolicyError("command is not allowed for the target integration")
        if _contains_non_finite_number(payload):
            raise CommandPolicyError("task payload contains non-finite numbers")
        encoded = json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode()
        if len(encoded) > _MAX_TASK_PAYLOAD_BYTES:
            raise CommandPolicyError("task payload exceeds the size limit")
        try:
            command.validator.validate(payload)
        except JSONSchemaValidationError as error:
            raise CommandPolicyError("task payload does not match the command schema") from error
        return ValidatedTaskPayload.model_validate(payload)

    def mode_for(self, command_name: str) -> TaskMode:
        command = self._commands.get(command_name)
        if command is None:
            raise CommandPolicyError("command is not present in the operator allowlist")
        return command.definition.mode

    def registration_names(self, integration: str) -> tuple[str, ...]:
        """Return the exact mock handler topics represented by the local policy."""

        return tuple(
            command.name
            for command in sorted(self._commands.values(), key=lambda item: item.name)
            if integration in command.definition.allowed_integrations
        )


__all__ = [
    "AllowedCommand",
    "CommandCatalog",
    "CommandPolicyError",
    "ValidatedTaskPayload",
]
