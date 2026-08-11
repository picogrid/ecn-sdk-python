# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
import math
import traceback
from pathlib import Path
from uuid import uuid4

import pytest
from picogrid_ecn_client import (
    BearerTokenAuth,
    EntityCategory,
    MTLSAuth,
    TaskMode,
    WireFormat,
)
from picogrid_ecn_client._profiles import save_profile

from operator_app import __main__ as operator_main
from operator_app.commands import CommandCatalog, CommandPolicyError
from operator_app.settings import (
    OperatorAuthProfile,
    OperatorMode,
    OperatorSettings,
    SettingsError,
)


def _base_environment() -> dict[str, str]:
    return {
        "OPERATOR_MODE": "mock",
        "OPERATOR_ECN_INTEGRATION_ALLOWLIST": "mock-sensor,mock-target",
        "OPERATOR_ECN_CATEGORY_ALLOWLIST": "TRACK,DETECTION,DEVICE",
    }


def test_mock_defaults_are_read_only_and_bounded() -> None:
    settings = OperatorSettings.from_env(_base_environment())

    assert settings.mode is OperatorMode.MOCK
    assert settings.tasking_enabled is False
    assert settings.integrations == ("mock-sensor", "mock-target")
    assert settings.maximum_entities == 256
    assert "http://127.0.0.1:8080" in settings.allowed_origins
    assert settings.basemap_url_template is None
    assert settings.basemap_attribution == "Offline graticule · WGS 84"


@pytest.mark.parametrize(
    "template",
    [
        "http://tiles.example.invalid/{z}/{x}/{y}.png",
        "https://tiles.example.invalid/{z}/{x}.png",
        "https://" + "user:secret@" + "tiles.example.invalid/{z}/{x}/{y}.png",
        "https://tiles.example.invalid/{z}/{x}/{y}.png?token=secret",
        "https://tiles.example.invalid';script-src *;/{z}/{x}/{y}.png",
        "https://tiles.example.invalid:99999/{z}/{x}/{y}.png",
        "/tiles/../outside/{z}/{x}/{y}.png",
        "//tiles.example.invalid/{z}/{x}/{y}.png",
    ],
)
def test_basemap_template_rejects_unsafe_or_unbounded_sources(template: str) -> None:
    environment = _base_environment() | {
        "OPERATOR_BASEMAP_URL_TEMPLATE": template,
        "OPERATOR_BASEMAP_ATTRIBUTION": "Example map data",
    }

    with pytest.raises(SettingsError):
        OperatorSettings.from_env(environment)


