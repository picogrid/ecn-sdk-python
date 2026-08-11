# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Strict environment configuration for the operator application."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from ipaddress import ip_address
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit
from uuid import UUID

from picogrid_ecn_client import (
    BearerTokenAuth,
    CertificateMaterial,
    ConfigurationError,
    ECNConfig,
    EntityCategory,
    MTLSAuth,
    PrivateKeyMaterial,
    TLSConfig,
    WireFormat,
    load_config,
)
from pydantic import SecretStr

_INTEGRATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,126}[A-Za-z0-9]")
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


class SettingsError(RuntimeError):
    """A secret-safe operator configuration failure."""


class OperatorMode(StrEnum):
    LIVE = "live"
    MOCK = "mock"


class OperatorAuthProfile(StrEnum):
    MTLS = "mtls"
    BEARER = "bearer"


class _CommonSettings(TypedDict):
    mode: OperatorMode
    client_integration: str
    integrations: tuple[str, ...]
    categories: tuple[EntityCategory, ...]
    wire_format: WireFormat
    tasking_enabled: bool
    commands_file: Path | None
    allowed_origins: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    task_entity_allowlist: frozenset[UUID]
    maximum_entities: int
    maximum_browser_clients: int
    browser_queue_size: int
    diagnostic_limit: int
    task_history_limit: int
    prepared_task_limit: int
    event_buffer_size: int
    stale_after_seconds: float
    prepare_ttl_seconds: float
    synthetic_period_seconds: float
    basemap_url_template: str | None
    basemap_attribution: str


def _value(environment: Mapping[str, str], name: str) -> str | None:
    candidate = environment.get(name)
    if candidate is None or not candidate.strip():
        return None
    return candidate.strip()


def _required(environment: Mapping[str, str], name: str) -> str:
    candidate = _value(environment, name)
    if candidate is None:
        raise SettingsError(f"required environment variable {name} is not set")
    return candidate


def _boolean(environment: Mapping[str, str], name: str, *, default: bool) -> bool:
    candidate = _value(environment, name)
    if candidate is None:
        return default
    normalized = candidate.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SettingsError(f"{name} must be true/false or 1/0")


def _integer(
    environment: Mapping[str, str],
    name: str,
    *,
    default: int | None,
    minimum: int,
    maximum: int,
) -> int:
    candidate = _value(environment, name)
    if candidate is None and default is None:
        raise SettingsError(f"required environment variable {name} is not set")
    try:
        parsed = default if candidate is None else int(candidate)
    except ValueError:
        raise SettingsError(f"{name} must be an integer") from None
    assert parsed is not None
    if not minimum <= parsed <= maximum:
        raise SettingsError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _number(
    environment: Mapping[str, str],
    name: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    candidate = _value(environment, name)
    try:
        parsed = default if candidate is None else float(candidate)
    except ValueError:
        raise SettingsError(f"{name} must be a number") from None
    if not minimum <= parsed <= maximum:
        raise SettingsError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _csv(environment: Mapping[str, str], name: str, *, required: bool = True) -> tuple[str, ...]:
    raw = _value(environment, name)
    if raw is None:
        if required:
            raise SettingsError(f"required environment variable {name} is not set")
        return ()
    values = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    if required and not values:
        raise SettingsError(f"{name} must contain at least one value")
    return values


def _origin(value: str) -> str:
    if (
        any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)
        or "?" in value
        or "#" in value
        or "\\" in value
    ):
        raise SettingsError("OPERATOR_ALLOWED_ORIGINS contains an invalid origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise SettingsError("OPERATOR_ALLOWED_ORIGINS contains an invalid origin") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.netloc.endswith(":")
        or (port is not None and not 0 < port <= 65535)
    ):
        raise SettingsError("OPERATOR_ALLOWED_ORIGINS contains an invalid origin")
    return value.rstrip("/")


def _host(value: str) -> str:
    if (
        not value
        or len(value) > 253
        or any(character in value for character in "/\\@:#?\x00\r\n\t ")
        or value == "*"
    ):
        raise SettingsError("OPERATOR_ALLOWED_HOSTS contains an invalid host")
    return value


