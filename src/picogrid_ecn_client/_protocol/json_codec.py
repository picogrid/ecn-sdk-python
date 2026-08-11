# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Strict JSON codec for the private public-protocol adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TypeVar

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from ..exceptions import ProtocolError, ResourceLimitError, ValidationError

ModelT = TypeVar("ModelT", bound=BaseModel)

_MAXIMUM_JSON_NESTING_DEPTH = 64


def _validate_limit(max_size: int) -> None:
    if isinstance(max_size, bool) or not isinstance(max_size, int) or max_size <= 0:
        raise ValidationError("max_size must be a positive integer", operation="codec")


def _reject_nonstandard_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number {value!r}")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _has_excessive_json_nesting(value: object) -> bool:
    stack = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if not isinstance(current, (dict, list, tuple)):
            continue
        nested_depth = depth + 1
        if nested_depth > _MAXIMUM_JSON_NESTING_DEPTH:
            return True
        children = current.values() if isinstance(current, dict) else current
        stack.extend((child, nested_depth) for child in children)
    return False


def encode_json(value: Mapping[str, Any] | BaseModel, max_size: int) -> bytes:
    """Encode one JSON object deterministically and enforce the configured bound."""

    _validate_limit(max_size)
    if not isinstance(value, (BaseModel, Mapping)):
        raise ValidationError("JSON protocol payload must be an object", operation="encode_json")
    try:
        serializable: object
        if isinstance(value, BaseModel):
            serializable = value.model_dump(mode="json")
        else:
            serializable = dict(value)
        if _has_excessive_json_nesting(serializable):
            raise ValueError("JSON nesting exceeds the supported limit")
        payload = json.dumps(
            serializable,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError, UnicodeError):
        raise ValidationError(
            "JSON protocol payload contains an unsupported value",
            operation="encode_json",
        ) from None
    if len(payload) > max_size:
        raise ResourceLimitError(
            "encoded payload exceeds maximum_payload_size",
            operation="encode_json",
            details={"payload_size": len(payload), "maximum_payload_size": max_size},
        )
    return payload


def decode_json(payload: bytes | bytearray | memoryview, max_size: int) -> dict[str, Any]:
    """Decode one bounded UTF-8 JSON object with unambiguous object keys."""

    _validate_limit(max_size)
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ProtocolError("protocol payload must be bytes", operation="decode_json")
    raw = bytes(payload)
    if len(raw) > max_size:
        raise ResourceLimitError(
            "received payload exceeds maximum_payload_size",
            operation="decode_json",
            details={"payload_size": len(raw), "maximum_payload_size": max_size},
        )
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonstandard_constant,
        )
        if _has_excessive_json_nesting(decoded):
            raise ValueError("JSON nesting exceeds the supported limit")
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ProtocolError("malformed JSON protocol payload", operation="decode_json") from None
    if not isinstance(decoded, dict):
        raise ProtocolError("JSON protocol payload must be an object", operation="decode_json")
    return decoded


def decode_model_json(
    payload: bytes | bytearray | memoryview,
    model: type[ModelT],
    max_size: int,
) -> ModelT:
    """Decode a JSON object into one frozen public model without leaking Pydantic errors."""

    data = decode_json(payload, max_size)
    try:
        return model.model_validate(data)
    except PydanticValidationError:
        raise ValidationError(
            f"JSON payload does not match {model.__name__}",
            operation="decode_json",
        ) from None


__all__ = ["decode_json", "decode_model_json", "encode_json"]
