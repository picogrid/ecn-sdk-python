# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from enum import Enum
from importlib.metadata import requires

import pytest

from picogrid_ecn_client import EntityCategory


@pytest.mark.differential
def test_installed_sdk_retains_shared_public_entity_categories() -> None:
    sdk = pytest.importorskip(
        "picogrid_edge_sdk",
        reason="optional private SDK is not installed in the public test environment",
    )
    sdk_category = getattr(sdk, "EntityCategory", None)
    if not isinstance(sdk_category, type) or not issubclass(sdk_category, Enum):
        pytest.skip("installed SDK does not export a documented EntityCategory enum")

    shared = {EntityCategory.TRACK.name, EntityCategory.DETECTION.name}
    assert shared <= set(sdk_category.__members__)


def test_release_package_has_no_private_sdk_runtime_dependency() -> None:
    dependencies = requires("picogrid-ecn-client") or ()
    assert not any("picogrid-edge-sdk" in dependency.casefold() for dependency in dependencies)