def _uuid_allowlist(environment: Mapping[str, str], name: str) -> frozenset[UUID]:
    values = _csv(environment, name, required=False)
    parsed: set[UUID] = set()
    for value in values:
        try:
            entity_id = UUID(value)
        except ValueError:
            raise SettingsError(f"{name} contains a non-UUID value") from None
        if value != str(entity_id):
            raise SettingsError(f"{name} must use canonical lowercase UUID strings")
        parsed.add(entity_id)
    if len(parsed) > 128:
        raise SettingsError(f"{name} exceeds 128 entries")
    return frozenset(parsed)


def _integration(value: str, *, variable: str) -> str:
    if _INTEGRATION.fullmatch(value) is None or value.casefold() == "geolocation":
        raise SettingsError(f"{variable} contains an invalid integration name")
    return value


def _mqtt_username(value: str) -> str:
    if len(value) > 256 or any(ord(character) < 0x20 for character in value):
        raise SettingsError("OPERATOR_ECN_MQTT_USERNAME is invalid")
    return value


def _basemap_url_template(environment: Mapping[str, str]) -> str | None:
    value = _value(environment, "OPERATOR_BASEMAP_URL_TEMPLATE")
    if value is None:
        return None
    if len(value) > 512 or any(ord(character) < 32 for character in value):
        raise SettingsError("OPERATOR_BASEMAP_URL_TEMPLATE is invalid")
    if any(value.count(marker) != 1 for marker in ("{z}", "{x}", "{y}")):
        raise SettingsError(
            "OPERATOR_BASEMAP_URL_TEMPLATE must contain {z}, {x}, and {y} exactly once"
        )
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise SettingsError("OPERATOR_BASEMAP_URL_TEMPLATE is invalid") from None
    is_local_path = (
        value.startswith("/")
        and not value.startswith("//")
        and parsed.scheme == ""
        and parsed.netloc == ""
    )
    is_https = (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
    )
    if is_https:
        _basemap_https_origin(value)
    if (
        not (is_local_path or is_https)
        or parsed.query
        or parsed.fragment
        or ".." in parsed.path.split("/")
    ):
        raise SettingsError(
            "OPERATOR_BASEMAP_URL_TEMPLATE must be HTTPS or a root-relative local path"
        )
    return value