@pytest.mark.parametrize(
    "template",
    [
        "https://tiles.example.invalid/{z}/{x}/{y}.png",
        "/tiles/{z}/{x}/{y}.png",
    ],
)
def test_basemap_template_requires_plain_text_attribution(template: str) -> None:
    environment = _base_environment() | {"OPERATOR_BASEMAP_URL_TEMPLATE": template}

    with pytest.raises(SettingsError, match="OPERATOR_BASEMAP_ATTRIBUTION"):
        OperatorSettings.from_env(environment)

    environment["OPERATOR_BASEMAP_ATTRIBUTION"] = "Authorized map data"
    settings = OperatorSettings.from_env(environment)
    assert settings.basemap_url_template == template
    assert settings.basemap_attribution == "Authorized map data"
    assert settings.basemap_origin == (
        "https://tiles.example.invalid" if template.startswith("https://") else None
    )

    environment["OPERATOR_BASEMAP_ATTRIBUTION"] = "<a>unsafe markup</a>"
    with pytest.raises(SettingsError, match="OPERATOR_BASEMAP_ATTRIBUTION"):
        OperatorSettings.from_env(environment)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OPERATOR_ECN_INTEGRATION_ALLOWLIST", "x"),
        ("OPERATOR_ECN_INTEGRATION_ALLOWLIST", "geolocation"),
        ("OPERATOR_ECN_CATEGORY_ALLOWLIST", "OTHER"),
        ("OPERATOR_ECN_CATEGORY_ALLOWLIST", "not-a-category"),
    ],
)
def test_invalid_or_reserved_scope_fails_closed(name: str, value: str) -> None:
    environment = _base_environment()
    environment[name] = value

    with pytest.raises(SettingsError):
        OperatorSettings.from_env(environment)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OPERATOR_ECN_INTEGRATION_ALLOWLIST", None),
        ("OPERATOR_ECN_INTEGRATION_ALLOWLIST", "   "),
        ("OPERATOR_ECN_CATEGORY_ALLOWLIST", None),
        ("OPERATOR_ECN_CATEGORY_ALLOWLIST", "   "),
    ],
)
def test_live_mode_requires_explicit_observation_allowlists(
    name: str,
    value: str | None,
) -> None:
    environment = _base_environment() | {
        "OPERATOR_MODE": "live",
        "OPERATOR_ECN_HOST": "mqtt.example.invalid",
        "OPERATOR_ECN_MQTT_PORT": "8883",
        "OPERATOR_ECN_CA_CERT": "/external/ca.crt",
        "OPERATOR_ECN_CLIENT_CERT": "/external/client.crt",
        "OPERATOR_ECN_CLIENT_KEY": "/external/client.key",
    }
    if value is None:
        environment.pop(name)
    else:
        environment[name] = value

    with pytest.raises(SettingsError, match=name):
        OperatorSettings.from_env(environment)


def test_live_mode_builds_verified_mtls_configuration() -> None:
    environment = _base_environment() | {
        "OPERATOR_MODE": "live",
        "OPERATOR_ECN_HOST": "mqtt.example.invalid",
        "OPERATOR_ECN_MQTT_PORT": "8883",
        "OPERATOR_ECN_CA_CERT": "/external/ca.crt",
        "OPERATOR_ECN_CLIENT_CERT": "/external/client.crt",
        "OPERATOR_ECN_CLIENT_KEY": "/external/client.key",
    }

    configuration = OperatorSettings.from_env(environment).live_client_config()

    assert configuration.host == "mqtt.example.invalid"
    assert configuration.tls.enabled is True
    assert configuration.tls.verify is True
    assert isinstance(configuration.auth, MTLSAuth)
    assert configuration.allow_insecure is False


def test_live_mode_uses_named_sdk_profile_without_weakening_operator_allowlists(
    tmp_path: Path,
) -> None:
    environment = {
        "XDG_CONFIG_HOME": str(tmp_path / "configuration"),
        "ECN_PROFILE": "operator",
        "OPERATOR_ECN_INTEGRATION_ALLOWLIST": "sensor-a,sensor-b",
        "OPERATOR_ECN_CATEGORY_ALLOWLIST": "TRACK,DETECTION",
        "OPERATOR_EVENT_BUFFER_SIZE": "64",
    }
    save_profile(
        "operator",
        {
            "host": "mqtt.example.invalid",
            "integration_name": "operator-view",
            "auth": "mtls",
            "ca_certificate": "/external/ca.crt",
            "client_certificate": "/external/client.crt",
            "client_key": "/external/client.key",
            "wire_format": "protobuf",
        },
        environment,
    )

    settings = OperatorSettings.from_env(environment)
    configuration = settings.live_client_config()

    assert settings.mode is OperatorMode.LIVE
    assert settings.integrations == ("sensor-a", "sensor-b")
    assert settings.categories == (EntityCategory.TRACK, EntityCategory.DETECTION)
    assert settings.client_integration == "operator-view"
    assert configuration.host == "mqtt.example.invalid"
    assert configuration.mqtt_port == 8883
    assert configuration.wire_format is WireFormat.PROTOBUF
    assert configuration.watcher_buffer_size == 64
    assert isinstance(configuration.auth, MTLSAuth)


