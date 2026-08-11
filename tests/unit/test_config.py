# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from pydantic import SecretStr, ValidationError

from picogrid_ecn_client import (
    BearerTokenAuth,
    CertificateMaterial,
    ConfigurationError,
    ECNConfig,
    MTLSAuth,
    NoAuth,
    PrivateKeyMaterial,
    ReconnectPolicy,
    ReviewedContainerNetwork,
    TLSConfig,
    load_config,
)
from picogrid_ecn_client import config as config_module
from picogrid_ecn_client._profiles import ProfileData, save_profile


def test_reconnect_policy_defaults_and_config_default_factory() -> None:
    policy = ReconnectPolicy()
    config = ECNConfig(
        host="authorized.example",
        mqtt_port=8884,
        integration_name="test-client",
        auth=BearerTokenAuth(
            username="integration-identity",
            token=SecretStr("synthetic"),
        ),
    )

    assert policy.model_dump() == {
        "initial_delay_seconds": 0.5,
        "multiplier": 2.0,
        "maximum_delay_seconds": 30.0,
        "stable_reset_seconds": 60.0,
        "maximum_attempts": None,
        "maximum_elapsed_seconds": None,
    }
    assert config.reconnect_policy == policy


@pytest.mark.parametrize(
    "values",
    [
        {"initial_delay_seconds": 0.0},
        {"multiplier": 0.5},
        {"maximum_delay_seconds": 0.0},
        {"stable_reset_seconds": 0.0},
        {"maximum_attempts": 0},
        {"maximum_attempts": True},
        {"maximum_elapsed_seconds": 0.0},
        {"maximum_elapsed_seconds": float("inf")},
        {"initial_delay_seconds": 2.0, "maximum_delay_seconds": 1.0},
    ],
)
def test_reconnect_policy_rejects_invalid_bounds(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ReconnectPolicy.model_validate(values)


@pytest.mark.parametrize(
    "field",
    [
        "initial_delay_seconds",
        "multiplier",
        "maximum_delay_seconds",
        "stable_reset_seconds",
        "maximum_elapsed_seconds",
    ],
)
@pytest.mark.parametrize("value", [False, True])
def test_reconnect_policy_rejects_boolean_float_fields(field: str, value: bool) -> None:
    with pytest.raises(ValidationError, match="numbers, not booleans"):
        ReconnectPolicy.model_validate({field: value})


@pytest.mark.parametrize(
    "host",
    [
        ".",
        "..",
        "...",
        " . ",
        ".bad",
        "bad..example",
        "bad..",
        "a\u3002.b",
        "a\uff0e.b",
        "a\uff61.b",
        f"{'x' * 64}.example",
        ".".join(["\u00e9" * 57] * 4),
    ],
)
def test_host_rejects_unresolvable_dns_shapes(host: str) -> None:
    with pytest.raises(ValidationError, match="host must be a DNS name or IP literal"):
        ECNConfig(
            host=host,
            mqtt_port=8884,
            integration_name="test-client",
            auth=BearerTokenAuth(
                username="integration-identity",
                token=SecretStr("synthetic"),
            ),
        )


def test_host_accepts_and_removes_one_dns_root_label() -> None:
    maximum_host = ".".join(("a" * 63, "b" * 63, "c" * 63, "d" * 61))
    config = ECNConfig(
        host=f"{maximum_host}.",
        mqtt_port=8884,
        integration_name="test-client",
        auth=BearerTokenAuth(
            username="integration-identity",
            token=SecretStr("synthetic"),
        ),
    )

    assert config.host == maximum_host


def test_host_input_boundary_is_applied_before_whitespace_normalization() -> None:
    normalized_host = "a.example"
    accepted = f"{' ' * (1024 - len(normalized_host))}{normalized_host}"
    rejected = f"{' ' * (1025 - len(normalized_host))}{normalized_host}"
    auth = BearerTokenAuth(
        username="integration-identity",
        token=SecretStr("synthetic"),
    )

    config = ECNConfig(
        host=accepted,
        mqtt_port=8884,
        integration_name="test-client",
        auth=auth,
    )
    assert config.host == normalized_host

    with pytest.raises(ValidationError, match="host must be a DNS name or IP literal"):
        ECNConfig(
            host=rejected,
            mqtt_port=8884,
            integration_name="test-client",
            auth=auth,
        )


def test_host_accepts_ipv6_literals() -> None:
    config = ECNConfig(
        host="::1",
        mqtt_port=1883,
        integration_name="test-client",
        auth=BearerTokenAuth(token=SecretStr("synthetic")),
        tls=TLSConfig(enabled=False, verify=False),
        allow_insecure=True,
    )

    assert config.host == "::1"


def test_reviewed_container_network_plaintext_no_auth_is_valid() -> None:
    config = ECNConfig(
        host="mqtt-container.example",
        mqtt_port=1883,
        integration_name="test-client",
        auth=NoAuth(),
        tls=TLSConfig(enabled=False),
        plaintext_container_network=ReviewedContainerNetwork(name="reviewed-network"),
    )

    assert isinstance(config.auth, NoAuth)
    assert config.plaintext_container_network == ReviewedContainerNetwork(name="reviewed-network")


def test_default_remote_bearer_tls_config_remains_valid() -> None:
    config = ECNConfig(
        host="authorized.example",
        mqtt_port=8884,
        integration_name="test-client",
        auth=BearerTokenAuth(
            username="integration-identity",
            token=SecretStr("synthetic"),
        ),
    )

    assert config.tls.enabled
    assert config.plaintext_container_network is None


def test_loopback_bearer_plaintext_mock_config_remains_valid() -> None:
    config = ECNConfig(
        host="127.0.0.1",
        mqtt_port=1883,
        integration_name="test-client",
        auth=BearerTokenAuth(token=SecretStr("synthetic")),
        tls=TLSConfig(enabled=False),
        allow_insecure=True,
    )

    assert not config.tls.enabled
    assert config.allow_insecure


def test_no_auth_requires_reviewed_container_network_attestation() -> None:
    with pytest.raises(
        ValidationError,
        match="authentication kind 'none' requires an explicit reviewed container-network attestation",
    ):
        ECNConfig(
            host="mqtt-container.example",
            mqtt_port=1883,
            integration_name="test-client",
            auth=NoAuth(),
            tls=TLSConfig(enabled=False),
        )


def test_static_bearer_auth_rejects_reviewed_container_network_attestation() -> None:
    with pytest.raises(
        ValidationError,
        match="reviewed container-network plaintext transport requires authentication kind 'none'",
    ):
        ECNConfig(
            host="mqtt-container.example",
            mqtt_port=1883,
            integration_name="test-client",
            auth=BearerTokenAuth(token=SecretStr("synthetic")),
            tls=TLSConfig(enabled=False),
            plaintext_container_network=ReviewedContainerNetwork(name="reviewed-network"),
        )


def test_credentials_provider_rejects_reviewed_container_network_attestation() -> None:
    async def credentials_provider() -> tuple[str, str]:
        return "integration-identity", "synthetic"

    with pytest.raises(
        ValidationError,
        match="reviewed container-network plaintext transport requires authentication kind 'none'",
    ):
        ECNConfig(
            host="mqtt-container.example",
            mqtt_port=1883,
            integration_name="test-client",
            auth=BearerTokenAuth(credentials_provider=credentials_provider),
            tls=TLSConfig(enabled=False),
            plaintext_container_network=ReviewedContainerNetwork(name="reviewed-network"),
        )


def test_mtls_rejects_reviewed_container_network_attestation() -> None:
    with pytest.raises(
        ValidationError,
        match="reviewed container-network plaintext transport requires authentication kind 'none'",
    ):
        ECNConfig(
            host="mqtt-container.example",
            mqtt_port=8883,
            integration_name="test-client",
            auth=MTLSAuth(
                client_certificate=CertificateMaterial(data=SecretStr("synthetic certificate")),
                client_key=PrivateKeyMaterial(data=SecretStr("synthetic private key")),
            ),
            plaintext_container_network=ReviewedContainerNetwork(name="reviewed-network"),
        )


def test_reviewed_container_network_attestation_rejects_enabled_tls() -> None:
    with pytest.raises(
        ValidationError,
        match="reviewed container-network plaintext attestation requires TLS to be explicitly disabled",
    ):
        ECNConfig(
            host="mqtt-container.example",
            mqtt_port=1883,
            integration_name="test-client",
            auth=NoAuth(),
            tls=TLSConfig(enabled=True),
            plaintext_container_network=ReviewedContainerNetwork(name="reviewed-network"),
        )


def test_reviewed_container_network_attestation_rejects_unverified_enabled_tls() -> None:
    with pytest.raises(
        ValidationError,
        match="reviewed container-network plaintext attestation requires TLS to be explicitly disabled",
    ):
        ECNConfig(
            host="mqtt-container.example",
            mqtt_port=1883,
            integration_name="test-client",
            auth=NoAuth(),
            tls=TLSConfig(enabled=True, verify=False),
            plaintext_container_network=ReviewedContainerNetwork(name="reviewed-network"),
        )


def test_reviewed_container_network_attestation_rejects_allow_insecure() -> None:
    with pytest.raises(
        ValidationError,
        match="attestation already authorizes the plaintext transport",
    ):
        ECNConfig(
            host="mqtt-container.example",
            mqtt_port=1883,
            integration_name="test-client",
            auth=NoAuth(),
            tls=TLSConfig(enabled=False),
            allow_insecure=True,
            plaintext_container_network=ReviewedContainerNetwork(name="reviewed-network"),
        )


def test_no_auth_with_enabled_tls_still_requires_attestation() -> None:
    with pytest.raises(
        ValidationError,
        match="authentication kind 'none' requires an explicit reviewed container-network attestation",
    ):
        ECNConfig(
            host="mqtt-container.example",
            mqtt_port=1883,
            integration_name="test-client",
            auth=NoAuth(),
            tls=TLSConfig(enabled=True),
        )


def test_load_config_no_auth_uses_plaintext_port_and_attestation() -> None:
    config = load_config(
        environment={
            "ECN_HOST": "mqtt-container.example",
            "ECN_INTEGRATION_NAME": "sensor-example",
            "ECN_AUTH": "none",
            "ECN_TLS_ENABLED": "false",
            "ECN_PLAINTEXT_CONTAINER_NETWORK": "reviewed-network",
        }
    )

    assert isinstance(config.auth, NoAuth)
    assert config.mqtt_port == 1883
    assert config.plaintext_container_network == ReviewedContainerNetwork(name="reviewed-network")


@pytest.mark.parametrize(
    ("source", "credential_name", "credential_value"),
    [
        ("environment", "ECN_BEARER_TOKEN", "synthetic-token"),
        ("environment", "ECN_MQTT_USERNAME", "synthetic-username"),
        ("environment", "ECN_CLIENT_CERT", "/synthetic/client.crt"),
        ("environment", "ECN_CLIENT_KEY", "/synthetic/client.key"),
        ("environment", "ECN_CLIENT_KEY_PASSWORD", "synthetic-password"),
        ("profile", "mqtt_username", "synthetic-username"),
        ("profile", "client_certificate", "/synthetic/client.crt"),
        ("profile", "client_key", "/synthetic/client.key"),
    ],
)
def test_load_config_no_auth_rejects_every_configured_credential(
    tmp_path: Path,
    source: str,
    credential_name: str,
    credential_value: str,
) -> None:
    profile_environment = {"XDG_CONFIG_HOME": str(tmp_path / "configuration")}
    base_environment = profile_environment | {
        "ECN_TLS_ENABLED": "false",
        "ECN_PLAINTEXT_CONTAINER_NETWORK": "reviewed-network",
    }
    if source == "profile":
        save_profile(
            "stored",
            {
                "host": "mqtt-container.example",
                "integration_name": "sensor-example",
                "auth": "bearer" if credential_name == "mqtt_username" else "mtls",
                credential_name: credential_value,
            },
            profile_environment,
        )
        profile = "stored"
        environment = base_environment | {"ECN_AUTH": "none"}
    else:
        profile = None
        environment = base_environment | {
            "ECN_HOST": "mqtt-container.example",
            "ECN_INTEGRATION_NAME": "sensor-example",
            "ECN_AUTH": "none",
            credential_name: credential_value,
        }

    with pytest.raises(
        ConfigurationError,
        match="authentication kind 'none' does not accept MQTT credential settings",
    ) as caught:
        load_config(profile=profile, environment=environment)

    assert credential_value not in str(caught.value)


def test_loopback_plaintext_requires_explicit_opt_in() -> None:
    with pytest.raises(ValidationError, match="allow_insecure"):
        ECNConfig(
            host="127.0.0.1",
            mqtt_port=1883,
            integration_name="test-client",
            auth=BearerTokenAuth(token=SecretStr("synthetic")),
            tls=TLSConfig(enabled=False, verify=False),
        )


def test_plaintext_is_limited_to_loopback() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        ECNConfig(
            host="example.invalid",
            mqtt_port=1883,
            integration_name="test-client",
            auth=BearerTokenAuth(token=SecretStr("synthetic")),
            tls=TLSConfig(enabled=False, verify=False),
            allow_insecure=True,
        )


def test_unverified_tls_is_limited_to_explicit_loopback_mock_use() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        ECNConfig(
            host="example.invalid",
            mqtt_port=8883,
            integration_name="test-client",
            auth=BearerTokenAuth(token=SecretStr("synthetic")),
            tls=TLSConfig(enabled=True, verify=False),
            allow_insecure=True,
        )

    config = ECNConfig(
        host="127.0.0.1",
        mqtt_port=8883,
        integration_name="test-client",
        auth=BearerTokenAuth(token=SecretStr("synthetic")),
        tls=TLSConfig(enabled=True, verify=False),
        allow_insecure=True,
    )
    assert not config.tls.verify


def test_mtls_authentication_requires_tls() -> None:
    auth = MTLSAuth(
        client_certificate=CertificateMaterial(data=SecretStr("synthetic certificate material")),
        client_key=PrivateKeyMaterial(data=SecretStr("synthetic private key material")),
    )
    with pytest.raises(ValidationError, match="mTLS authentication requires TLS") as raised:
        ECNConfig(
            host="127.0.0.1",
            mqtt_port=1883,
            integration_name="test-client",
            auth=auth,
            tls=TLSConfig(enabled=False, verify=False),
            allow_insecure=True,
        )
    rendered = str(raised.value)
    assert "synthetic certificate material" not in rendered
    assert "synthetic private key material" not in rendered


def test_bearer_token_is_masked_and_provider_is_not_in_schema() -> None:
    auth = BearerTokenAuth(token=SecretStr("synthetic"))
    assert "synthetic" not in repr(auth)
    assert "token_provider" not in BearerTokenAuth.model_json_schema()["properties"]
    assert "credentials_provider" not in BearerTokenAuth.model_json_schema()["properties"]


@pytest.mark.parametrize("field", ["token_provider", "credentials_provider"])
def test_synchronous_credential_provider_is_rejected_before_invocation(field: str) -> None:
    called = False

    def provider() -> object:
        nonlocal called
        called = True
        return "synthetic"

    with pytest.raises(ValidationError, match="cooperative async callables"):
        BearerTokenAuth(**{field: provider})

    assert called is False


@pytest.mark.asyncio
async def test_bearer_tokens_respect_mqtt_encoded_password_limit() -> None:
    accepted = "x" * 65_535
    static = BearerTokenAuth(token=SecretStr(accepted))
    assert await static._resolve_token() == accepted

    async def oversized_provider() -> str:
        return "\u00e9" * 32_768

    async def unsafe_provider() -> str:
        return "token-canary\ud800"

    invalid = (
        BearerTokenAuth(token=SecretStr("x" * 65_536)),
        BearerTokenAuth(token_provider=oversized_provider),
        BearerTokenAuth(token_provider=unsafe_provider),
    )
    for auth in invalid:
        with pytest.raises(ValueError, match="invalid token") as raised:
            await auth._resolve_token()
        assert "token-canary" not in str(raised.value)


def test_credentials_provider_owns_username_and_is_the_only_token_source() -> None:
    async def provider() -> tuple[str, str]:
        return "integration-identity", "synthetic"

    auth = BearerTokenAuth(credentials_provider=provider)
    assert auth.credentials_provider is provider

    with pytest.raises(ValidationError, match="supplies the MQTT username"):
        BearerTokenAuth(username="override", credentials_provider=provider)
    with pytest.raises(ValidationError, match="exactly one"):
        BearerTokenAuth(
            token=SecretStr("synthetic"),
            credentials_provider=provider,
        )


def test_bearer_username_is_trimmed_and_rejects_invalid_mqtt_utf8() -> None:
    auth = BearerTokenAuth(
        username="  deployment-identity-\U0001f6f0  ",
        token=SecretStr("synthetic"),
    )
    assert auth.username == "deployment-identity-\U0001f6f0"

    invalid_usernames = (
        "   ",
        "deployment\x00identity",
        "deployment\nidentity",
        "deployment\x7fidentity",
        "deployment\x85identity",
        "deployment\ud800identity",
        "deployment\ufdd0identity",
        "deployment\ufffeidentity",
        "deployment\U0001fffeidentity",
        "deployment\U0001ffffidentity",
        "x" * 257,
    )
    for username in invalid_usernames:
        with pytest.raises(ValidationError, match="MQTT UTF-8 value"):
            BearerTokenAuth(username=username, token=SecretStr("synthetic"))


def test_remote_bearer_profile_requires_provided_mqtt_username() -> None:
    with pytest.raises(ValidationError, match="provided MQTT username"):
        ECNConfig(
            host="authorized.example",
            mqtt_port=8883,
            integration_name="test-client",
            auth=BearerTokenAuth(token=SecretStr("synthetic")),
        )

    config = ECNConfig(
        host="authorized.example",
        mqtt_port=8883,
        integration_name="test-client",
        auth=BearerTokenAuth(
            username="00000000-0000-4000-8000-000000000001",
            token=SecretStr("synthetic"),
        ),
    )
    assert isinstance(config.auth, BearerTokenAuth)
    assert config.auth.username == "00000000-0000-4000-8000-000000000001"


def test_load_config_uses_source_confirmed_authentication_port_defaults() -> None:
    mtls = load_config(
        environment={
            "ECN_HOST": "authorized.example",
            "ECN_INTEGRATION_NAME": "sensor-example",
            "ECN_AUTH": "mtls",
            "ECN_CLIENT_CERT": "/external/client.crt",
            "ECN_CLIENT_KEY": "/external/client.key",
        }
    )
    bearer = load_config(
        environment={
            "ECN_HOST": "authorized.example",
            "ECN_INTEGRATION_NAME": "sensor-example",
            "ECN_AUTH": "bearer",
            "ECN_MQTT_USERNAME": "integration-identity",
            "ECN_BEARER_TOKEN": "synthetic-token",
        }
    )

    assert mtls.mqtt_port == 8883
    assert bearer.mqtt_port == 8884


@pytest.mark.parametrize("auth_kind", ["mtls", "legion"])
def test_blank_bearer_environment_does_not_conflict_with_other_authentication(
    auth_kind: str,
) -> None:
    environment = {
        "ECN_HOST": "authorized.example",
        "ECN_INTEGRATION_NAME": "sensor-example",
        "ECN_AUTH": auth_kind,
        "ECN_BEARER_TOKEN": "   ",
    }
    if auth_kind == "mtls":
        environment |= {
            "ECN_CLIENT_CERT": "/external/client.crt",
            "ECN_CLIENT_KEY": "/external/client.key",
        }

    config = load_config(environment=environment)

    assert isinstance(config.auth, MTLSAuth if auth_kind == "mtls" else BearerTokenAuth)


def test_nonblank_bearer_token_bytes_are_not_normalized() -> None:
    token = " synthetic-token "
    config = load_config(
        environment={
            "ECN_HOST": "authorized.example",
            "ECN_INTEGRATION_NAME": "sensor-example",
            "ECN_AUTH": "bearer",
            "ECN_MQTT_USERNAME": "integration-identity",
            "ECN_BEARER_TOKEN": token,
        }
    )

    assert isinstance(config.auth, BearerTokenAuth)
    assert config.auth.token is not None
    assert config.auth.token.get_secret_value() == token


def test_load_config_profile_resolution_and_environment_override(tmp_path: Path) -> None:
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "configuration")}
    save_profile(
        "stored",
        {
            "host": "stored.example.invalid",
            "integration_name": "sensor-example",
            "auth": "mtls",
            "client_certificate": "/external/client.crt",
            "client_key": "/external/client.key",
        },
        environment,
    )

    config = load_config(
        profile="stored",
        environment=environment
        | {
            "ECN_HOST": "override.example.invalid",
            "ECN_MQTT_PORT": "9443",
        },
    )

    assert config.host == "override.example.invalid"
    assert config.mqtt_port == 9443


