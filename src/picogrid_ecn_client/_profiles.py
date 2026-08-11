# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Private, non-secret named-profile persistence."""

from __future__ import annotations

import errno
import json
import math
import os
import re
import stat
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Final, TypeAlias, TypedDict

from ._network import normalize_host
from .auth import _validate_mqtt_username
from .exceptions import ConfigurationError

ReconnectProfileValue: TypeAlias = float | int
ReconnectProfileData: TypeAlias = dict[str, ReconnectProfileValue]
ProfileValue: TypeAlias = str | int | ReconnectProfileData
ProfileData: TypeAlias = dict[str, ProfileValue]


class _ProfileDocument(TypedDict):
    version: int
    profiles: dict[str, ProfileData]


_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_INTEGRATION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,126}[A-Za-z0-9]$")
_DOCUMENT_VERSION: Final = 1
_MAXIMUM_PROFILE_FILE_SIZE: Final = 1024 * 1024
_MAXIMUM_JSON_NESTING_DEPTH: Final = 64
_PROFILE_FILE_NAME: Final = "profiles.json"
_PROFILE_LOCK_FILE_NAME: Final = ".profiles.lock"
_PROFILE_LOCK_TIMEOUT_SECONDS: Final = 5.0
_PROFILE_LOCK_POLL_SECONDS: Final = 0.05
_PROFILE_LOCK_CONTENTION_ERRNOS: Final = frozenset({errno.EACCES, errno.EAGAIN})
_STRING_FIELDS: Final = frozenset(
    {
        "host",
        "integration_name",
        "terminal_id",
        "auth",
        "ca_certificate",
        "client_certificate",
        "client_key",
        "legion_auth_storage",
        "mqtt_username",
        "ntp_host",
        "wire_format",
    }
)
_PATH_FIELDS: Final = frozenset(
    {
        "ca_certificate",
        "client_certificate",
        "client_key",
        "legion_auth_storage",
    }
)
_RECONNECT_POLICY_FLOAT_FIELDS: Final = frozenset(
    {
        "initial_delay_seconds",
        "multiplier",
        "maximum_delay_seconds",
        "stable_reset_seconds",
        "maximum_elapsed_seconds",
    }
)
_RECONNECT_POLICY_FIELDS: Final = _RECONNECT_POLICY_FLOAT_FIELDS | {"maximum_attempts"}
_ALLOWED_FIELDS: Final = _STRING_FIELDS | {"mqtt_port", "ntp_port", "reconnect_policy"}
_ENVIRONMENT_OVERRIDES: Final = {
    "ECN_HOST": "host",
    "ECN_MQTT_PORT": "mqtt_port",
    "ECN_INTEGRATION_NAME": "integration_name",
    "ECN_TERMINAL_ID": "terminal_id",
    "ECN_AUTH": "auth",
    "ECN_CA_CERT": "ca_certificate",
    "ECN_CLIENT_CERT": "client_certificate",
    "ECN_CLIENT_KEY": "client_key",
    "ECN_MQTT_USERNAME": "mqtt_username",
    "ECN_NTP_HOST": "ntp_host",
    "ECN_NTP_PORT": "ntp_port",
    "LEGION_AUTH_STORAGE_PATH": "legion_auth_storage",
    "ECN_LEGION_AUTH_STORAGE": "legion_auth_storage",
    "ECN_WIRE_FORMAT": "wire_format",
    "ECN_RECONNECT_INITIAL_DELAY_SECONDS": "reconnect_policy.initial_delay_seconds",
    "ECN_RECONNECT_MULTIPLIER": "reconnect_policy.multiplier",
    "ECN_RECONNECT_MAXIMUM_DELAY_SECONDS": "reconnect_policy.maximum_delay_seconds",
    "ECN_RECONNECT_STABLE_RESET_SECONDS": "reconnect_policy.stable_reset_seconds",
    "ECN_RECONNECT_MAXIMUM_ATTEMPTS": "reconnect_policy.maximum_attempts",
    "ECN_RECONNECT_MAXIMUM_ELAPSED_SECONDS": "reconnect_policy.maximum_elapsed_seconds",
}
# ``LEGION_AUTH_STORAGE_PATH`` is the pinned local service's compatibility
# variable. ``ECN_LEGION_AUTH_STORAGE`` is canonical and appears later so it wins
# when both are supplied; the provider also honors the alias without a profile.
_SECRET_FIELD_FRAGMENTS: Final = ("password", "secret", "token")