@pytest.mark.parametrize("tasking_enabled", [False, True])
def test_named_live_profile_rejects_mock_only_insecure_tls(
    tmp_path: Path,
    tasking_enabled: bool,
) -> None:
    entity_id = uuid4()
    environment = {
        "XDG_CONFIG_HOME": str(tmp_path / "configuration"),
        "ECN_PROFILE": "operator",
        "ECN_ALLOW_INSECURE": "1",
        "ECN_BEARER_TOKEN": "synthetic-secret-token",
        "OPERATOR_ECN_INTEGRATION_ALLOWLIST": "sensor-a",
        "OPERATOR_ECN_CATEGORY_ALLOWLIST": "TRACK",
    }
    if tasking_enabled:
        environment.update(
            {
                "OPERATOR_TASKING_ENABLED": "true",
                "OPERATOR_COMMANDS_FILE": "config/commands.example.json",
                "OPERATOR_TASK_ENTITY_ALLOWLIST": str(entity_id),
            }
        )
    save_profile(
        "operator",
        {
            "host": "localhost",
            "integration_name": "operator-view",
            "auth": "bearer",
            "mqtt_username": "integration-identity",
        },
        environment,
    )

    settings = OperatorSettings.from_env(
        environment,
        application_root=Path(__file__).resolve().parents[2],
    )

    assert settings.tasking_enabled is tasking_enabled
    with pytest.raises(SettingsError, match="live ECN profiles require verified TLS"):
        settings.live_client_config()


def test_named_profile_rejects_legacy_operator_connection_overrides(tmp_path: Path) -> None:
    environment = {
        "XDG_CONFIG_HOME": str(tmp_path / "configuration"),
        "ECN_PROFILE": "operator",
        "OPERATOR_ECN_INTEGRATION_ALLOWLIST": "sensor-a",
        "OPERATOR_ECN_CATEGORY_ALLOWLIST": "TRACK",
    }
    save_profile(
        "operator",
        {
            "host": "mqtt.example.invalid",
            "integration_name": "operator-view",
            "auth": "mtls",
            "client_certificate": "/external/client.crt",
            "client_key": "/external/client.key",
        },
        environment,
    )

    with pytest.raises(SettingsError, match="cannot override an ECN profile"):
        OperatorSettings.from_env(environment | {"OPERATOR_ECN_CLIENT_INTEGRATION": "other-client"})


def test_named_profile_rejects_explicit_mock_mode() -> None:
    with pytest.raises(SettingsError, match="ECN_PROFILE cannot be used with OPERATOR_MODE=mock"):
        OperatorSettings.from_env(
            {
                "ECN_PROFILE": "operator",
                "OPERATOR_MODE": "mock",
            }
        )


def test_operator_entry_point_selects_profile_or_demo_without_persisting_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocations: list[dict[str, object]] = []
    monkeypatch.setattr(
        operator_main.uvicorn, "run", lambda *args, **kwargs: invocations.append(kwargs)
    )
    environment: dict[str, str] = {}
    monkeypatch.setattr(operator_main.os, "environ", environment)

    operator_main.main(["--profile", "operator"])
    assert environment["ECN_PROFILE"] == "operator"
    assert environment["OPERATOR_MODE"] == "live"
    assert invocations[-1]["host"] == "127.0.0.1"

    environment.update(
        {
            "OPERATOR_COMMANDS_FILE": "/external/commands.json",
            "OPERATOR_TASKING_ENABLED": "true",
            "OPERATOR_TASK_ENTITY_ALLOWLIST": str(uuid4()),
            "OPERATOR_BASEMAP_URL_TEMPLATE": "https://tiles.example.invalid/{z}/{x}/{y}.png",
            "OPERATOR_BASEMAP_ATTRIBUTION": "Remote map data",
        }
    )
    operator_main.main(["--demo"])
    assert "ECN_PROFILE" not in environment
    assert environment["OPERATOR_MODE"] == "mock"
    assert environment["OPERATOR_ECN_INTEGRATION_ALLOWLIST"] == "mock-sensor,mock-target"
    assert environment["OPERATOR_ECN_CATEGORY_ALLOWLIST"] == "TRACK,DETECTION,DEVICE"
    assert environment["OPERATOR_TASKING_ENABLED"] == "false"
    assert "OPERATOR_COMMANDS_FILE" not in environment
    assert "OPERATOR_TASK_ENTITY_ALLOWLIST" not in environment
    assert "OPERATOR_BASEMAP_URL_TEMPLATE" not in environment
    assert "OPERATOR_BASEMAP_ATTRIBUTION" not in environment

    demo_settings = OperatorSettings.from_env(environment)
    assert demo_settings.basemap_url_template is None
    assert demo_settings.basemap_attribution == "Offline graticule · WGS 84"