def test_load_config_maps_profile_reconnect_policy_with_environment_precedence(
    tmp_path: Path,
) -> None:
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "configuration")}
    save_profile(
        "stored",
        {
            "host": "stored.example.invalid",
            "integration_name": "sensor-example",
            "auth": "bearer",
            "mqtt_username": "integration-identity",
            "reconnect_policy": {
                "initial_delay_seconds": 0.75,
                "multiplier": 1.5,
                "maximum_delay_seconds": 12.0,
                "stable_reset_seconds": 45.0,
                "maximum_attempts": 7,
                "maximum_elapsed_seconds": 90.0,
            },
        },
        environment,
    )

    config = load_config(
        profile="stored",
        environment=environment
        | {
            "ECN_BEARER_TOKEN": "synthetic-token",
            "ECN_RECONNECT_INITIAL_DELAY_SECONDS": "1.25",
            "ECN_RECONNECT_MULTIPLIER": "3",
            "ECN_RECONNECT_MAXIMUM_DELAY_SECONDS": "20",
            "ECN_RECONNECT_STABLE_RESET_SECONDS": "75",
            "ECN_RECONNECT_MAXIMUM_ATTEMPTS": "11",
        },
    )

    assert config.reconnect_policy == ReconnectPolicy(
        initial_delay_seconds=1.25,
        multiplier=3.0,
        maximum_delay_seconds=20.0,
        stable_reset_seconds=75.0,
        maximum_attempts=11,
        maximum_elapsed_seconds=90.0,
    )


