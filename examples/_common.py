# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Shared environment and command-line handling for runnable examples."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast
from uuid import UUID

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

import picogrid_ecn_client as _client_package
from picogrid_ecn_client import (
    ECNClientError,
    ECNConfig,
    EventStream,
    Location,
    WireFormat,
)

ModelT = TypeVar("ModelT", bound=BaseModel)
EnumT = TypeVar("EnumT", bound=StrEnum)
ExampleMain = Callable[[], Coroutine[Any, Any, None]]
ExampleCheck = Callable[[], None]
_profile_override: str | None = None


class _ConfigLoader(Protocol):
    def __call__(self, *, profile: str | None = None) -> ECNConfig: ...


class ExampleConfigurationError(RuntimeError):
    """An understandable, credential-safe example configuration failure."""


def required_env(name: str, *, secret: bool = False) -> str:
    """Read one required value without ever including its contents in an error."""

    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ExampleConfigurationError(f"required environment variable {name} is not set")
    return value if secret else value.strip()


def optional_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def env_int(name: str, *, default: int | None = None, minimum: int | None = None) -> int:
    value = optional_env(name)
    if value is None:
        if default is None:
            raise ExampleConfigurationError(f"required environment variable {name} is not set")
        parsed = default
    else:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ExampleConfigurationError(f"{name} must be an integer") from exc
    if minimum is not None and parsed < minimum:
        raise ExampleConfigurationError(f"{name} must be at least {minimum}")
    return parsed


def env_float(name: str, *, required: bool = False) -> float | None:
    value = optional_env(name)
    if value is None:
        if required:
            raise ExampleConfigurationError(f"required environment variable {name} is not set")
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ExampleConfigurationError(f"{name} must be a number") from exc


def env_uuid(name: str) -> UUID:
    value = required_env(name)
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ExampleConfigurationError(f"{name} must be a UUID") from exc
    if str(parsed) != value:
        raise ExampleConfigurationError(f"{name} must be a canonical UUID")
    return parsed


def optional_env_uuid(name: str) -> UUID | None:
    value = optional_env(name)
    if value is None:
        return None
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ExampleConfigurationError(f"{name} must be a UUID") from exc
    if str(parsed) != value:
        raise ExampleConfigurationError(f"{name} must be a canonical UUID")
    return parsed


def env_enum(
    name: str,
    enum_type: type[EnumT],
    *,
    default: EnumT | None = None,
) -> EnumT:
    value = optional_env(name)
    if value is None:
        if default is None:
            raise ExampleConfigurationError(f"required environment variable {name} is not set")
        return default
    normalized = value.casefold()
    for member in enum_type:
        if normalized in {member.name.casefold(), member.value.casefold()}:
            return member
    allowed = ", ".join(member.value for member in enum_type)
    raise ExampleConfigurationError(f"{name} must be one of: {allowed}")


def utc_now() -> datetime:
    return datetime.now(UTC)


def location_from_env(*, default_source: str) -> Location:
    latitude = env_float("ECN_LATITUDE", required=True)
    longitude = env_float("ECN_LONGITUDE", required=True)
    assert latitude is not None and longitude is not None
    return Location(
        latitude=latitude,
        longitude=longitude,
        altitude=env_float("ECN_ALTITUDE"),
        bearing=env_float("ECN_BEARING"),
        accuracy=env_float("ECN_ACCURACY"),
        confidence=env_float("ECN_CONFIDENCE"),
        source=optional_env("ECN_LOCATION_SOURCE") or default_source,
        recorded_at=utc_now(),
    )


def load_config(*, force_wire_format: WireFormat | None = None) -> ECNConfig:
    """Load the public configuration selected by ``--profile`` or the environment."""

    loader = getattr(_client_package, "load_config", None)
    if loader is None:
        raise ExampleConfigurationError("installed SDK does not provide profile loading")
    config = cast("_ConfigLoader", loader)(profile=_profile_override)
    if force_wire_format is None:
        return config
    return config.model_copy(update={"wire_format": force_wire_format})


def emit(model: BaseModel) -> None:
    print(model.model_dump_json(indent=2, serialize_as_any=True), flush=True)


async def emit_stream(stream: EventStream[ModelT], *, limit: int) -> None:
    """Print a bounded number of typed events, or run until interrupted at zero."""

    emitted = 0
    try:
        async for event in stream:
            emit(event)
            emitted += 1
            if limit and emitted >= limit:
                return
    finally:
        await stream.aclose()


def read_payload_file(name: str = "ECN_PROTOBUF_PAYLOAD_FILE") -> bytes:
    path = Path(required_env(name))
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ExampleConfigurationError(f"could not read the file named by {name}") from exc


def run_example(name: str, main: ExampleMain, check: ExampleCheck) -> None:
    global _profile_override

    parser = argparse.ArgumentParser(description=f"Picogrid ECN {name} example")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate this example offline without credentials or network access",
    )
    parser.add_argument(
        "--profile",
        metavar="NAME",
        help="load a named profile (ECN_PROFILE is used when this flag is omitted)",
    )
    arguments = parser.parse_args()
    _profile_override = arguments.profile
    try:
        if arguments.check:
            check()
            print(f"{name}: offline check passed")
            return
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"{name}: stopped", file=sys.stderr)
    except (ExampleConfigurationError, ECNClientError, PydanticValidationError) as exc:
        print(f"{name}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


__all__ = [
    "ExampleConfigurationError",
    "emit",
    "emit_stream",
    "env_enum",
    "env_float",
    "env_int",
    "env_uuid",
    "load_config",
    "location_from_env",
    "optional_env",
    "read_payload_file",
    "required_env",
    "run_example",
    "utc_now",
]
