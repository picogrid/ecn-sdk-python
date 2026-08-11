# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Public client configuration with conservative local resource limits."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Final, Self
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

from ._legion_auth import legion_system_auth_provider
from ._network import normalize_host
from ._profiles import ProfileData, load_profile, resolve_profile_name
from .auth import (
    AuthConfig,
    BearerTokenAuth,
    CertificateMaterial,
    MTLSAuth,
    NoAuth,
    PrivateKeyMaterial,
    TLSConfig,
)
from .exceptions import ConfigurationError
from .models import WireFormat
from .models._base import IntegrationName, PublicModel

_MTLS_DEFAULT_PORT: Final = 8883
_BEARER_DEFAULT_PORT: Final = 8884
_RECONNECT_ENVIRONMENT_FIELDS: Final = {
    "initial_delay_seconds": "ECN_RECONNECT_INITIAL_DELAY_SECONDS",
    "multiplier": "ECN_RECONNECT_MULTIPLIER",
    "maximum_delay_seconds": "ECN_RECONNECT_MAXIMUM_DELAY_SECONDS",
    "stable_reset_seconds": "ECN_RECONNECT_STABLE_RESET_SECONDS",
    "maximum_attempts": "ECN_RECONNECT_MAXIMUM_ATTEMPTS",
    "maximum_elapsed_seconds": "ECN_RECONNECT_MAXIMUM_ELAPSED_SECONDS",
}
_NO_AUTH_DEFAULT_PORT: Final = 1883
_CREDENTIAL_INPUTS: Final[tuple[tuple[str | None, tuple[str, ...]], ...]] = (
    (None, ("ECN_BEARER_TOKEN",)),
    ("mqtt_username", ("ECN_MQTT_USERNAME",)),
    ("client_certificate", ("ECN_CLIENT_CERT",)),
    ("client_key", ("ECN_CLIENT_KEY",)),
    (None, ("ECN_CLIENT_KEY_PASSWORD",)),
    ("ca_certificate", ("ECN_CA_CERT",)),
    (
        "legion_auth_storage",
        ("LEGION_AUTH_STORAGE_PATH", "ECN_LEGION_AUTH_STORAGE"),
    ),
)


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ReconnectPolicy(PublicModel):
    """Configure one bounded exponential reconnect policy with mandatory full jitter."""

    initial_delay_seconds: Annotated[
        float,
        Field(
            gt=0,
            allow_inf_nan=False,
            description="Positive finite initial retry-delay cap in seconds.",
        ),
    ] = 0.5
    multiplier: Annotated[
        float,
        Field(
            ge=1,
            allow_inf_nan=False,
            description="Finite exponential retry multiplier greater than or equal to one.",
        ),
    ] = 2.0
    maximum_delay_seconds: Annotated[
        float,
        Field(
            gt=0,
            allow_inf_nan=False,
            description=(
                "Positive finite maximum full-jitter delay cap in seconds; not below "
                "initial_delay_seconds."
            ),
        ),
    ] = 30.0
    stable_reset_seconds: Annotated[
        float,
        Field(
            gt=0,
            allow_inf_nan=False,
            description=(
                "Positive finite strict-ready duration before attempt and elapsed budgets reset."
            ),
        ),
    ] = 60.0
    maximum_attempts: Annotated[
        int | None,
        Field(
            ge=1,
            description="Optional positive maximum attempts in one recovery episode.",
        ),
    ] = None
    maximum_elapsed_seconds: Annotated[
        float | None,
        Field(
            gt=0,
            allow_inf_nan=False,
            description=("Optional positive finite elapsed-time limit for one recovery episode."),
        ),
    ] = None

    @field_validator(
        "initial_delay_seconds",
        "multiplier",
        "maximum_delay_seconds",
        "stable_reset_seconds",
        "maximum_elapsed_seconds",
        mode="before",
    )
    @classmethod
    def reject_boolean_float_fields(cls, value: object) -> object:
        """Keep booleans from being coerced to floating-point policy values."""

        if isinstance(value, bool):
            raise ValueError("reconnect timing values must be numbers, not booleans")
        return value

    @field_validator("maximum_attempts", mode="before")
    @classmethod
    def reject_boolean_maximum_attempts(cls, value: object) -> object:
        """Keep booleans from being coerced to integer retry budgets."""

        if isinstance(value, bool):
            raise ValueError("maximum_attempts must be a positive integer or None")
        return value

    @model_validator(mode="after")
    def validate_delay_bounds(self) -> Self:
        """Require the maximum retry cap to include the initial retry cap."""

        if self.maximum_delay_seconds < self.initial_delay_seconds:
            raise ValueError("maximum_delay_seconds must not be below initial_delay_seconds")
        return self