def test_load_config_maps_environment_only_reconnect_policy() -> None:
    config = load_config(
        environment={
            "ECN_HOST": "authorized.example",
            "ECN_INTEGRATION_NAME": "sensor-example",
            "ECN_AUTH": "bearer",
            "ECN_MQTT_USERNAME": "integration-identity",
            "ECN_BEARER_TOKEN": "synthetic-token",
            "ECN_RECONNECT_INITIAL_DELAY_SECONDS": "0.25",
            "ECN_RECONNECT_MULTIPLIER": "2.5",
            "ECN_RECONNECT_MAXIMUM_DELAY_SECONDS": "15",
            "ECN_RECONNECT_STABLE_RESET_SECONDS": "30",
            "ECN_RECONNECT_MAXIMUM_ATTEMPTS": "4",
            "ECN_RECONNECT_MAXIMUM_ELAPSED_SECONDS": "60",
        }
    )

    assert config.reconnect_policy.model_dump() == {
        "initial_delay_seconds": 0.25,
        "multiplier": 2.5,
        "maximum_delay_seconds": 15.0,
        "stable_reset_seconds": 30.0,
        "maximum_attempts": 4,
        "maximum_elapsed_seconds": 60.0,
    }


def test_load_config_rejects_nonmapping_profile_reconnect_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed = cast(
        "ProfileData",
        {
            "host": "stored.example.invalid",
            "integration_name": "sensor-example",
            "auth": "bearer",
            "mqtt_username": "integration-identity",
            "reconnect_policy": "aggressive",
        },
    )
    monkeypatch.setattr(config_module, "load_profile", lambda *_args, **_kwargs: malformed)

    with pytest.raises(ConfigurationError, match="reconnect policy must be a mapping"):
        load_config(
            profile="stored",
            environment={"ECN_BEARER_TOKEN": "synthetic-token"},
        )


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("mqtt_port", "ECN_MQTT_PORT must be an integer"),
        ("ntp_port", "ECN NTP port must be an integer"),
    ],
)
def test_load_config_rejects_boolean_profile_ports(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    message: str,
) -> None:
    malformed = cast(
        "ProfileData",
        {
            "host": "stored.example.invalid",
            "integration_name": "sensor-example",
            "auth": "bearer",
            "mqtt_username": "integration-identity",
            field: True,
        },
    )
    monkeypatch.setattr(config_module, "load_profile", lambda *_args, **_kwargs: malformed)

    with pytest.raises(ConfigurationError, match=message):
        load_config(
            profile="stored",
            environment={"ECN_BEARER_TOKEN": "synthetic-token"},
        )