@pytest.mark.parametrize(
    ("host", "container_bind", "accepted"),
    [
        ("127.0.0.1", "false", True),
        ("0.0.0.0", "false", False),
        ("0.0.0.0", "true", True),
        ("operator.example.invalid", "true", False),
    ],
)
def test_operator_entry_point_enforces_loopback_or_guarded_container_bind(
    host: str,
    container_bind: str,
    accepted: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocations: list[dict[str, object]] = []
    monkeypatch.setattr(
        operator_main.uvicorn, "run", lambda *args, **kwargs: invocations.append(kwargs)
    )
    monkeypatch.setattr(
        operator_main.os,
        "environ",
        {
            "OPERATOR_HTTP_BIND": host,
            "OPERATOR_ALLOW_CONTAINER_BIND": container_bind,
        },
    )

    if accepted:
        operator_main.main([])
        assert len(invocations) == 1
        assert invocations[0]["host"] == host
    else:
        with pytest.raises(RuntimeError, match="must be loopback"):
            operator_main.main([])
        assert invocations == []


def test_live_mode_requires_authentication_profile_listener_port() -> None:
    environment = _base_environment() | {
        "OPERATOR_MODE": "live",
        "OPERATOR_ECN_HOST": "mqtt.example.invalid",
        "OPERATOR_ECN_CA_CERT": "/external/ca.crt",
        "OPERATOR_ECN_CLIENT_CERT": "/external/client.crt",
        "OPERATOR_ECN_CLIENT_KEY": "/external/client.key",
    }

    with pytest.raises(SettingsError, match="OPERATOR_ECN_MQTT_PORT"):
        OperatorSettings.from_env(environment)


def test_live_mode_builds_secret_safe_bearer_configuration() -> None:
    token = "bearer-token-secret-canary"
    environment = _base_environment() | {
        "OPERATOR_MODE": "live",
        "OPERATOR_ECN_AUTH": "bearer",
        "OPERATOR_ECN_HOST": "mqtt.example.invalid",
        "OPERATOR_ECN_MQTT_PORT": "8883",
        "OPERATOR_ECN_CA_CERT": "/external/ca.crt",
        "OPERATOR_ECN_MQTT_USERNAME": "deployment-issued-username",
        "OPERATOR_ECN_BEARER_TOKEN": token,
    }

    settings = OperatorSettings.from_env(environment)
    configuration = settings.live_client_config()

    assert settings.auth_profile is OperatorAuthProfile.BEARER
    assert settings.client_certificate is None
    assert settings.client_key is None
    assert configuration.tls.enabled is True
    assert configuration.tls.verify is True
    assert isinstance(configuration.auth, BearerTokenAuth)
    assert configuration.auth.username == "deployment-issued-username"
    assert configuration.auth.token is not None
    assert configuration.auth.token.get_secret_value() == token
    assert configuration.allow_insecure is False
    assert token not in repr(settings)
    assert token not in repr(configuration)


@pytest.mark.parametrize(
    "environment",
    [
        {
            "OPERATOR_ECN_AUTH": "mtls",
            "OPERATOR_ECN_CLIENT_CERT": "/external/client.crt",
            "OPERATOR_ECN_CLIENT_KEY": "/external/client.key",
            "OPERATOR_ECN_MQTT_USERNAME": "deployment-issued-username",
            "OPERATOR_ECN_BEARER_TOKEN": "bearer-secret-canary",
        },
        {
            "OPERATOR_ECN_AUTH": "bearer",
            "OPERATOR_ECN_CLIENT_CERT": "/external/client.crt",
            "OPERATOR_ECN_CLIENT_KEY": "/external/client.key",
            "OPERATOR_ECN_MQTT_USERNAME": "deployment-issued-username",
            "OPERATOR_ECN_BEARER_TOKEN": "bearer-secret-canary",
        },
    ],
)
def test_live_authentication_profiles_are_mutually_exclusive(
    environment: dict[str, str],
) -> None:
    values = _base_environment() | {
        "OPERATOR_MODE": "live",
        "OPERATOR_ECN_HOST": "mqtt.example.invalid",
        "OPERATOR_ECN_MQTT_PORT": "8883",
        "OPERATOR_ECN_CA_CERT": "/external/ca.crt",
    }
    values.update(environment)

    with pytest.raises(SettingsError) as captured:
        OperatorSettings.from_env(values)

    assert "bearer-secret-canary" not in "".join(traceback.format_exception(captured.value))


@pytest.mark.parametrize(
    "missing",
    ["OPERATOR_ECN_MQTT_USERNAME", "OPERATOR_ECN_BEARER_TOKEN"],
)
def test_live_bearer_requires_explicit_username_and_token(missing: str) -> None:
    environment = _base_environment() | {
        "OPERATOR_MODE": "live",
        "OPERATOR_ECN_AUTH": "bearer",
        "OPERATOR_ECN_HOST": "mqtt.example.invalid",
        "OPERATOR_ECN_MQTT_PORT": "8883",
        "OPERATOR_ECN_CA_CERT": "/external/ca.crt",
        "OPERATOR_ECN_MQTT_USERNAME": "deployment-issued-username",
        "OPERATOR_ECN_BEARER_TOKEN": "bearer-secret-canary",
    }
    environment.pop(missing)

    with pytest.raises(SettingsError, match=missing):
        OperatorSettings.from_env(environment)


def test_live_mode_requires_complete_mtls_material_without_path_disclosure() -> None:
    environment = _base_environment() | {
        "OPERATOR_MODE": "live",
        "OPERATOR_ECN_HOST": "mqtt.example.invalid",
        "OPERATOR_ECN_MQTT_PORT": "8883",
    }

    with pytest.raises(SettingsError) as captured:
        OperatorSettings.from_env(environment)

    assert "required environment variable" in str(captured.value)
    assert "/" not in str(captured.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OPERATOR_MODE", "mode-secret-canary"),
        ("OPERATOR_ECN_CATEGORY_ALLOWLIST", "category-secret-canary"),
        ("OPERATOR_ECN_WIRE_FORMAT", "wire-secret-canary"),
        ("OPERATOR_MAX_ENTITIES", "integer-secret-canary"),
        ("OPERATOR_STALE_AFTER_SECONDS", "number-secret-canary"),
        ("OPERATOR_TASK_ENTITY_ALLOWLIST", "uuid-secret-canary"),
        ("OPERATOR_ALLOWED_ORIGINS", "invalid-origin-secret-canary"),
        ("OPERATOR_BASEMAP_URL_TEMPLATE", "basemap-secret-canary"),
        ("OPERATOR_BASEMAP_ATTRIBUTION", "<basemap-secret-canary>"),
    ],
)
def test_invalid_settings_tracebacks_do_not_disclose_raw_values(name: str, value: str) -> None:
    environment = _base_environment() | {name: value}

    with pytest.raises(SettingsError) as captured:
        OperatorSettings.from_env(environment)

    rendered = "".join(traceback.format_exception(captured.value))
    assert value not in rendered


