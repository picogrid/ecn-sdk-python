# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from picogrid_ecn_client import (
    AuthenticationError,
    BearerTokenAuth,
    CertificateMaterial,
    ECNConfig,
    ReconnectPolicy,
    TLSConfig,
    _cli,
)
from picogrid_ecn_client._cli import main
from picogrid_ecn_client._legion_auth import legion_system_auth_provider
from picogrid_ecn_client._profiles import load_profile, profile_path
from picogrid_ecn_client.config import load_config


def _profile_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    configuration_home = tmp_path / "configuration"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(configuration_home))
    monkeypatch.delenv("ECN_PROFILE", raising=False)
    return profile_path(dict(os.environ))


def test_configure_persists_only_nonsecret_profile_with_private_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _profile_environment(tmp_path, monkeypatch)

    assert (
        main(
            [
                "configure",
                "--profile",
                "operator",
                "--host",
                "broker.example.invalid",
                "--integration-name",
                "operator-view",
                "--auth",
                "mtls",
                "--ca-certificate",
                "/credentials/ca.crt",
                "--client-certificate",
                "/credentials/client.crt",
                "--client-key",
                "/credentials/client.key",
                "--non-interactive",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "operator" in output
    assert "8883" in output
    assert "/credentials" not in output
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    document = path.read_text(encoding="utf-8")
    assert "token" not in document.casefold()
    assert "password" not in document.casefold()


def test_configure_round_trips_and_replaces_supplied_reconnect_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _profile_environment(tmp_path, monkeypatch)
    base_arguments = [
        "configure",
        "--profile",
        "operator",
        "--host",
        "broker.example.invalid",
        "--integration-name",
        "operator-view",
        "--auth",
        "legion",
        "--non-interactive",
    ]

    assert (
        main(
            [
                *base_arguments,
                "--reconnect-initial-delay-seconds",
                "0.75",
                "--reconnect-multiplier",
                "1.5",
                "--reconnect-maximum-delay-seconds",
                "12",
                "--reconnect-stable-reset-seconds",
                "45",
                "--reconnect-maximum-attempts",
                "7",
                "--reconnect-maximum-elapsed-seconds",
                "90",
            ]
        )
        == 0
    )
    environment = {"XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"]}
    assert load_profile("operator", environment)["reconnect_policy"] == {
        "initial_delay_seconds": 0.75,
        "multiplier": 1.5,
        "maximum_delay_seconds": 12.0,
        "stable_reset_seconds": 45.0,
        "maximum_attempts": 7,
        "maximum_elapsed_seconds": 90.0,
    }
    assert load_config(
        profile="operator", environment=environment
    ).reconnect_policy.model_dump() == {
        "initial_delay_seconds": 0.75,
        "multiplier": 1.5,
        "maximum_delay_seconds": 12.0,
        "stable_reset_seconds": 45.0,
        "maximum_attempts": 7,
        "maximum_elapsed_seconds": 90.0,
    }
    stored = path.read_text(encoding="utf-8")
    assert "token" not in stored.casefold()
    assert "password" not in stored.casefold()

    assert main(base_arguments) == 0
    replaced = load_profile("operator", environment)
    assert replaced["host"] == "broker.example.invalid"
    assert "reconnect_policy" not in replaced
    assert (
        load_config(profile="operator", environment=environment).reconnect_policy
        == ReconnectPolicy()
    )


@pytest.mark.parametrize(
    "reconnect_arguments",
    [
        ["--reconnect-initial-delay-seconds", "0"],
        ["--reconnect-multiplier", "0.5"],
        ["--reconnect-maximum-delay-seconds", "nan"],
        ["--reconnect-stable-reset-seconds", "-1"],
        ["--reconnect-maximum-attempts", "0"],
        ["--reconnect-maximum-elapsed-seconds", "inf"],
        [
            "--reconnect-initial-delay-seconds",
            "2",
            "--reconnect-maximum-delay-seconds",
            "1",
        ],
    ],
)
def test_configure_rejects_invalid_reconnect_policy_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reconnect_arguments: list[str],
) -> None:
    path = _profile_environment(tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "configure",
                "--profile",
                "invalid-reconnect",
                "--host",
                "broker.example.invalid",
                "--integration-name",
                "operator-view",
                "--auth",
                "legion",
                "--non-interactive",
                *reconnect_arguments,
            ]
        )

    assert raised.value.code == 2
    assert not path.exists()


def test_configure_reconnect_parser_does_not_echo_invalid_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _profile_environment(tmp_path, monkeypatch)
    canary = "reconnect-value-canary"

    with pytest.raises(SystemExit) as raised:
        main(["configure", "--reconnect-initial-delay-seconds", canary])

    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert "invalid arguments" in error
    assert canary not in error
    assert not path.exists()