def profile_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the platform configuration file used for named profiles."""

    values = os.environ if environ is None else environ
    xdg_home = _configuration_directory_override(values.get("XDG_CONFIG_HOME"))
    if xdg_home is not None:
        base = _absolute_configuration_directory(xdg_home)
    elif sys.platform == "darwin":
        base = _home_directory(values) / "Library" / "Application Support"
    elif os.name == "nt":
        app_data = _configuration_directory_override(values.get("APPDATA"))
        base = (
            _absolute_configuration_directory(app_data)
            if app_data is not None
            else _home_directory(values)
        )
    else:
        base = _home_directory(values) / ".config"
    return base / "picogrid" / "ecn-sdk" / _PROFILE_FILE_NAME


def save_profile(
    name: str,
    data: Mapping[str, object],
    environ: Mapping[str, str] | None = None,
) -> None:
    """Atomically create or replace one validated non-secret profile."""

    profile_name = _validate_profile_name(name)
    profile = _validate_profile_data(data)
    path = profile_path(environ)
    _ensure_private_directory(path.parent)
    lock_descriptor = _acquire_profile_transaction_lock(path.parent)
    try:
        document = _read_document(path, missing_ok=True)
        document["profiles"][profile_name] = profile
        _write_document(path, document)
    except BaseException:
        # Closing the descriptor releases the advisory lock. A cleanup failure
        # must not replace the transaction's primary exception.
        with suppress(OSError):
            os.close(lock_descriptor)
        raise
    try:
        os.close(lock_descriptor)
    except OSError:
        raise ConfigurationError(
            "the ECN profile transaction lock could not be released safely"
        ) from None


def load_profile(
    name: str,
    environ: Mapping[str, str] | None = None,
) -> ProfileData:
    """Load one profile and apply supported non-secret environment overrides."""

    profile_name = _validate_profile_name(name)
    document = _read_document(profile_path(environ), missing_ok=False)
    profiles = document["profiles"]
    if profile_name not in profiles:
        raise ConfigurationError("the requested ECN profile does not exist")
    raw_profile = profiles[profile_name]
    if not isinstance(raw_profile, dict):
        raise ConfigurationError("the ECN profile store is malformed")

    merged: dict[str, object] = dict(raw_profile)
    values = os.environ if environ is None else environ
    for environment_name, field_name in _ENVIRONMENT_OVERRIDES.items():
        raw_value = values.get(environment_name)
        if raw_value is not None and raw_value.strip():
            if field_name.startswith("reconnect_policy."):
                policy = merged.get("reconnect_policy", {})
                if not isinstance(policy, Mapping):
                    raise ConfigurationError("ECN profile reconnect policy is invalid")
                merged_policy = dict(policy)
                merged_policy[field_name.removeprefix("reconnect_policy.")] = raw_value
                merged["reconnect_policy"] = merged_policy
            else:
                merged[field_name] = raw_value
    return _validate_profile_data(merged, allow_no_auth=True)


def resolve_profile_name(
    cli_name: str | None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve an explicit CLI profile before the ``ECN_PROFILE`` environment."""

    if cli_name is not None:
        return _validate_profile_name(cli_name)
    values = os.environ if environ is None else environ
    environment_name = _nonempty(values.get("ECN_PROFILE"))
    return None if environment_name is None else _validate_profile_name(environment_name)


def _home_directory(environ: Mapping[str, str]) -> Path:
    configured = _configuration_directory_override(environ.get("HOME"))
    if configured is None:
        configured = _configuration_directory_override(environ.get("USERPROFILE"))
    if configured is not None:
        return _absolute_configuration_directory(configured)
    try:
        home = Path.home()
    except RuntimeError:
        raise ConfigurationError("the platform configuration directory is invalid") from None
    if not home.is_absolute():  # pragma: no cover - platform invariant
        raise ConfigurationError("the platform configuration directory is invalid")
    return home


def _absolute_configuration_directory(value: str) -> Path:
    if _has_control_character(value):
        raise ConfigurationError("the platform configuration directory is invalid")
    path = _expand_user_path(
        value,
        failure_message="the platform configuration directory is invalid",
    )
    if not path.is_absolute() or ".." in path.parts:
        raise ConfigurationError("the platform configuration directory is invalid")
    return path


def _configuration_directory_override(value: str | None) -> str | None:
    if value is not None and _has_control_character(value):
        raise ConfigurationError("the platform configuration directory is invalid")
    return _nonempty(value)


def _expand_user_path(value: str, *, failure_message: str) -> Path:
    try:
        return Path(value).expanduser()
    except RuntimeError:
        raise ConfigurationError(failure_message) from None


