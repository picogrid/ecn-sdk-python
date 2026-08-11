# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import pytest

from operator_app.http_security import RuntimeTrustedHostMiddleware
from operator_app.settings import OperatorSettings, SettingsError


@pytest.mark.parametrize(
    "authority",
    [
        "localhost/path",
        "localhost?query",
        "localhost?",
        "localhost#fragment",
        "localhost#",
        "local\thost",
        "local\rhost",
        "local\\host",
        "localhost:invalid",
        "localhost:70000",
        "localhost:",
        "localhost:0",
    ],
)
def test_runtime_host_parser_rejects_malformed_authorities(authority: str) -> None:
    assert RuntimeTrustedHostMiddleware._host([(b"host", authority.encode("ascii"))]) is None


@pytest.mark.parametrize(
    ("authority", "expected"),
    [
        ("localhost", "localhost"),
        ("localhost:8080", "localhost"),
        ("127.0.0.1:8080", "127.0.0.1"),
    ],
)
def test_runtime_host_parser_accepts_valid_host_authorities(
    authority: str,
    expected: str,
) -> None:
    assert RuntimeTrustedHostMiddleware._host([(b"host", authority.encode("ascii"))]) == expected


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:invalid",
        "http://localhost:70000",
        "http://localhost:",
        "http://localhost:0",
        "http://localhost?",
        "http://localhost#",
        "http" + "://local" + chr(9) + "host",
        "http" + "://local" + chr(92) + "host",
    ],
)
def test_operator_settings_reject_malformed_origin_ports(origin: str) -> None:
    environment = {
        "OPERATOR_MODE": "mock",
        "OPERATOR_ECN_INTEGRATION_ALLOWLIST": "mock-sensor",
        "OPERATOR_ECN_CATEGORY_ALLOWLIST": "TRACK",
        "OPERATOR_ALLOWED_ORIGINS": origin,
    }

    with pytest.raises(SettingsError, match="OPERATOR_ALLOWED_ORIGINS"):
        OperatorSettings.from_env(environment)