@pytest.mark.parametrize(
    "name",
    [
        "ECN_RECONNECT_INITIAL_DELAY_SECONDS",
        "ECN_RECONNECT_MAXIMUM_ATTEMPTS",
    ],
)
def test_load_config_rejects_boolean_reconnect_environment_values(name: str) -> None:
    with pytest.raises(ConfigurationError, match="configuration is invalid"):
        load_config(
            environment={
                "ECN_HOST": "authorized.example",
                "ECN_INTEGRATION_NAME": "sensor-example",
                "ECN_AUTH": "bearer",
                "ECN_MQTT_USERNAME": "integration-identity",
                "ECN_BEARER_TOKEN": "synthetic-token",
                name: "true",
            }
        )


def test_load_config_host_environment_boundary_matches_direct_configuration(
    tmp_path: Path,
) -> None:
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "configuration")}
    normalized_host = "a.example"
    accepted = f"{' ' * (1024 - len(normalized_host))}{normalized_host}"
    rejected = f"{' ' * (1025 - len(normalized_host))}{normalized_host}"
    authentication = {
        "ECN_AUTH": "bearer",
        "ECN_INTEGRATION_NAME": "sensor-example",
        "ECN_MQTT_USERNAME": "integration-identity",
        "ECN_BEARER_TOKEN": "synthetic-token",
    }
    save_profile(
        "stored",
        {
            "host": "stored.example.invalid",
            "integration_name": "sensor-example",
            "auth": "bearer",
            "mqtt_username": "integration-identity",
        },
        environment,
    )

    for host_override in ({}, {"ECN_HOST": ""}, {"ECN_HOST": "   "}):
        stored = load_config(
            profile="stored",
            environment=environment | authentication | host_override,
        )
        assert stored.host == "stored.example.invalid"

    for profile, error_pattern in (
        (None, "configuration is invalid"),
        ("stored", "DNS name or IP literal"),
    ):
        config = load_config(
            profile=profile,
            environment=environment | authentication | {"ECN_HOST": accepted},
        )
        assert config.host == normalized_host

        with pytest.raises(ConfigurationError, match=error_pattern):
            load_config(
                profile=profile,
                environment=environment | authentication | {"ECN_HOST": rejected},
            )


