# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Installed command-line workflows for profiles and read-only diagnostics."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import math
import os
import sys
from collections.abc import Sequence
from typing import Any, NoReturn, Protocol, cast

from pydantic import ValidationError as PydanticValidationError

from . import __version__
from . import config as _config_module
from ._legion_auth import _LegionCredentialError
from ._profiles import ProfileData, resolve_profile_name, save_profile
from ._transport.credentials import build_lifecycle_owned_client_ssl_context
from .auth import BearerTokenAuth
from .client import ECNClient
from .config import ECNConfig, _mqtt_port
from .exceptions import ClockToleranceError, ConfigurationError, ECNClientError


class _ConfigLoader(Protocol):
    def __call__(self, *, profile: str | None = None) -> ECNConfig: ...


class _OperatorMain(Protocol):
    def __call__(self, argv: Sequence[str] | None = None) -> None: ...


class _SafeArgumentParser(argparse.ArgumentParser):
    """Keep arbitrary command-line values out of parser diagnostics."""

    def error(self, _message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid arguments; use --help\n")


def _clock_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout must be a number") from None
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise argparse.ArgumentTypeError("timeout must be positive and at most 60 seconds")
    return timeout


def _positive_finite(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("value must be a number") from None
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive and finite")
    return parsed


def _reconnect_multiplier(value: str) -> float:
    parsed = _positive_finite(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("multiplier must be at least one")
    return parsed


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError:
        raise argparse.ArgumentTypeError("value must be an integer") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="picogrid-ecn",
        description="Configure and validate an installed Picogrid ECN SDK client.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    configure = commands.add_parser("configure", help="create or replace a named profile")
    configure.add_argument("--profile", metavar="NAME")
    configure.add_argument("--host")
    configure.add_argument("--integration-name")
    configure.add_argument("--terminal-id")
    configure.add_argument("--auth", choices=("mtls", "bearer", "legion"))
    configure.add_argument("--mqtt-port", type=int)
    configure.add_argument("--ntp-host")
    configure.add_argument("--ntp-port", type=int)
    configure.add_argument("--ca-certificate")
    configure.add_argument("--client-certificate")
    configure.add_argument("--client-key")
    configure.add_argument("--mqtt-username")
    configure.add_argument("--legion-auth-storage")
    configure.add_argument("--wire-format", choices=("json", "protobuf"), default="json")
    configure.add_argument("--reconnect-initial-delay-seconds", type=_positive_finite)
    configure.add_argument("--reconnect-multiplier", type=_reconnect_multiplier)
    configure.add_argument("--reconnect-maximum-delay-seconds", type=_positive_finite)
    configure.add_argument("--reconnect-stable-reset-seconds", type=_positive_finite)
    configure.add_argument("--reconnect-maximum-attempts", type=_positive_integer)
    configure.add_argument("--reconnect-maximum-elapsed-seconds", type=_positive_finite)
    configure.add_argument(
        "--non-interactive",
        action="store_true",
        help="fail instead of prompting for omitted required settings",
    )

    doctor = commands.add_parser("doctor", help="validate local configuration and credentials")
    doctor.add_argument("--profile", metavar="NAME")

    preflight = commands.add_parser("preflight", help="run read-only ECN connectivity checks")
    preflight.add_argument("--profile", metavar="NAME")

    operator = commands.add_parser("operator", help="launch the installed operator application")
    operator_selection = operator.add_mutually_exclusive_group(required=True)
    operator_selection.add_argument("--demo", action="store_true")
    operator_selection.add_argument("--profile", metavar="NAME")

    clock = commands.add_parser("clock", help="run ECN-relative clock diagnostics")
    clock_commands = clock.add_subparsers(dest="clock_command", required=True)
    clock_check = clock_commands.add_parser("check", help="require a maximum clock offset")
    clock_check.add_argument("--profile", metavar="NAME")
    clock_check.add_argument("--max-offset", type=float, required=True, metavar="SECONDS")
    clock_check.add_argument(
        "--samples",
        type=int,
        default=3,
        choices=range(1, 11),
        metavar="N",
    )
    clock_check.add_argument("--timeout", type=_clock_timeout, metavar="SECONDS")

    return parser


def _prompt(
    parser: argparse.ArgumentParser,
    current: str | None,
    label: str,
    *,
    required: bool,
    non_interactive: bool,
    preserve_raw: bool = False,
) -> str | None:
    if current is not None and current.strip():
        return current if preserve_raw else current.strip()
    if non_interactive or not sys.stdin.isatty():
        if required:
            _configuration_error(parser, f"{label} is required in non-interactive mode")
        return None
    raw_value = input(f"{label}{' (required)' if required else ' (optional)'}: ")
    value = raw_value if preserve_raw else raw_value.strip()
    if required and not value.strip():
        _configuration_error(parser, f"{label} is required")
    return value if value.strip() else None


def _configure(parser: argparse.ArgumentParser, arguments: argparse.Namespace) -> int:
    environment = dict(os.environ)
    profile_name = resolve_profile_name(arguments.profile, environment) or "default"
    auth = _prompt(
        parser,
        arguments.auth,
        "authentication profile (mtls, bearer, or legion)",
        required=True,
        non_interactive=arguments.non_interactive,
    )
    assert auth is not None
    _reject_inapplicable_auth_options(parser, arguments, auth)
    host = _prompt(
        parser,
        arguments.host,
        "ECN host",
        required=True,
        non_interactive=arguments.non_interactive,
        preserve_raw=True,
    )
    integration_name = _prompt(
        parser,
        arguments.integration_name,
        "integration name",
        required=True,
        non_interactive=arguments.non_interactive,
    )
    assert host is not None and integration_name is not None

    profile: ProfileData = {
        "host": host,
        "integration_name": integration_name,
        "auth": auth,
        "wire_format": arguments.wire_format,
    }
    _include(profile, "terminal_id", arguments.terminal_id)
    _include(profile, "mqtt_port", arguments.mqtt_port)
    _include(profile, "ntp_host", arguments.ntp_host)
    _include(profile, "ntp_port", arguments.ntp_port)
    _include(profile, "ca_certificate", arguments.ca_certificate)
    reconnect_policy = {
        field: value
        for field, value in (
            ("initial_delay_seconds", arguments.reconnect_initial_delay_seconds),
            ("multiplier", arguments.reconnect_multiplier),
            ("maximum_delay_seconds", arguments.reconnect_maximum_delay_seconds),
            ("stable_reset_seconds", arguments.reconnect_stable_reset_seconds),
            ("maximum_attempts", arguments.reconnect_maximum_attempts),
            ("maximum_elapsed_seconds", arguments.reconnect_maximum_elapsed_seconds),
        )
        if value is not None
    }
    if reconnect_policy:
        profile["reconnect_policy"] = reconnect_policy

    if auth == "mtls":
        certificate = _prompt(
            parser,
            arguments.client_certificate,
            "client certificate reference",
            required=True,
            non_interactive=arguments.non_interactive,
        )
        key = _prompt(
            parser,
            arguments.client_key,
            "client key reference",
            required=True,
            non_interactive=arguments.non_interactive,
        )
        _include(profile, "client_certificate", certificate)
        _include(profile, "client_key", key)
    elif auth == "bearer":
        username = _prompt(
            parser,
            arguments.mqtt_username,
            "MQTT username",
            required=True,
            non_interactive=arguments.non_interactive,
        )
        _include(profile, "mqtt_username", username)
    else:
        _include(profile, "legion_auth_storage", arguments.legion_auth_storage)

    selected_port = _mqtt_port(profile, environment, auth)
    save_profile(profile_name, profile, environment)
    print(f"Configured ECN profile {profile_name!r}; connection port {selected_port}.")
    return 0


def _reject_inapplicable_auth_options(
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
    auth: str,
) -> None:
    allowed_options = {
        "mtls": {"--client-certificate", "--client-key"},
        "bearer": {"--mqtt-username"},
        "legion": {"--legion-auth-storage"},
    }.get(auth)
    if allowed_options is None:
        _configuration_error(parser, "authentication profile must be mtls, bearer, or legion")
    assert allowed_options is not None

    supplied_options = {
        "--client-certificate": arguments.client_certificate,
        "--client-key": arguments.client_key,
        "--mqtt-username": arguments.mqtt_username,
        "--legion-auth-storage": arguments.legion_auth_storage,
    }
    rejected_options = [
        option
        for option, value in supplied_options.items()
        if value is not None and option not in allowed_options
    ]
    if rejected_options:
        _configuration_error(
            parser,
            f"{auth} authentication does not accept {', '.join(rejected_options)}",
        )


def _include(target: ProfileData, name: str, value: str | int | None) -> None:
    if value is not None:
        target[name] = value


def _configuration_error(parser: argparse.ArgumentParser, message: str) -> None:
    parser.print_usage(sys.stderr)
    parser.exit(2, f"{parser.prog}: error: {message}\n")


async def _doctor_configuration(config: ECNConfig) -> dict[str, Any]:
    checks: list[dict[str, str]] = [
        {"name": "configuration", "status": "pass", "detail": "configuration is valid"}
    ]
    try:
        if (
            config.tls.enabled
            and (await build_lifecycle_owned_client_ssl_context(config.tls, config.auth)) is None
        ):
            raise ValueError("TLS context was not created")
        if isinstance(config.auth, BearerTokenAuth):
            await config.auth._resolve_credentials(config.integration_name)
    except _LegionCredentialError as error:
        checks.append({"name": "credentials", "status": "fail", "detail": str(error)})
        return {"ready": False, "checks": checks}
    except ECNClientError:
        checks.append(
            {
                "name": "credentials",
                "status": "fail",
                "detail": "credential material could not be validated",
            }
        )
        return {"ready": False, "checks": checks}
    except Exception:
        checks.append(
            {
                "name": "credentials",
                "status": "fail",
                "detail": "credential material could not be validated",
            }
        )
        return {"ready": False, "checks": checks}
    checks.append(
        {"name": "credentials", "status": "pass", "detail": "credential material is valid"}
    )
    return {"ready": True, "checks": checks}


def _doctor(arguments: argparse.Namespace) -> int:
    result = asyncio.run(_doctor_configuration(_load_config(profile=arguments.profile)))
    print(json.dumps(result, indent=2))
    return 0 if result["ready"] else 2


async def _run_preflight(config: ECNConfig) -> tuple[str, bool]:
    client = ECNClient(config)
    try:
        report = await client.preflight()
    finally:
        await client.close()
    return report.model_dump_json(indent=2), report.successful


def _preflight(arguments: argparse.Namespace) -> int:
    rendered, successful = asyncio.run(_run_preflight(_load_config(profile=arguments.profile)))
    print(rendered)
    return 0 if successful else 2


async def _run_clock_check(
    config: ECNConfig,
    *,
    max_offset_seconds: float,
    samples: int,
    timeout: float | None,
) -> tuple[str, bool]:
    client = ECNClient(config)
    try:
        try:
            report = await client.clock.require_within(
                max_offset_seconds=max_offset_seconds,
                samples=samples,
                timeout=timeout,
            )
        except ClockToleranceError as error:
            return error.report.model_dump_json(indent=2), False
        return report.model_dump_json(indent=2), True
    finally:
        await client.close()


def _clock_check(arguments: argparse.Namespace) -> int:
    rendered, within_tolerance = asyncio.run(
        _run_clock_check(
            _load_config(profile=arguments.profile),
            max_offset_seconds=arguments.max_offset,
            samples=arguments.samples,
            timeout=arguments.timeout,
        )
    )
    print(rendered)
    return 0 if within_tolerance else 3


def _load_config(*, profile: str | None) -> ECNConfig:
    """Call the root-owned public loader without coupling this slice to its module edit."""

    loader = getattr(_config_module, "load_config", None)
    if loader is None:
        raise ConfigurationError("profile loading is unavailable in this installation")
    return cast("_ConfigLoader", loader)(profile=profile)


def _load_operator_main() -> _OperatorMain:
    try:
        module = importlib.import_module("operator_app.__main__")
        entry = module.main
    except (AttributeError, ImportError):
        raise ConfigurationError(
            "the operator application is not installed; install the matching "
            "picogrid-ecn-operator-app wheel"
        ) from None
    if not callable(entry):
        raise ConfigurationError(
            "the operator application is not installed; install the matching "
            "picogrid-ecn-operator-app wheel"
        )
    return cast("_OperatorMain", entry)


def _operator(arguments: argparse.Namespace) -> int:
    command = ["--demo"] if arguments.demo else ["--profile", arguments.profile]
    _load_operator_main()(command)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "configure":
            return _configure(parser, arguments)
        if arguments.command == "doctor":
            return _doctor(arguments)
        if arguments.command == "preflight":
            return _preflight(arguments)
        if arguments.command == "operator":
            return _operator(arguments)
        if arguments.command == "clock" and arguments.clock_command == "check":
            return _clock_check(arguments)
    except ECNClientError as error:
        parser.exit(2, f"{arguments.command} failed: {error}\n")
    except PydanticValidationError:
        parser.exit(2, f"{arguments.command} failed: configuration is invalid\n")
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