def test_configure_legion_profile_uses_bearer_port_without_persisting_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _profile_environment(tmp_path, monkeypatch)

    assert (
        main(
            [
                "configure",
                "--profile",
                "local-auth",
                "--host",
                "broker.example.invalid",
                "--integration-name",
                "operator-view",
                "--auth",
                "legion",
                "--non-interactive",
            ]
        )
        == 0
    )

    assert "8884" in capsys.readouterr().out
    document = path.read_text(encoding="utf-8")
    assert '"auth": "legion"' in document
    assert "access_token" not in document
    assert "clientSecret" not in document


def test_configure_reports_effective_environment_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _profile_environment(tmp_path, monkeypatch)
    monkeypatch.setenv("ECN_MQTT_PORT", "9443")

    assert (
        main(
            [
                "configure",
                "--profile",
                "environment-port",
                "--host",
                "broker.example.invalid",
                "--integration-name",
                "operator-view",
                "--auth",
                "legion",
                "--mqtt-port",
                "8884",
                "--non-interactive",
            ]
        )
        == 0
    )

    assert "connection port 9443" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("auth", "rejected_option"),
    [
        ("mtls", "--mqtt-username"),
        ("mtls", "--legion-auth-storage"),
        ("bearer", "--client-certificate"),
        ("bearer", "--client-key"),
        ("bearer", "--legion-auth-storage"),
        ("legion", "--client-certificate"),
        ("legion", "--client-key"),
        ("legion", "--mqtt-username"),
    ],
)
def test_configure_rejects_options_from_other_auth_profiles_before_prompt_or_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    auth: str,
    rejected_option: str,
) -> None:
    path = _profile_environment(tmp_path, monkeypatch)
    canary = "canary-auth-option-value"

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "configure",
                "--profile",
                "invalid-auth-options",
                "--auth",
                auth,
                rejected_option,
                canary,
                "--non-interactive",
            ]
        )

    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert f"{auth} authentication does not accept {rejected_option}" in error
    assert canary not in error
    assert "ECN host is required" not in error
    assert not path.exists()


@pytest.mark.parametrize(
    "host",
    [".", "..", "...", ".bad", "bad..example", "bad..", f"{'x' * 64}.example"],
)
def test_configure_rejects_unresolvable_dns_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host: str,
) -> None:
    path = _profile_environment(tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "configure",
                "--profile",
                "invalid-host",
                "--host",
                host,
                "--integration-name",
                "operator-view",
                "--auth",
                "legion",
                "--non-interactive",
            ]
        )

    assert raised.value.code == 2
    assert "DNS name or IP literal" in capsys.readouterr().err
    assert not path.exists()


def test_configure_host_input_boundary_is_applied_before_whitespace_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _profile_environment(tmp_path, monkeypatch)
    normalized_host = "a.example"
    accepted = f"{' ' * (1024 - len(normalized_host))}{normalized_host}"
    rejected = f"{' ' * (1025 - len(normalized_host))}{normalized_host}"

    assert (
        main(
            [
                "configure",
                "--profile",
                "valid-host",
                "--host",
                accepted,
                "--integration-name",
                "operator-view",
                "--auth",
                "legion",
                "--non-interactive",
            ]
        )
        == 0
    )
    document_before_rejection = path.read_text(encoding="utf-8")
    assert f'"host": "{normalized_host}"' in document_before_rejection
    capsys.readouterr()

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "configure",
                "--profile",
                "invalid-host",
                "--host",
                rejected,
                "--integration-name",
                "operator-view",
                "--auth",
                "legion",
                "--non-interactive",
            ]
        )

    assert raised.value.code == 2
    assert "DNS name or IP literal" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == document_before_rejection


@pytest.mark.parametrize("username", ["username-canary\ud800", "x" * 257])
def test_configure_rejects_invalid_mqtt_username_without_storing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    username: str,
) -> None:
    path = _profile_environment(tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "configure",
                "--profile",
                "invalid-username",
                "--host",
                "broker.example.invalid",
                "--integration-name",
                "operator-view",
                "--auth",
                "bearer",
                "--mqtt-username",
                username,
                "--non-interactive",
            ]
        )

    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert "ECN profile MQTT username is invalid" in error
    assert "username-canary" not in error
    assert not path.exists()