def test_load_config_legion_uses_one_dynamic_credentials_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generations = iter(
        [
            ("integration-one", "token-one"),
            ("integration-two", "token-two"),
        ]
    )

    async def provider() -> tuple[str, str]:
        return next(generations)

    monkeypatch.setattr(
        "picogrid_ecn_client.config.legion_system_auth_provider",
        lambda *_args, **_kwargs: provider,
    )

    config = load_config(
        environment={
            "ECN_HOST": "authorized.example",
            "ECN_INTEGRATION_NAME": "sensor-example",
            "ECN_AUTH": "legion",
        }
    )

    assert isinstance(config.auth, BearerTokenAuth)
    assert config.auth.credentials_provider is provider
    assert config.mqtt_port == 8884


def test_load_config_rejects_invalid_port_without_echoing_values() -> None:
    with pytest.raises(ConfigurationError, match="ECN_MQTT_PORT must be an integer"):
        load_config(
            environment={
                "ECN_HOST": "authorized.example",
                "ECN_MQTT_PORT": "not-a-port",
                "ECN_INTEGRATION_NAME": "sensor-example",
                "ECN_AUTH": "bearer",
                "ECN_MQTT_USERNAME": "integration-identity",
                "ECN_BEARER_TOKEN": "synthetic-token",
            }
        )