def _nonempty(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _validate_profile_name(name: object) -> str:
    if not isinstance(name, str):
        raise ConfigurationError("ECN profile name must be a string")
    normalized = name.strip()
    if normalized in {".", ".."} or _PROFILE_NAME.fullmatch(normalized) is None:
        raise ConfigurationError(
            "ECN profile name must use 1-64 letters, digits, periods, underscores, or hyphens"
        )
    return normalized


def _validate_profile_data(
    data: Mapping[str, object],
    *,
    allow_no_auth: bool = False,
) -> ProfileData:
    unknown = set(data) - _ALLOWED_FIELDS
    if unknown:
        if any(
            fragment in key.casefold() for key in unknown for fragment in _SECRET_FIELD_FRAGMENTS
        ):
            raise ConfigurationError("ECN profiles cannot persist secrets")
        raise ConfigurationError("ECN profile contains unsupported settings")

    result: ProfileData = {}
    for key, value in data.items():
        if value is None:
            continue
        if key == "reconnect_policy":
            result[key] = _validate_reconnect_policy(value)
            continue
        if key in {"mqtt_port", "ntp_port"}:
            result[key] = _validate_port(value, kind="MQTT" if key == "mqtt_port" else "NTP")
            continue
        if not isinstance(value, str):
            raise ConfigurationError("ECN profile setting has an invalid type")
        if key in {"host", "ntp_host"}:
            try:
                normalized = normalize_host(value)
            except ValueError:
                raise ConfigurationError(
                    "ECN profile host must be a DNS name or IP literal"
                    if key == "host"
                    else "ECN profile NTP host must be a DNS name or IP literal"
                ) from None
        else:
            normalized = value.strip()
        if not normalized or _has_control_character(normalized):
            raise ConfigurationError(
                "ECN profile setting must be non-empty and contain no controls"
            )
        result[key] = normalized

    _validate_special_fields(result, allow_no_auth=allow_no_auth)
    return result


def _validate_reconnect_policy(value: object) -> ReconnectProfileData:
    if not isinstance(value, Mapping):
        raise ConfigurationError("ECN profile reconnect policy is invalid")
    unknown = set(value) - _RECONNECT_POLICY_FIELDS
    if unknown:
        if any(
            fragment in str(key).casefold()
            for key in unknown
            for fragment in _SECRET_FIELD_FRAGMENTS
        ):
            raise ConfigurationError("ECN profiles cannot persist secrets")
        raise ConfigurationError("ECN profile reconnect policy contains unsupported settings")

    result: ReconnectProfileData = {}
    for key, raw_value in value.items():
        if raw_value is None:
            continue
        if not isinstance(key, str):
            raise ConfigurationError("ECN profile reconnect policy is invalid")
        if isinstance(raw_value, bool):
            raise ConfigurationError("ECN profile reconnect policy values must be numeric")
        if key == "maximum_attempts":
            result[key] = _validate_reconnect_attempts(raw_value)
        else:
            result[key] = _validate_reconnect_float(key, raw_value)

    initial_delay = float(result.get("initial_delay_seconds", 0.5))
    maximum_delay = float(result.get("maximum_delay_seconds", 30.0))
    if maximum_delay < initial_delay:
        raise ConfigurationError(
            "ECN profile reconnect maximum delay must not be below initial delay"
        )
    return result


def _validate_reconnect_attempts(value: object) -> int:
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized or _has_control_character(normalized):
            raise ConfigurationError("ECN profile reconnect maximum attempts must be an integer")
        try:
            parsed = int(normalized, 10)
        except ValueError:
            raise ConfigurationError(
                "ECN profile reconnect maximum attempts must be an integer"
            ) from None
    elif isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    else:
        raise ConfigurationError("ECN profile reconnect maximum attempts must be an integer")
    if parsed < 1:
        raise ConfigurationError("ECN profile reconnect maximum attempts must be positive")
    return parsed


def _validate_reconnect_float(field: str, value: object) -> float:
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized or _has_control_character(normalized):
            raise ConfigurationError("ECN profile reconnect timing value must be numeric")
        try:
            parsed = float(normalized)
        except (OverflowError, ValueError):
            raise ConfigurationError("ECN profile reconnect timing value must be numeric") from None
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = float(value)
        except OverflowError:
            raise ConfigurationError(
                "ECN profile reconnect timing value is outside its valid range"
            ) from None
    else:
        raise ConfigurationError("ECN profile reconnect timing value must be numeric")
    minimum = 1.0 if field == "multiplier" else 0.0
    if not math.isfinite(parsed) or parsed <= 0.0 or parsed < minimum:
        raise ConfigurationError("ECN profile reconnect timing value is outside its valid range")
    return parsed


def _validate_port(value: object, *, kind: str) -> int:
    setting = f"ECN {kind} port"
    if isinstance(value, str):
        try:
            parsed = int(value, 10)
        except ValueError:
            raise ConfigurationError(f"{setting} must be an integer") from None
    elif isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    else:
        raise ConfigurationError(f"{setting} must be an integer")
    if not 1 <= parsed <= 65_535:
        raise ConfigurationError(f"{setting} must be between 1 and 65535")
    return parsed


def _validate_special_fields(data: ProfileData, *, allow_no_auth: bool) -> None:
    integration = data.get("integration_name")
    if isinstance(integration, str) and (
        _INTEGRATION_NAME.fullmatch(integration) is None or integration.casefold() == "geolocation"
    ):
        raise ConfigurationError("ECN profile integration name is invalid")

    terminal = data.get("terminal_id")
    if isinstance(terminal, str):
        try:
            data["terminal_id"] = str(uuid.UUID(terminal))
        except ValueError:
            raise ConfigurationError("ECN profile terminal ID must be a UUID") from None

    auth = data.get("auth")
    allowed_auth = (
        {"bearer", "legion", "mtls", "none"}
        if allow_no_auth
        else {
            "bearer",
            "legion",
            "mtls",
        }
    )
    if isinstance(auth, str) and auth not in allowed_auth:
        raise ConfigurationError("ECN profile authentication must be bearer, legion, or mtls")

    wire_format = data.get("wire_format")
    if isinstance(wire_format, str) and wire_format not in {"json", "protobuf"}:
        raise ConfigurationError("ECN profile wire format must be json or protobuf")

    mqtt_username = data.get("mqtt_username")
    if isinstance(mqtt_username, str):
        try:
            data["mqtt_username"] = _validate_mqtt_username(mqtt_username)
        except ValueError:
            raise ConfigurationError("ECN profile MQTT username is invalid") from None

    for field in _PATH_FIELDS:
        value = data.get(field)
        if isinstance(value, str):
            candidate = _expand_user_path(
                value,
                failure_message="ECN profile credential references must be absolute paths",
            )
            if not candidate.is_absolute() or ".." in candidate.parts:
                raise ConfigurationError("ECN profile credential references must be absolute paths")
            if "-----BEGIN" in value:
                raise ConfigurationError(
                    "ECN profiles may reference credential files but not embed them"
                )
            data[field] = str(candidate)


def _has_control_character(value: str) -> bool:
    return any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in value)


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
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