def test_command_policy_traceback_does_not_disclose_credential_bearing_path(
    tmp_path: Path,
) -> None:
    canary = "command-policy-secret-path-canary"
    policy = tmp_path / f"{canary}.json"

    with pytest.raises(CommandPolicyError) as captured:
        CommandCatalog.load(policy)

    rendered = "".join(traceback.format_exception(captured.value))
    assert canary not in rendered


def test_tasking_requires_an_explicit_command_file() -> None:
    environment = _base_environment() | {"OPERATOR_TASKING_ENABLED": "true"}

    with pytest.raises(SettingsError, match="OPERATOR_COMMANDS_FILE"):
        OperatorSettings.from_env(environment)


@pytest.mark.parametrize("mode", ["mock", "live"])
def test_every_tasking_mode_requires_exact_canonical_entity_uuid_allowlist(
    mode: str, tmp_path: Path
) -> None:
    root = Path(__file__).resolve().parents[2]
    environment = _base_environment() | {
        "OPERATOR_MODE": mode,
        "OPERATOR_TASKING_ENABLED": "true",
        "OPERATOR_COMMANDS_FILE": "config/commands.example.json",
    }
    if mode == "live":
        environment.update(
            {
                "OPERATOR_ECN_HOST": "mqtt.example.invalid",
                "OPERATOR_ECN_MQTT_PORT": "8883",
                "OPERATOR_ECN_CA_CERT": str(tmp_path / "material-a.pem"),
                "OPERATOR_ECN_CLIENT_CERT": str(tmp_path / "material-b.pem"),
                "OPERATOR_ECN_CLIENT_KEY": str(tmp_path / "material-c.pem"),
            }
        )

    with pytest.raises(SettingsError, match="OPERATOR_TASK_ENTITY_ALLOWLIST"):
        OperatorSettings.from_env(environment, application_root=root)

    entity_id = uuid4()
    environment["OPERATOR_TASK_ENTITY_ALLOWLIST"] = str(entity_id)
    settings = OperatorSettings.from_env(environment, application_root=root)
    assert settings.task_entity_allowlist == frozenset({entity_id})

    environment["OPERATOR_TASK_ENTITY_ALLOWLIST"] = str(entity_id).upper()
    with pytest.raises(SettingsError, match="canonical lowercase UUID"):
        OperatorSettings.from_env(environment, application_root=root)