def test_doctor_uses_loaded_profile_without_rendering_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _profile_environment(tmp_path, monkeypatch)
    config = ECNConfig(
        host="broker.example.invalid",
        mqtt_port=8884,
        integration_name="operator-view",
        auth=BearerTokenAuth(
            username="integration-identity",
            token=SecretStr("synthetic-secret-token"),
        ),
    )
    selected_profiles: list[str | None] = []

    def load(*, profile: str | None) -> ECNConfig:
        selected_profiles.append(profile)
        return config

    monkeypatch.setattr(_cli, "_load_config", load)

    assert main(["doctor", "--profile", "bearer"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["ready"] is True
    assert [check["status"] for check in result["checks"]] == ["pass", "pass"]
    assert "synthetic-secret-token" not in json.dumps(result)
    assert selected_profiles == ["bearer"]


def test_doctor_does_not_trust_spoofed_legion_error_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _profile_environment(tmp_path, monkeypatch)
    canary = "caller-provider-secret-canary"

    async def provider() -> tuple[str, str]:
        raise AuthenticationError(canary, code="legion_credentials_missing")

    config = ECNConfig(
        host="broker.example.invalid",
        mqtt_port=8884,
        integration_name="operator-view",
        auth=BearerTokenAuth(credentials_provider=provider),
    )
    monkeypatch.setattr(_cli, "_load_config", lambda *, profile: config)

    assert main(["doctor", "--profile", "bearer"]) == 2

    rendered = capsys.readouterr().out
    assert canary not in rendered
    assert "legion-auth setup" not in rendered


def test_doctor_rejects_invalid_provider_username_without_disclosure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _profile_environment(tmp_path, monkeypatch)
    canary = "provider-username-secret-canary"
    expiry = datetime.now(UTC) + timedelta(hours=1)

    def encode(value: object) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    token = (
        f"{encode({'alg': 'RS256'})}.{encode({'exp': expiry.timestamp()})}."
        f"{base64.urlsafe_b64encode(b'synthetic-signature').decode().rstrip('=')}"
    )
    credentials = tmp_path / "legion-auth"
    credentials.mkdir(mode=0o755)
    for name, document in (
        ("oauth_config.json", {"integrationId": f"{canary}\ud800"}),
        ("access_token.json", {"access_token": token, "expires_at": expiry.isoformat()}),
    ):
        path = credentials / name
        path.write_text(json.dumps(document), encoding="utf-8")
        path.chmod(0o640)
    config = ECNConfig(
        host="broker.example.invalid",
        mqtt_port=8884,
        integration_name="operator-view",
        auth=BearerTokenAuth(credentials_provider=legion_system_auth_provider(credentials)),
    )
    monkeypatch.setattr(_cli, "_load_config", lambda *, profile: config)

    assert main(["doctor", "--profile", "bearer"]) == 2

    result = json.loads(capsys.readouterr().out)
    assert result["ready"] is False
    assert [check["status"] for check in result["checks"]] == ["pass", "fail"]
    assert result["checks"][-1]["detail"] == "credential material could not be validated"
    assert canary not in json.dumps(result)


def test_doctor_validates_tls_material_for_bearer_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _profile_environment(tmp_path, monkeypatch)
    missing_ca = tmp_path / "credential-path-canary" / "ca.crt"
    config = ECNConfig(
        host="broker.example.invalid",
        mqtt_port=8884,
        integration_name="operator-view",
        auth=BearerTokenAuth(
            username="integration-identity",
            token=SecretStr("synthetic-secret-token"),
        ),
        tls=TLSConfig(ca_certificate=CertificateMaterial(path=missing_ca)),
    )
    monkeypatch.setattr(_cli, "_load_config", lambda *, profile: config)

    assert main(["doctor", "--profile", "bearer"]) == 2

    rendered = capsys.readouterr().out
    assert "credential material could not be validated" in rendered
    assert str(missing_ca) not in rendered
    assert "synthetic-secret-token" not in rendered


def test_doctor_reports_unexpandable_legion_storage_without_traceback_or_disclosure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "~missing-cli-secret-canary/auth"
    original_expanduser = Path.expanduser

    def expanduser(path: Path) -> Path:
        if str(path) == canary:
            raise RuntimeError("unknown home for missing-cli-secret-canary")
        return original_expanduser(path)

    monkeypatch.setattr(Path, "expanduser", expanduser)
    environment = {
        "ECN_AUTH": "legion",
        "ECN_HOST": "broker.example.invalid",
        "ECN_INTEGRATION_NAME": "operator-view",
        "ECN_LEGION_AUTH_STORAGE": canary,
    }
    monkeypatch.setattr(
        _cli,
        "_load_config",
        lambda *, profile: load_config(profile=profile, environment=environment),
    )

    with pytest.raises(SystemExit) as raised:
        main(["doctor"])

    assert raised.value.code == 2
    rendered = capsys.readouterr().err
    assert "legion-system-auth storage must be an absolute safe path" in rendered
    assert canary not in rendered
    assert "missing-cli-secret-canary" not in rendered
    assert "RuntimeError" not in rendered


@pytest.mark.parametrize(
    "arguments",
    [
        [
            "configure",
            "--profile",
            "operator",
            "--host",
            "broker.example.invalid",
            "--integration-name",
            "operator-view",
            "--auth",
            "mtls",
            "--ca-certificate",
            "/credentials/ca.crt",
            "--client-certificate",
            "/credentials/client.crt",
            "--client-key",
            "/credentials/client.key",
            "--non-interactive",
        ],
        ["doctor", "--profile", "operator"],
        ["preflight", "--profile", "operator"],
    ],
)
def test_profile_commands_convert_unavailable_platform_home(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in ("APPDATA", "HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)

    def unavailable_home(_path_class: type[Path]) -> Path:
        raise RuntimeError("platform home lookup failed")

    monkeypatch.setattr(Path, "home", classmethod(unavailable_home))

    with pytest.raises(SystemExit) as raised:
        main(arguments)

    assert raised.value.code == 2
    rendered = capsys.readouterr().err
    assert "platform configuration directory is invalid" in rendered
    assert "RuntimeError" not in rendered
    assert "platform home lookup failed" not in rendered


def test_configure_rejects_control_character_in_configuration_directory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/path-canary\n")

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "configure",
                "--profile",
                "operator",
                "--host",
                "broker.example.invalid",
                "--integration-name",
                "operator-view",
                "--auth",
                "legion",
                "--non-interactive",
            ]
        )

    assert raised.value.code == 2
    rendered = capsys.readouterr().err
    assert "platform configuration directory is invalid" in rendered
    assert "path-canary" not in rendered
    assert "ValueError" not in rendered


def test_cli_exposes_no_token_or_password_persistence_option(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["configure", "--help"])
    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    assert "bearer-token" not in help_text
    assert "password" not in help_text.casefold()


@pytest.mark.parametrize(
    ("arguments", "forwarded"),
    [
        (["operator", "--demo"], ["--demo"]),
        (["operator", "--profile", "operator"], ["--profile", "operator"]),
    ],
)
def test_operator_command_delegates_to_the_separately_installed_application(
    arguments: list[str],
    forwarded: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[list[str] | None] = []
    monkeypatch.setattr(_cli, "_load_operator_main", lambda: captured.append)

    assert main(arguments) == 0
    assert captured == [forwarded]


def test_operator_command_fails_closed_when_the_optional_application_is_absent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing(_name: str) -> None:
        raise ModuleNotFoundError

    monkeypatch.setattr(_cli.importlib, "import_module", missing)

    with pytest.raises(SystemExit) as raised:
        main(["operator", "--demo"])

    assert raised.value.code == 2
    rendered = capsys.readouterr().err
    assert "matching picogrid-ecn-operator-app wheel" in rendered
    assert "Traceback" not in rendered


def test_operator_command_fails_closed_when_the_lazy_entry_point_is_not_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InvalidOperatorModule:
        main = None

    monkeypatch.setattr(
        _cli.importlib,
        "import_module",
        lambda _name: InvalidOperatorModule,
    )

    with pytest.raises(_cli.ConfigurationError, match="operator application is not installed"):
        _cli._load_operator_main()


def test_cli_parser_never_echoes_an_unrecognized_secret_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["configure", "--bearer-token", "secret-canary"])

    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert "secret-canary" not in error
    assert "invalid arguments" in error


@pytest.mark.asyncio
async def test_doctor_rejects_oversized_tls_material_before_openssl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ca_path = tmp_path / "oversized-ca.pem"
    ca_path.write_bytes(b"x" * (1024 * 1024 + 1))
    config = ECNConfig(
        host="broker.example.invalid",
        mqtt_port=8884,
        integration_name="operator-view",
        auth=BearerTokenAuth(username="integration-identity", token=SecretStr("synthetic-token")),
        tls=TLSConfig(ca_certificate=CertificateMaterial(path=ca_path)),
    )
    openssl_calls = 0

    def reject_openssl_parse(*_args: object, **_kwargs: object) -> object:
        nonlocal openssl_calls
        openssl_calls += 1
        raise AssertionError("oversized TLS material reached OpenSSL")

    monkeypatch.setattr(
        "picogrid_ecn_client._transport.credentials.ssl.create_default_context",
        reject_openssl_parse,
    )

    result = await _cli._doctor_configuration(config)

    assert result["ready"] is False
    assert openssl_calls == 0