class ReviewedContainerNetwork(PublicModel):
    """Record an operator attestation the SDK cannot prove; the SDK enforces that every resolved address is private and revalidated on connect and reconnect.

    The SDK never infers this attestation from a port, hostname, DNS suffix, or deployment
    mode.
    """

    name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
            description=(
                "Operator-attested container-network name recorded for audit; the SDK "
                "cannot prove the review and instead enforces private resolved addresses "
                "revalidated on connect and reconnect. Uses 1 through 128 allowed characters, "
                "such as 'example-reviewed-network'."
            ),
        ),
    ]


class ECNConfig(PublicModel):
    """Configure one immutable SDK MQTT v5 connection.

    Direct construction is explicit: callers provide the endpoint port and
    authentication while bounded timeout and resource settings have conservative
    defaults.
    """

    host: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1024,
            description=(
                "DNS name or IP literal without scheme, port, credentials, whitespace, "
                "or path, from 1 through 1024 characters."
            ),
        ),
    ]
    integration_name: IntegrationName = Field(
        description=(
            "Integration name of 2 through 128 allowed ASCII characters; starts and "
            "ends alphanumeric and excludes reserved 'geolocation'."
        )
    )
    terminal_id: UUID | None = Field(
        default=None,
        description=(
            "Optional canonical connected-terminal UUID used for task source and "
            "addressed response routing."
        ),
    )
    mqtt_port: Annotated[
        int,
        Field(
            ge=1,
            le=65535,
            description="Explicit MQTT port, from 1 through 65,535.",
        ),
    ]
    ntp_host: (
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=1024,
                description=(
                    "Optional NTP host for the ECN-relative clock diagnostic, from 1 "
                    "through 1024 characters. When unset the diagnostic measures "
                    "`host`, so set this only for a separately provided endpoint."
                ),
            ),
        ]
        | None
    ) = Field(
        default=None,
        description=(
            "Optional NTP host for the ECN-relative clock diagnostic, from 1 through "
            "1024 characters. When unset the diagnostic measures `host`, so set this "
            "only for a separately provided endpoint."
        ),
    )
    ntp_port: Annotated[
        int,
        Field(
            ge=1,
            le=65535,
            description="NTP port used by the clock diagnostic, from 1 through 65,535.",
        ),
    ] = 123
    auth: AuthConfig = Field(
        description="Bearer-token, mutual-TLS, or no-authentication configuration."
    )
    tls: TLSConfig = Field(
        default_factory=TLSConfig,
        description="TLS settings; defaults to enabled peer-verified TLS.",
    )
    wire_format: WireFormat = Field(
        default=WireFormat.JSON,
        description="Entity and location payload encoding; defaults to JSON over MQTT v5.",
    )
    connection_timeout: Annotated[
        float,
        Field(
            gt=0,
            le=120,
            description="Connect-stage timeout in seconds, greater than 0 through 120.",
        ),
    ] = 10.0
    operation_timeout: Annotated[
        float,
        Field(
            gt=0,
            le=300,
            description=(
                "General operation and observed-update timeout in seconds, greater "
                "than 0 through 300."
            ),
        ),
    ] = 30.0
    task_timeout: Annotated[
        float,
        Field(
            gt=0,
            le=3600,
            description="Default task response timeout in seconds, greater than 0 through 3,600.",
        ),
    ] = 10.0
    shutdown_timeout: Annotated[
        float,
        Field(
            gt=0,
            le=60,
            description="Whole-client cleanup timeout in seconds, greater than 0 through 60.",
        ),
    ] = 5.0
    reconnect_policy: ReconnectPolicy = Field(
        default_factory=ReconnectPolicy,
        description="Typed exponential reconnect, jitter, stable-reset, and retry-budget policy.",
    )
    watcher_buffer_size: Annotated[
        int,
        Field(
            ge=1,
            le=100_000,
            description="Maximum buffered events per watcher, from 1 through 100,000.",
        ),
    ] = 256
    maximum_payload_size: Annotated[
        int,
        Field(
            ge=1024,
            le=16 * 1024 * 1024,
            description=(
                "Maximum accepted or emitted payload in bytes, from 1,024 through 16,777,216 bytes."
            ),
        ),
    ] = 1024 * 1024
    maximum_outstanding_operations: Annotated[
        int,
        Field(
            ge=1,
            le=10_000,
            description="Maximum correlated task operations, from 1 through 10,000.",
        ),
    ] = 128
    allow_insecure: bool = Field(
        default=False,
        description=(
            "Whether plaintext or unverified TLS is permitted for loopback mock "
            "endpoints; defaults to false."
        ),
    )
    plaintext_container_network: ReviewedContainerNetwork | None = Field(
        default=None,
        description=(
            "Explicit attestation that plaintext MQTT is confined to one reviewed container "
            "network; requires TLS to be disabled and authentication kind to be 'none'; "
            "defaults to null."
        ),
    )

    @field_validator("host", mode="before")
    @classmethod
    def validate_host(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return normalize_host(value)

    @field_validator("ntp_host", mode="before")
    @classmethod
    def validate_ntp_host(cls, value: object) -> object:
        if value is None or not isinstance(value, str):
            return value
        return normalize_host(value)

    @model_validator(mode="after")
    def validate_security_boundary(self) -> ECNConfig:
        if isinstance(self.auth, MTLSAuth) and not self.tls.enabled:
            raise ValueError("mTLS authentication requires TLS to be enabled")
        if self.plaintext_container_network is not None:
            if self.allow_insecure:
                raise ValueError(
                    "allow_insecure=True cannot be combined with a reviewed "
                    "container-network attestation; the attestation already authorizes "
                    "the plaintext transport"
                )
            if not isinstance(self.auth, NoAuth):
                raise ValueError(
                    "reviewed container-network plaintext transport requires authentication "
                    "kind 'none'"
                )
            if self.tls.enabled:
                raise ValueError(
                    "reviewed container-network plaintext attestation requires TLS to be "
                    "explicitly disabled"
                )
        if isinstance(self.auth, NoAuth) and self.plaintext_container_network is None:
            raise ValueError(
                "authentication kind 'none' requires an explicit reviewed container-network "
                "attestation"
            )
        if self.plaintext_container_network is None and (
            not self.tls.enabled or not self.tls.verify
        ):
            if not self.allow_insecure:
                raise ValueError("plaintext or unverified TLS requires allow_insecure=True")
            if not _is_loopback(self.host):
                raise ValueError(
                    "plaintext or unverified TLS is allowed only for loopback mock endpoints"
                )
        if (
            isinstance(self.auth, BearerTokenAuth)
            and self.auth.username is None
            and self.auth.credentials_provider is None
            and not _is_loopback(self.host)
        ):
            raise ValueError("remote bearer authentication requires a provided MQTT username")
        return self


def load_config(
    *,
    profile: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> ECNConfig:
    """Load a named non-secret profile with environment overrides.

    An explicit ``profile`` takes precedence over the profile selected by the
    environment. Without either, configuration comes from environment values only.
    Environment values override stored non-secret profile settings. Profile loading
    applies the documented authentication-specific port defaults; direct
    ``ECNConfig`` construction does not.

    Args:
        profile: Optional profile name to load.
        environment: Optional environment mapping, primarily for controlled callers
            and tests. Defaults to the process environment.

    Returns:
        Validated immutable configuration for one MQTT v5 connection.

    Raises:
        ConfigurationError: If profile loading or configuration validation fails.
    """

    try:
        return _load_config(profile=profile, environment=environment)
    except ConfigurationError:
        raise
    except (PydanticValidationError, TypeError, ValueError):
        raise ConfigurationError("ECN configuration is invalid") from None


def _load_config(
    *,
    profile: str | None,
    environment: Mapping[str, str] | None,
) -> ECNConfig:
    values = dict(os.environ if environment is None else environment)
    selected_profile = resolve_profile_name(profile, values)
    data = load_profile(selected_profile, values) if selected_profile is not None else {}
    auth_kind = _auth_kind(data, values)
    auth = _authentication(auth_kind, data, values)
    port = _mqtt_port(data, values, auth_kind)
    allow_insecure = _environment_bool(values, "ECN_ALLOW_INSECURE", default=False)
    tls_enabled = _environment_bool(
        values,
        "ECN_TLS_ENABLED",
        default=True if auth_kind == "none" else not allow_insecure,
    )
    plaintext_network_name = _nonempty(values.get("ECN_PLAINTEXT_CONTAINER_NETWORK"))
    plaintext_container_network = (
        ReviewedContainerNetwork(name=plaintext_network_name)
        if plaintext_network_name is not None
        else None
    )
    ca_certificate = _setting(data, values, "ca_certificate", "ECN_CA_CERT")
    wire_format = _setting(data, values, "wire_format", "ECN_WIRE_FORMAT") or "json"
    ntp_host = _setting(data, values, "ntp_host", "ECN_NTP_HOST")
    ntp_port = _ntp_port(data, values)
    reconnect_policy = _reconnect_policy(data, values)

    config_values: dict[str, object] = {
        "host": _required_setting(
            data,
            values,
            "host",
            "ECN_HOST",
            preserve_raw=True,
        ),
        "integration_name": _required_setting(
            data,
            values,
            "integration_name",
            "ECN_INTEGRATION_NAME",
        ),
        "terminal_id": _setting(data, values, "terminal_id", "ECN_TERMINAL_ID"),
        "mqtt_port": port,
        "ntp_host": ntp_host,
        "auth": auth,
        "tls": TLSConfig(
            enabled=tls_enabled,
            verify=_environment_bool(values, "ECN_TLS_VERIFY", default=True),
            ca_certificate=(
                CertificateMaterial(path=Path(ca_certificate)) if ca_certificate else None
            ),
        ),
        "wire_format": WireFormat(wire_format),
        "reconnect_policy": reconnect_policy,
        "allow_insecure": allow_insecure,
        "plaintext_container_network": plaintext_container_network,
    }
    if ntp_port is not None:
        config_values["ntp_port"] = ntp_port
    return ECNConfig.model_validate(config_values)


def _reconnect_policy(
    data: ProfileData,
    values: Mapping[str, str],
) -> ReconnectPolicy:
    stored = data.get("reconnect_policy")
    if stored is not None and not isinstance(stored, Mapping):
        raise ConfigurationError("ECN profile reconnect policy must be a mapping")
    policy_values: dict[str, object] = dict(stored) if isinstance(stored, Mapping) else {}
    for field, environment_name in _RECONNECT_ENVIRONMENT_FIELDS.items():
        override = _nonempty(values.get(environment_name))
        if override is not None:
            policy_values[field] = override
    return ReconnectPolicy.model_validate(policy_values)


def _auth_kind(data: ProfileData, values: Mapping[str, str]) -> str:
    explicit = _setting(data, values, "auth", "ECN_AUTH")
    if explicit is not None:
        if explicit not in {"bearer", "legion", "mtls", "none"}:
            raise ConfigurationError("ECN_AUTH must be bearer, legion, mtls, or none")
        return explicit

    bearer_token = _nonblank_secret(values.get("ECN_BEARER_TOKEN"))
    client_certificate = _setting(data, values, "client_certificate", "ECN_CLIENT_CERT")
    client_key = _setting(data, values, "client_key", "ECN_CLIENT_KEY")
    if bearer_token is not None:
        if client_certificate is not None or client_key is not None:
            raise ConfigurationError("configure one ECN authentication method")
        return "bearer"
    if client_certificate is not None or client_key is not None:
        return "mtls"
    raise ConfigurationError("select ECN authentication with a profile or ECN_AUTH")


def _authentication(
    auth_kind: str,
    data: ProfileData,
    values: Mapping[str, str],
) -> BearerTokenAuth | MTLSAuth | NoAuth:
    bearer_token = _nonblank_secret(values.get("ECN_BEARER_TOKEN"))
    client_certificate = _setting(data, values, "client_certificate", "ECN_CLIENT_CERT")
    client_key = _setting(data, values, "client_key", "ECN_CLIENT_KEY")
    if auth_kind == "none":
        if _credential_input_is_configured(data, values):
            raise ConfigurationError(
                "authentication kind 'none' does not accept MQTT credential settings"
            )
        return NoAuth()

    if auth_kind == "mtls":
        if bearer_token is not None:
            raise ConfigurationError("configure one ECN authentication method")
        if client_certificate is None or client_key is None:
            raise ConfigurationError("mTLS requires client certificate and key references")
        key_password = values.get("ECN_CLIENT_KEY_PASSWORD")
        return MTLSAuth(
            client_certificate=CertificateMaterial(path=Path(client_certificate)),
            client_key=PrivateKeyMaterial(
                path=Path(client_key),
                password=SecretStr(key_password) if key_password is not None else None,
            ),
        )

    if client_certificate is not None or client_key is not None:
        raise ConfigurationError("configure one ECN authentication method")
    if auth_kind == "legion":
        if bearer_token is not None:
            raise ConfigurationError("legion authentication does not accept ECN_BEARER_TOKEN")
        if _setting(data, values, "mqtt_username", "ECN_MQTT_USERNAME") is not None:
            raise ConfigurationError(
                "legion authentication derives the MQTT username from integrationId"
            )
        storage = _setting(
            data,
            values,
            "legion_auth_storage",
            "ECN_LEGION_AUTH_STORAGE",
        )
        provider = legion_system_auth_provider(storage, environ=values)
        return BearerTokenAuth(credentials_provider=provider)

    if bearer_token is None:
        raise ConfigurationError("bearer authentication requires ECN_BEARER_TOKEN")
    username = _setting(data, values, "mqtt_username", "ECN_MQTT_USERNAME")
    return BearerTokenAuth(username=username, token=SecretStr(bearer_token))


def _mqtt_port(data: ProfileData, values: Mapping[str, str], auth_kind: str) -> int:
    stored = data.get("mqtt_port")
    if isinstance(stored, bool):
        raise ConfigurationError("ECN_MQTT_PORT must be an integer")
    raw: str | int | None = stored if isinstance(stored, (str, int)) else None
    environment_port = _nonempty(values.get("ECN_MQTT_PORT"))
    if environment_port is not None:
        raw = environment_port
    if raw is None:
        if auth_kind == "mtls":
            return _MTLS_DEFAULT_PORT
        if auth_kind == "none":
            return _NO_AUTH_DEFAULT_PORT
        return _BEARER_DEFAULT_PORT
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise ConfigurationError("ECN_MQTT_PORT must be an integer") from None
    if not 1 <= port <= 65_535:
        raise ConfigurationError("ECN_MQTT_PORT must be between 1 and 65535")
    return port


def _ntp_port(data: ProfileData, values: Mapping[str, str]) -> int | None:
    stored = data.get("ntp_port")
    if isinstance(stored, bool):
        raise ConfigurationError("ECN NTP port must be an integer")
    raw: str | int | None = stored if isinstance(stored, (str, int)) else None
    environment_port = _nonempty(values.get("ECN_NTP_PORT"))
    if environment_port is not None:
        raw = environment_port
    if raw is None:
        return None
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise ConfigurationError("ECN NTP port must be an integer") from None
    if not 1 <= port <= 65_535:
        raise ConfigurationError("ECN NTP port must be between 1 and 65535")
    return port


def _required_setting(
    data: ProfileData,
    values: Mapping[str, str],
    field: str,
    environment_name: str,
    *,
    preserve_raw: bool = False,
) -> str:
    value = _setting(
        data,
        values,
        field,
        environment_name,
        preserve_raw=preserve_raw,
    )
    if value is None:
        raise ConfigurationError(f"required setting {environment_name} is not configured")
    return value


def _setting(
    data: ProfileData,
    values: Mapping[str, str],
    field: str,
    environment_name: str,
    *,
    preserve_raw: bool = False,
) -> str | None:
    raw_environment_value = values.get(environment_name)
    if preserve_raw and raw_environment_value is not None and raw_environment_value.strip():
        return raw_environment_value
    environment_value = _nonempty(values.get(environment_name))
    if environment_value is not None:
        return environment_value
    value = data.get(field)
    return value if isinstance(value, str) else None


def _environment_bool(
    values: Mapping[str, str],
    name: str,
    *,
    default: bool,
) -> bool:
    value = _nonempty(values.get(name))
    if value is None:
        return default
    normalized = value.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true/false or 1/0")


def _nonempty(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _credential_input_is_configured(
    data: ProfileData,
    values: Mapping[str, str],
) -> bool:
    for profile_field, environment_names in _CREDENTIAL_INPUTS:
        if profile_field is not None:
            profile_value = data.get(profile_field)
            if isinstance(profile_value, str) and profile_value.strip():
                return True
        if any(_nonblank_secret(values.get(name)) is not None for name in environment_names):
            return True
    return False


def _nonblank_secret(value: str | None) -> str | None:
    """Treat blank secret variables as unset without changing valid secret bytes."""

    if value is None or not value.strip():
        return None
    return value


__all__ = ["ECNConfig", "ReconnectPolicy", "ReviewedContainerNetwork", "load_config"]
