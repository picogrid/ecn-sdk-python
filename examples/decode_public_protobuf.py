# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Decode one public entity-event protobuf file without a network connection."""

from __future__ import annotations

from base64 import b64decode

from picogrid_ecn_client import EntityCategory
from picogrid_ecn_client.workflows import decode_public_protobuf

if __package__:
    from ._common import emit, env_enum, env_int, read_payload_file, required_env, run_example
else:
    from _common import (  # type: ignore[import-not-found,no-redef]
        emit,
        env_enum,
        env_int,
        read_payload_file,
        required_env,
        run_example,
    )

_SYNTHETIC_ENTITY_EVENT = (
    "CgYIgPLWygYSTAoQAAAAAAAAQACAAAAAAAACBBoZU3ludGhldGljIHByb3RvYnVmIGVudGl0eSIT"
    "c3ludGhldGljLWRldGVjdGlvbigCMAE4BEoCe30="
)


async def main() -> None:
    result = decode_public_protobuf(
        read_payload_file(),
        integration=required_env("ECN_INTEGRATION_NAME"),
        category=env_enum("ECN_ENTITY_CATEGORY", EntityCategory),
        maximum_payload_size=env_int(
            "ECN_MAXIMUM_PAYLOAD_SIZE",
            default=1024 * 1024,
            minimum=1024,
        ),
    )
    emit(result.event)


def _check() -> None:
    result = decode_public_protobuf(
        b64decode(_SYNTHETIC_ENTITY_EVENT),
        integration="offline-example",
        category=EntityCategory.DETECTION,
    )
    if result.event.entity.category is not EntityCategory.DETECTION:
        raise AssertionError("public protobuf model validation failed")


if __name__ == "__main__":
    run_example("decode public protobuf", main, _check)