def _read_document(path: Path, *, missing_ok: bool) -> _ProfileDocument:
    try:
        directory_descriptor = _open_private_directory(path.parent)
    except FileNotFoundError:
        if missing_ok:
            return {"version": _DOCUMENT_VERSION, "profiles": {}}
        raise ConfigurationError("no ECN profiles have been configured") from None
    except OSError:
        raise ConfigurationError("the ECN profile store cannot be read safely") from None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path.name, flags, dir_fd=directory_descriptor)
        except FileNotFoundError:
            if missing_ok:
                return {"version": _DOCUMENT_VERSION, "profiles": {}}
            raise ConfigurationError("no ECN profiles have been configured") from None
        except OSError:
            raise ConfigurationError("the ECN profile store cannot be read safely") from None
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_size > _MAXIMUM_PROFILE_FILE_SIZE:
                raise ConfigurationError("the ECN profile store is too large")
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise ConfigurationError("the ECN profile store failed local safety checks")
            raw = _read_descriptor(descriptor, _MAXIMUM_PROFILE_FILE_SIZE)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_descriptor)

    try:
        parsed = json.loads(raw, object_pairs_hook=_json_object)
        if _has_excessive_json_nesting(parsed):
            raise ValueError("JSON nesting exceeds the supported limit")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ConfigurationError("the ECN profile store is malformed") from None
    return _validate_document(parsed)


def _validate_document(value: object) -> _ProfileDocument:
    if not isinstance(value, dict) or set(value) != {"version", "profiles"}:
        raise ConfigurationError("the ECN profile store is malformed")
    if value["version"] != _DOCUMENT_VERSION or not isinstance(value["profiles"], dict):
        raise ConfigurationError("the ECN profile store version is unsupported")
    profiles = value["profiles"]
    assert isinstance(profiles, dict)
    validated_profiles: dict[str, ProfileData] = {}
    for name, data in profiles.items():
        profile_name = _validate_profile_name(name)
        if not isinstance(data, dict):
            raise ConfigurationError("the ECN profile store is malformed")
        validated_profiles[profile_name] = _validate_profile_data(data)
    return {"version": _DOCUMENT_VERSION, "profiles": validated_profiles}