def test_credential_paths_are_absent_from_repr_and_validation_errors() -> None:
    certificate_path = Path("/synthetic-sensitive/client-certificate.pem")
    key_path = Path("/synthetic-sensitive/client-key.pem")
    certificate = CertificateMaterial(path=certificate_path)
    key = PrivateKeyMaterial(path=key_path)

    assert str(certificate_path) not in repr(certificate)
    assert str(key_path) not in repr(key)

    with pytest.raises(ValidationError) as raised:
        CertificateMaterial(
            path=certificate_path,
            data=SecretStr("synthetic certificate material"),
        )
    rendered = str(raised.value)
    assert str(certificate_path) not in rendered
    assert "synthetic-sensitive" not in rendered
    assert "synthetic certificate material" not in rendered


@pytest.mark.parametrize(
    "integration_name",
    ["x", "-leading", "trailing-", "contains.dot", "geolocation"],
)
def test_integration_name_matches_pinned_identifier_grammar(
    integration_name: str,
) -> None:
    with pytest.raises(ValidationError):
        ECNConfig(
            host="127.0.0.1",
            mqtt_port=1883,
            integration_name=integration_name,
            auth=BearerTokenAuth(token=SecretStr("synthetic")),
            tls=TLSConfig(enabled=False),
            allow_insecure=True,
        )