def _write_command_file(path: Path, schema: dict[str, object]) -> None:
    path.write_text(
        json.dumps(
            {
                "commands": {
                    "echo": {
                        "label": "Echo",
                        "description": "Synthetic echo",
                        "allowed_integrations": ["mock-target"],
                        "request_schema": schema,
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def test_command_catalog_validates_allowlist_and_closed_payload(tmp_path: Path) -> None:
    policy = tmp_path / "commands.json"
    _write_command_file(
        policy,
        {
            "type": "object",
            "properties": {"message": {"type": "string", "minLength": 1, "maxLength": 12}},
            "required": ["message"],
            "additionalProperties": False,
        },
    )
    catalog = CommandCatalog.load(policy)

    request = catalog.validate(
        command_name="echo",
        integration="mock-target",
        payload={"message": "synthetic"},
    )

    assert request.model_dump() == {"message": "synthetic"}
    assert catalog.mode_for("echo") is TaskMode.COMPLETE
    with pytest.raises(CommandPolicyError, match="target integration"):
        catalog.validate(
            command_name="echo",
            integration="mock-sensor",
            payload={"message": "synthetic"},
        )
    with pytest.raises(CommandPolicyError, match="does not match"):
        catalog.validate(
            command_name="echo",
            integration="mock-target",
            payload={"message": "synthetic", "unexpected": True},
        )


def test_command_catalog_allows_only_explicit_complete_or_acknowledgment_modes(
    tmp_path: Path,
) -> None:
    policy = tmp_path / "commands.json"
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"message": {"type": "string", "minLength": 1, "maxLength": 12}},
        "required": ["message"],
        "additionalProperties": False,
    }
    document = {
        "commands": {
            "ack": {
                "label": "Acknowledge",
                "description": "Synthetic acknowledgment",
                "allowed_integrations": ["mock-target"],
                "mode": "acknowledgment",
                "request_schema": schema,
            }
        }
    }
    policy.write_text(json.dumps(document), encoding="utf-8")
    catalog = CommandCatalog.load(policy)

    assert catalog.mode_for("ack") is TaskMode.ACKNOWLEDGMENT
    assert catalog.public_inventory()[0]["mode"] == "acknowledgment"
    assert catalog.registration_names("mock-target") == ("ack",)

    document["commands"]["ack"]["mode"] = "fire_and_forget"
    policy.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(CommandPolicyError, match="could not be loaded"):
        CommandCatalog.load(policy)


def test_command_catalog_rejects_unrenderable_or_referenced_schema(tmp_path: Path) -> None:
    policy = tmp_path / "commands.json"
    _write_command_file(
        policy,
        {
            "type": "object",
            "properties": {"nested": {"type": "object"}},
            "required": ["nested"],
            "additionalProperties": False,
        },
    )

    with pytest.raises(CommandPolicyError, match="could not be loaded"):
        CommandCatalog.load(policy)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_command_catalog_rejects_non_finite_task_numbers(tmp_path: Path, value: float) -> None:
    policy = tmp_path / "commands.json"
    _write_command_file(
        policy,
        {
            "type": "object",
            "properties": {"value": {"type": "number", "minimum": -10, "maximum": 10}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    catalog = CommandCatalog.load(policy)

    with pytest.raises(CommandPolicyError, match="non-finite"):
        catalog.validate(
            command_name="echo",
            integration="mock-target",
            payload={"value": value},
        )


def test_command_catalog_rejects_non_standard_constants_and_unbounded_scalars(
    tmp_path: Path,
) -> None:
    policy = tmp_path / "commands.json"
    _write_command_file(
        policy,
        {
            "type": "object",
            "properties": {
                "value": {
                    "type": "number",
                    "minimum": -10,
                    "maximum": 10,
                    "default": math.nan,
                }
            },
            "additionalProperties": False,
        },
    )
    with pytest.raises(CommandPolicyError, match="could not be loaded"):
        CommandCatalog.load(policy)
    _write_command_file(
        policy,
        {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    with pytest.raises(CommandPolicyError, match="could not be loaded"):
        CommandCatalog.load(policy)
    _write_command_file(
        policy,
        {
            "type": "object",
            "properties": {"message": {"type": "string", "maxLength": 5, "default": "too long"}},
            "additionalProperties": False,
        },
    )
    with pytest.raises(CommandPolicyError, match="could not be loaded"):
        CommandCatalog.load(policy)


def test_docker_runtime_consumes_exact_prebuilt_wheels_without_rebuilding() -> None:
    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text(encoding="utf-8")

    assert "pip install --require-hashes --only-binary=:all: -r requirements.txt" in dockerfile
    assert "ARG CLIENT_WHEEL" in dockerfile
    assert "ARG OPERATOR_WHEEL" in dockerfile
    assert "COPY wheelhouse/${CLIENT_WHEEL} /tmp/${CLIENT_WHEEL}" in dockerfile
    assert "COPY wheelhouse/${OPERATOR_WHEEL} /tmp/${OPERATOR_WHEEL}" in dockerfile
    assert 'pip install --no-deps "/tmp/${CLIENT_WHEEL}" "/tmp/${OPERATOR_WHEEL}"' in dockerfile
    assert sum(line.startswith("FROM ") for line in dockerfile.splitlines()) == 1
    assert "requirements-build.txt" not in dockerfile
    assert "pip wheel" not in dockerfile
    assert "build_backend.py" not in dockerfile
    assert "npm" not in dockerfile