def _open_private_directory(path: Path) -> int:
    descriptor = _open_directory(path)
    metadata = os.fstat(descriptor)
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        os.close(descriptor)
        raise ConfigurationError("the ECN profile directory failed local safety checks")
    return descriptor


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise ConfigurationError("the ECN profile directory failed local safety checks")
    return descriptor


def _acquire_profile_transaction_lock(
    directory: Path,
    *,
    _monotonic: Callable[[], float] = time.monotonic,
    _sleeper: Callable[[float], None] = time.sleep,
) -> int:
    try:
        import fcntl
    except ImportError:
        raise ConfigurationError(
            "named ECN profile persistence requires POSIX file locking"
        ) from None

    directory_descriptor = _open_private_directory(directory)
    lock_descriptor: int | None = None
    try:
        try:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            lock_descriptor = os.open(
                _PROFILE_LOCK_FILE_NAME,
                flags,
                0o600,
                dir_fd=directory_descriptor,
            )
            metadata = os.fstat(lock_descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise ConfigurationError(
                    "the ECN profile transaction lock failed local safety checks"
                )
            os.fchmod(lock_descriptor, 0o600)
            if stat.S_IMODE(os.fstat(lock_descriptor).st_mode) != 0o600:
                raise ConfigurationError(
                    "the ECN profile transaction lock failed local safety checks"
                )
            deadline = _monotonic() + _PROFILE_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    if error.errno not in _PROFILE_LOCK_CONTENTION_ERRNOS:
                        raise
                    remaining = deadline - _monotonic()
                    if remaining <= 0:
                        raise ConfigurationError(
                            "another process holds the ECN profile transaction lock"
                        ) from None
                    _sleeper(min(_PROFILE_LOCK_POLL_SECONDS, remaining))
                else:
                    break
        except ConfigurationError:
            raise
        except OSError:
            raise ConfigurationError(
                "the ECN profile transaction lock could not be used safely"
            ) from None
    except BaseException:
        if lock_descriptor is not None:
            with suppress(OSError):
                os.close(lock_descriptor)
        with suppress(OSError):
            os.close(directory_descriptor)
        raise
    try:
        os.close(directory_descriptor)
    except OSError:
        assert lock_descriptor is not None
        with suppress(OSError):
            os.close(lock_descriptor)
        raise ConfigurationError(
            "the ECN profile transaction lock could not be used safely"
        ) from None
    assert lock_descriptor is not None
    return lock_descriptor


def _ensure_private_directory(path: Path) -> None:
    try:
        descriptor = _open_directory(path)
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = _open_directory(path)
        except OSError:
            raise ConfigurationError("the ECN profile directory cannot be created safely") from None
    except OSError:
        raise ConfigurationError("the ECN profile directory cannot be used safely") from None
    try:
        try:
            # A profile directory must be traversable only by its owner. The
            # generic file-permission rule's 0644 recommendation is invalid for
            # directories and would expose profile names and credential paths.
            os.fchmod(  # nosemgrep: rules.python.lang.security.audit.insecure-file-permissions
                descriptor, 0o700
            )
            metadata = os.fstat(descriptor)
        except OSError:
            raise ConfigurationError("the ECN profile directory cannot be secured") from None
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ConfigurationError("the ECN profile directory cannot be secured")
    finally:
        os.close(descriptor)


def _read_descriptor(descriptor: int, maximum_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum_size + 1
    while remaining:
        chunk = os.read(descriptor, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    value = b"".join(chunks)
    if len(value) > maximum_size:
        raise ConfigurationError("the ECN profile store is too large")
    return value


def _write_document(path: Path, document: _ProfileDocument) -> None:
    encoded = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > _MAXIMUM_PROFILE_FILE_SIZE:
        raise ConfigurationError("the ECN profile store would exceed its size limit")
    directory_descriptor = _open_private_directory(path.parent)
    temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_descriptor)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            os.fsync(directory_descriptor)
        except OSError:
            raise ConfigurationError("the ECN profile store could not be written safely") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
        finally:
            os.close(directory_descriptor)


__all__ = [
    "ProfileData",
    "load_profile",
    "profile_path",
    "resolve_profile_name",
    "save_profile",
]