def _basemap_https_origin(value: str) -> str:
    """Return one normalized CSP source for an already HTTPS tile template."""

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise SettingsError("OPERATOR_BASEMAP_URL_TEMPLATE is invalid") from None
    if (
        parsed.scheme != "https"
        or hostname is None
        or not hostname.isascii()
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise SettingsError("OPERATOR_BASEMAP_URL_TEMPLATE is invalid")
    try:
        ip_address(hostname)
    except ValueError:
        labels = hostname.split(".")
        if len(hostname) > 253 or any(_DNS_LABEL.fullmatch(label) is None for label in labels):
            raise SettingsError("OPERATOR_BASEMAP_URL_TEMPLATE is invalid") from None
    authority = f"[{hostname}]" if ":" in hostname else hostname.casefold()
    if port is not None:
        authority = f"{authority}:{port}"
    return f"https://{authority}"


def _basemap_attribution(
    environment: Mapping[str, str], *, basemap_url_template: str | None
) -> str:
    value = _value(environment, "OPERATOR_BASEMAP_ATTRIBUTION")
    if value is None:
        if basemap_url_template is None:
            return "Offline graticule · WGS 84"
        raise SettingsError(
            "OPERATOR_BASEMAP_ATTRIBUTION is required when a basemap URL is configured"
        )
    if (
        len(value) > 160
        or any(ord(character) < 32 for character in value)
        or any(character in value for character in "<>&")
    ):
        raise SettingsError("OPERATOR_BASEMAP_ATTRIBUTION is invalid")
    return value


@dataclass(frozen=True, slots=True)
class OperatorSettings:
    """Validated settings with bounded runtime limits and no embedded secrets."""

    mode: OperatorMode
    client_integration: str
    integrations: tuple[str, ...]
    categories: tuple[EntityCategory, ...]
    wire_format: WireFormat
    tasking_enabled: bool
    commands_file: Path | None
    allowed_origins: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    task_entity_allowlist: frozenset[UUID]
    maximum_entities: int
    maximum_browser_clients: int
    browser_queue_size: int
    diagnostic_limit: int
    task_history_limit: int
    prepared_task_limit: int
    event_buffer_size: int
    stale_after_seconds: float
    prepare_ttl_seconds: float
    synthetic_period_seconds: float
    basemap_url_template: str | None
    basemap_attribution: str
    host: str | None = None
    mqtt_port: int | None = None
    auth_profile: OperatorAuthProfile | None = None
    ca_certificate: Path | None = None
    client_certificate: Path | None = None
    client_key: Path | None = None
    client_key_password: SecretStr | None = None
    mqtt_username: str | None = None
    bearer_token: SecretStr | None = None
    profile_config: ECNConfig | None = field(default=None, repr=False)

    @property
    def basemap_origin(self) -> str | None:
        """Exact external image origin admitted by the browser CSP, if any."""

        template = self.basemap_url_template
        if template is None or template.startswith("/"):
            return None
        try:
            return _basemap_https_origin(template)
        except SettingsError:
            # Direct dataclass construction can bypass ``from_env``. The browser
            # boundary remains fail-closed even for that unsupported path.
            return None

    @classmethod
    def from_env(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        application_root: Path | None = None,
    ) -> OperatorSettings:
        values = os.environ if environment is None else environment
        root = (application_root or Path.cwd()).resolve()
        profile_name = _value(values, "ECN_PROFILE")
        configured_mode = _value(values, "OPERATOR_MODE")
        if profile_name is not None and configured_mode == OperatorMode.MOCK:
            raise SettingsError("ECN_PROFILE cannot be used with OPERATOR_MODE=mock")
        try:
            mode = OperatorMode(
                configured_mode
                or (OperatorMode.LIVE if profile_name is not None else OperatorMode.MOCK)
            )
        except ValueError:
            raise SettingsError("OPERATOR_MODE must be live or mock") from None

        profile_config: ECNConfig | None = None
        if mode is OperatorMode.LIVE and profile_name is not None:
            try:
                profile_config = load_config(profile=profile_name, environment=values)
            except ConfigurationError:
                raise SettingsError(
                    "the selected ECN profile cannot be used; run picogrid-ecn doctor"
                ) from None

        integrations = tuple(
            _integration(item, variable="OPERATOR_ECN_INTEGRATION_ALLOWLIST")
            for item in _csv(values, "OPERATOR_ECN_INTEGRATION_ALLOWLIST")
        )
        if len(integrations) > 16:
            raise SettingsError("OPERATOR_ECN_INTEGRATION_ALLOWLIST exceeds 16 entries")

        raw_categories = _csv(values, "OPERATOR_ECN_CATEGORY_ALLOWLIST")
        categories: list[EntityCategory] = []
        for item in raw_categories:
            try:
                category = EntityCategory(item.upper())
            except ValueError:
                raise SettingsError(
                    "OPERATOR_ECN_CATEGORY_ALLOWLIST contains an unsupported category"
                ) from None
            if category is EntityCategory.OTHER:
                raise SettingsError("OPERATOR_ECN_CATEGORY_ALLOWLIST cannot contain OTHER")
            if category not in categories:
                categories.append(category)

        if profile_config is None:
            try:
                wire_format = WireFormat(_value(values, "OPERATOR_ECN_WIRE_FORMAT") or "json")
            except ValueError:
                raise SettingsError("OPERATOR_ECN_WIRE_FORMAT must be json or protobuf") from None
            client_integration = _integration(
                _value(values, "OPERATOR_ECN_CLIENT_INTEGRATION") or "operator-console",
                variable="OPERATOR_ECN_CLIENT_INTEGRATION",
            )
        else:
            if _value(values, "OPERATOR_ECN_CLIENT_INTEGRATION") is not None:
                raise SettingsError(
                    "OPERATOR_ECN_CLIENT_INTEGRATION cannot override an ECN profile"
                )
            if _value(values, "OPERATOR_ECN_WIRE_FORMAT") is not None:
                raise SettingsError("OPERATOR_ECN_WIRE_FORMAT cannot override an ECN profile")
            wire_format = profile_config.wire_format
            client_integration = str(profile_config.integration_name)
        basemap_url_template = _basemap_url_template(values)
        commands_value = _value(values, "OPERATOR_COMMANDS_FILE")
        commands_file = None
        if commands_value is not None:
            commands_file = Path(commands_value)
            if not commands_file.is_absolute():
                commands_file = root / commands_file

        tasking_enabled = _boolean(values, "OPERATOR_TASKING_ENABLED", default=False)
        if tasking_enabled and commands_file is None:
            raise SettingsError("OPERATOR_COMMANDS_FILE is required when tasking is enabled")
        task_entity_allowlist = _uuid_allowlist(values, "OPERATOR_TASK_ENTITY_ALLOWLIST")
        if tasking_enabled and not task_entity_allowlist:
            raise SettingsError(
                "OPERATOR_TASK_ENTITY_ALLOWLIST is required when tasking is enabled"
            )

        raw_origins = _csv(values, "OPERATOR_ALLOWED_ORIGINS", required=False) or (
            "http://127.0.0.1:4173",
            "http://localhost:4173",
            "http://127.0.0.1:8080",
            "http://localhost:8080",
        )
        raw_hosts = _csv(values, "OPERATOR_ALLOWED_HOSTS", required=False) or (
            "127.0.0.1",
            "localhost",
            "testserver",
        )

        common: _CommonSettings = {
            "mode": mode,
            "client_integration": client_integration,
            "integrations": integrations,
            "categories": tuple(categories),
            "wire_format": wire_format,
            "tasking_enabled": tasking_enabled,
            "commands_file": commands_file,
            "allowed_origins": tuple(_origin(value) for value in raw_origins),
            "allowed_hosts": tuple(_host(value) for value in raw_hosts),
            "task_entity_allowlist": task_entity_allowlist,
            "maximum_entities": _integer(
                values, "OPERATOR_MAX_ENTITIES", default=256, minimum=16, maximum=2_000
            ),
            "maximum_browser_clients": _integer(
                values, "OPERATOR_MAX_BROWSER_CLIENTS", default=8, minimum=1, maximum=64
            ),
            "browser_queue_size": _integer(
                values, "OPERATOR_BROWSER_QUEUE_SIZE", default=16, minimum=2, maximum=256
            ),
            "diagnostic_limit": _integer(
                values, "OPERATOR_DIAGNOSTIC_LIMIT", default=128, minimum=16, maximum=1_000
            ),
            "task_history_limit": _integer(
                values, "OPERATOR_TASK_HISTORY_LIMIT", default=64, minimum=8, maximum=512
            ),
            "prepared_task_limit": _integer(
                values, "OPERATOR_PREPARED_TASK_LIMIT", default=32, minimum=1, maximum=128
            ),
            "event_buffer_size": _integer(
                values, "OPERATOR_EVENT_BUFFER_SIZE", default=128, minimum=8, maximum=2_048
            ),
            "stale_after_seconds": _number(
                values, "OPERATOR_STALE_AFTER_SECONDS", default=30.0, minimum=1.0, maximum=3_600
            ),
            "prepare_ttl_seconds": _number(
                values, "OPERATOR_PREPARE_TTL_SECONDS", default=30.0, minimum=5.0, maximum=120
            ),
            "synthetic_period_seconds": _number(
                values, "OPERATOR_SYNTHETIC_PERIOD_SECONDS", default=0.75, minimum=0.1, maximum=10
            ),
            "basemap_url_template": basemap_url_template,
            "basemap_attribution": _basemap_attribution(
                values, basemap_url_template=basemap_url_template
            ),
        }

        if mode is OperatorMode.MOCK:
            return cls(**common)

        if profile_config is not None:
            return cls(
                **common,
                host=profile_config.host,
                mqtt_port=profile_config.mqtt_port,
                auth_profile=(
                    OperatorAuthProfile.MTLS
                    if isinstance(profile_config.auth, MTLSAuth)
                    else OperatorAuthProfile.BEARER
                ),
                profile_config=profile_config,
            )

        try:
            auth_profile = OperatorAuthProfile(
                _value(values, "OPERATOR_ECN_AUTH") or OperatorAuthProfile.MTLS
            )
        except ValueError:
            raise SettingsError("OPERATOR_ECN_AUTH must be mtls or bearer") from None

        host = _required(values, "OPERATOR_ECN_HOST")
        mqtt_port = _integer(
            values, "OPERATOR_ECN_MQTT_PORT", default=None, minimum=1, maximum=65_535
        )
        ca_certificate = Path(_required(values, "OPERATOR_ECN_CA_CERT"))
        mtls_variables = (
            "OPERATOR_ECN_CLIENT_CERT",
            "OPERATOR_ECN_CLIENT_KEY",
            "OPERATOR_ECN_CLIENT_KEY_PASSWORD",
        )
        bearer_variables = (
            "OPERATOR_ECN_MQTT_USERNAME",
            "OPERATOR_ECN_BEARER_TOKEN",
        )

        if auth_profile is OperatorAuthProfile.MTLS:
            if any(_value(values, name) is not None for name in bearer_variables):
                raise SettingsError(
                    "bearer authentication variables cannot be set when OPERATOR_ECN_AUTH=mtls"
                )
            password = values.get("OPERATOR_ECN_CLIENT_KEY_PASSWORD")
            return cls(
                **common,
                host=host,
                mqtt_port=mqtt_port,
                auth_profile=auth_profile,
                ca_certificate=ca_certificate,
                client_certificate=Path(_required(values, "OPERATOR_ECN_CLIENT_CERT")),
                client_key=Path(_required(values, "OPERATOR_ECN_CLIENT_KEY")),
                client_key_password=SecretStr(password) if password else None,
            )

        if any(_value(values, name) is not None for name in mtls_variables):
            raise SettingsError(
                "mTLS authentication variables cannot be set when OPERATOR_ECN_AUTH=bearer"
            )
        return cls(
            **common,
            host=host,
            mqtt_port=mqtt_port,
            auth_profile=auth_profile,
            ca_certificate=ca_certificate,
            mqtt_username=_mqtt_username(_required(values, "OPERATOR_ECN_MQTT_USERNAME")),
            bearer_token=SecretStr(_required(values, "OPERATOR_ECN_BEARER_TOKEN")),
        )

    def live_client_config(self) -> ECNConfig:
        """Build a verified-TLS live configuration for the selected auth profile."""

        if self.mode is not OperatorMode.LIVE:
            raise SettingsError("live client configuration is unavailable in mock mode")
        if self.profile_config is not None:
            if (
                not self.profile_config.tls.enabled
                or not self.profile_config.tls.verify
                or self.profile_config.allow_insecure
            ):
                raise SettingsError("live ECN profiles require verified TLS")
            return self.profile_config.model_copy(
                update={"watcher_buffer_size": self.event_buffer_size}
            )
        assert self.host is not None
        assert self.mqtt_port is not None
        assert self.auth_profile is not None
        assert self.ca_certificate is not None
        auth: MTLSAuth | BearerTokenAuth
        if self.auth_profile is OperatorAuthProfile.MTLS:
            assert self.client_certificate is not None
            assert self.client_key is not None
            auth = MTLSAuth(
                client_certificate=CertificateMaterial(path=self.client_certificate),
                client_key=PrivateKeyMaterial(
                    path=self.client_key,
                    password=self.client_key_password,
                ),
            )
        else:
            assert self.mqtt_username is not None
            assert self.bearer_token is not None
            auth = BearerTokenAuth(
                username=self.mqtt_username,
                token=self.bearer_token,
            )
        return ECNConfig(
            host=self.host,
            mqtt_port=self.mqtt_port,
            integration_name=self.client_integration,
            auth=auth,
            tls=TLSConfig(
                enabled=True,
                verify=True,
                ca_certificate=CertificateMaterial(path=self.ca_certificate),
            ),
            wire_format=self.wire_format,
            watcher_buffer_size=self.event_buffer_size,
        )


__all__ = ["OperatorAuthProfile", "OperatorMode", "OperatorSettings", "SettingsError"]
