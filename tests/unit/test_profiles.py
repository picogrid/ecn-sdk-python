# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import errno
import json
import multiprocessing
import os
from contextlib import suppress
from pathlib import Path

import pytest

from picogrid_ecn_client import _profiles as profile_module
from picogrid_ecn_client._profiles import (
    load_profile,
    profile_path,
    resolve_profile_name,
    save_profile,
)
from picogrid_ecn_client.exceptions import ConfigurationError


def _environment(tmp_path: Path) -> dict[str, str]:
    return {"XDG_CONFIG_HOME": str(tmp_path / "configuration")}


def _profile() -> dict[str, object]:
    return {
        "host": "mqtt.example.invalid",
        "integration_name": "sensor-example",
        "auth": "mtls",
        "ca_certificate": "/external/ca.crt",
        "client_certificate": "/external/client.crt",
        "client_key": "/external/client.key",
    }


def _record_descriptor_closes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    closed: list[int] = []
    original_close = os.close

    def tracked_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(profile_module.os, "close", tracked_close)
    return closed


def test_profile_path_uses_xdg_location(tmp_path: Path) -> None:
    path = profile_path(_environment(tmp_path))
    assert path == tmp_path / "configuration" / "picogrid" / "ecn-sdk" / "profiles.json"


def test_profile_path_rejects_relative_platform_locations() -> None:
    for environment in (
        {"XDG_CONFIG_HOME": "relative/config"},
        {"HOME": "relative/home"},
    ):
        with pytest.raises(ConfigurationError, match="platform configuration directory"):
            profile_path(environment)


@pytest.mark.parametrize(
    "value",
    (
        "/tmp/configuration\0canary",
        "/tmp/configuration-canary\n",
        "\x1f",
        "/tmp/configuration\x7fcanary",
        "/tmp/configuration\x9fcanary",
    ),
)
def test_profile_path_rejects_control_characters(value: str) -> None:
    with pytest.raises(
        ConfigurationError,
        match=r"^the platform configuration directory is invalid$",
    ) as raised:
        profile_path({"XDG_CONFIG_HOME": value})

    assert "canary" not in str(raised.value)


def test_profile_path_converts_unavailable_platform_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable_home(_path_class: type[Path]) -> Path:
        raise RuntimeError("platform home lookup failed")

    monkeypatch.setattr(Path, "home", classmethod(unavailable_home))

    with pytest.raises(ConfigurationError, match="platform configuration directory") as raised:
        profile_path({})

    assert "RuntimeError" not in str(raised.value)
    assert "platform home lookup failed" not in str(raised.value)


def test_save_and_load_profile_with_private_atomic_storage(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    save_profile("sensor", _profile(), environment)

    path = profile_path(environment)
    assert stat_mode(path.parent) == 0o700
    assert stat_mode(path) == 0o600
    assert load_profile("sensor", environment) == _profile()
    assert not tuple(path.parent.glob(".*.tmp"))


def test_reconnect_policy_round_trips_as_restricted_nonsecret_profile_data(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    reconnect_policy = {
        "initial_delay_seconds": 0.75,
        "multiplier": 1.5,
        "maximum_delay_seconds": 12.0,
        "stable_reset_seconds": 45.0,
        "maximum_attempts": 7,
        "maximum_elapsed_seconds": 90.0,
    }
    save_profile(
        "sensor",
        _profile() | {"reconnect_policy": reconnect_policy},
        environment,
    )

    assert load_profile("sensor", environment)["reconnect_policy"] == reconnect_policy
    document = json.loads(profile_path(environment).read_text(encoding="utf-8"))
    assert document["profiles"]["sensor"]["reconnect_policy"] == reconnect_policy

    with pytest.raises(ConfigurationError, match="cannot persist secrets"):
        save_profile(
            "unsafe",
            _profile() | {"reconnect_policy": {"token": 1}},
            environment,
        )


@pytest.mark.parametrize(
    "reconnect_policy",
    [
        {"initial_delay_seconds": True},
        {"maximum_attempts": False},
    ],
)
def test_profile_rejects_boolean_reconnect_policy_values(
    tmp_path: Path,
    reconnect_policy: dict[str, object],
) -> None:
    with pytest.raises(ConfigurationError, match="must be numeric"):
        save_profile(
            "sensor",
            _profile() | {"reconnect_policy": reconnect_policy},
            _environment(tmp_path),
        )


def test_profile_rejects_unrepresentable_reconnect_timing_as_configuration_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError, match="outside its valid range"):
        save_profile(
            "sensor",
            _profile() | {"reconnect_policy": {"initial_delay_seconds": 10**400}},
            _environment(tmp_path),
        )


def test_save_preserves_other_profiles_and_replaces_named_profile(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    save_profile("first", _profile(), environment)
    save_profile("second", _profile() | {"host": "second.example.invalid"}, environment)
    save_profile("first", _profile() | {"host": "replacement.example.invalid"}, environment)

    assert load_profile("first", environment)["host"] == "replacement.example.invalid"
    assert load_profile("second", environment)["host"] == "second.example.invalid"


def test_environment_overrides_stored_nonsecret_values(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    save_profile("sensor", _profile(), environment)

    loaded = load_profile(
        "sensor",
        environment
        | {
            "ECN_HOST": "override.example.invalid",
            "ECN_MQTT_PORT": "8884",
            "ECN_AUTH": "legion",
            "LEGION_AUTH_STORAGE_PATH": "/external/legacy-legion-auth",
            "ECN_LEGION_AUTH_STORAGE": "/external/legion-auth",
        },
    )
    assert loaded["host"] == "override.example.invalid"
    assert loaded["mqtt_port"] == 8884
    assert loaded["auth"] == "legion"
    assert loaded["legion_auth_storage"] == "/external/legion-auth"


def test_environment_host_override_is_bounded_before_normalization(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    normalized_host = "a.example"
    accepted = f"{' ' * (1024 - len(normalized_host))}{normalized_host}"
    rejected = f"{' ' * (1025 - len(normalized_host))}{normalized_host}"
    save_profile("sensor", _profile(), environment)

    loaded = load_profile("sensor", environment | {"ECN_HOST": accepted})
    assert loaded["host"] == normalized_host

    with pytest.raises(ConfigurationError, match="DNS name or IP literal"):
        load_profile("sensor", environment | {"ECN_HOST": rejected})


def test_bearer_profile_persists_username_but_rejects_every_secret_field(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    save_profile(
        "bearer",
        {
            "host": "mqtt.example.invalid",
            "integration_name": "sensor-example",
            "auth": "bearer",
            "mqtt_username": "integration-identity",
        },
        environment,
    )
    stored = profile_path(environment).read_text(encoding="utf-8")
    assert "integration-identity" in stored

    for key in ("bearer_token", "client_secret", "client_key_password", "password"):
        with pytest.raises(ConfigurationError, match="cannot persist secrets"):
            save_profile("unsafe", _profile() | {key: "secret-canary"}, environment)
    assert "secret-canary" not in profile_path(environment).read_text(encoding="utf-8")


def test_profile_rejects_embedded_credentials_and_relative_references(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    with pytest.raises(ConfigurationError, match="absolute paths"):
        save_profile("unsafe", _profile() | {"client_key": "relative/client.key"}, environment)

    with pytest.raises(ConfigurationError, match="not embed them"):
        save_profile(
            "unsafe",
            _profile() | {"client_certificate": "/external/-----BEGIN CERTIFICATE-----"},
            environment,
        )


def test_profile_rejects_unexpandable_credential_reference_without_disclosure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    canary = "~missing-profile-secret-canary/client.key"
    original_expanduser = Path.expanduser

    def expanduser(path: Path) -> Path:
        if str(path) == canary:
            raise RuntimeError("unknown home for missing-profile-secret-canary")
        return original_expanduser(path)

    monkeypatch.setattr(Path, "expanduser", expanduser)

    with pytest.raises(ConfigurationError, match="absolute paths") as raised:
        save_profile("unsafe", _profile() | {"client_key": canary}, environment)

    assert canary not in str(raised.value)
    assert "missing-profile-secret-canary" not in str(raised.value)
    assert not profile_path(environment).exists()


def test_profile_names_and_identity_fields_are_validated(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    for name in ("../escape", ".", "", "contains space"):
        with pytest.raises(ConfigurationError, match="profile name"):
            save_profile(name, _profile(), environment)

    with pytest.raises(ConfigurationError, match="terminal ID"):
        save_profile("bad-uuid", _profile() | {"terminal_id": "not-a-uuid"}, environment)

    terminal_id = "A125DAB0-FBC8-4E4B-A251-803792548E10"
    save_profile("canonical", _profile() | {"terminal_id": terminal_id}, environment)
    assert load_profile("canonical", environment)["terminal_id"] == terminal_id.lower()


@pytest.mark.parametrize(
    "host",
    [".", "..", "...", ".bad", "bad..example", "bad..", f"{'x' * 64}.example"],
)
def test_profile_rejects_unresolvable_dns_shapes(tmp_path: Path, host: str) -> None:
    environment = _environment(tmp_path)

    with pytest.raises(ConfigurationError, match="DNS name or IP literal"):
        save_profile("invalid-host", _profile() | {"host": host}, environment)

    assert not profile_path(environment).exists()


def test_profile_host_input_boundary_is_applied_before_whitespace_normalization(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    normalized_host = "a.example"
    accepted = f"{' ' * (1024 - len(normalized_host))}{normalized_host}"
    rejected = f"{' ' * (1025 - len(normalized_host))}{normalized_host}"

    save_profile("valid-host", _profile() | {"host": accepted}, environment)
    assert load_profile("valid-host", environment)["host"] == normalized_host

    with pytest.raises(ConfigurationError, match="DNS name or IP literal"):
        save_profile("invalid-host", _profile() | {"host": rejected}, environment)

    assert load_profile("valid-host", environment)["host"] == normalized_host
    with pytest.raises(ConfigurationError, match="does not exist"):
        load_profile("invalid-host", environment)


@pytest.mark.parametrize("separator", ["\u3002", "\uff0e", "\uff61"])
def test_profile_rejects_empty_labels_after_idna_mapping(
    tmp_path: Path,
    separator: str,
) -> None:
    environment = _environment(tmp_path)

    with pytest.raises(ConfigurationError, match="DNS name or IP literal"):
        save_profile("invalid-host", _profile() | {"host": f"a{separator}.b"}, environment)

    assert not profile_path(environment).exists()


def test_profile_accepts_exact_rooted_dns_wire_boundary(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    maximum_host = ".".join(("a" * 63, "b" * 63, "c" * 63, "d" * 61))

    save_profile("rooted", _profile() | {"host": f"{maximum_host}."}, environment)

    assert load_profile("rooted", environment)["host"] == maximum_host


@pytest.mark.parametrize(
    "username",
    ["username-canary\ud800", "username-canary\ufdd0", "username-canary\U0001ffff", "x" * 257],
)
def test_profile_rejects_invalid_mqtt_username_without_writing(
    tmp_path: Path,
    username: str,
) -> None:
    environment = _environment(tmp_path)

    with pytest.raises(ConfigurationError, match="MQTT username is invalid") as raised:
        save_profile(
            "invalid-username",
            _profile() | {"auth": "bearer", "mqtt_username": username},
            environment,
        )

    assert "username-canary" not in str(raised.value)
    assert not profile_path(environment).exists()


def test_profile_store_size_boundary_preserves_last_readable_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    path = profile_path(environment)
    save_profile("first", _profile(), environment)
    maximum_size = path.stat().st_size
    monkeypatch.setattr(profile_module, "_MAXIMUM_PROFILE_FILE_SIZE", maximum_size)

    exact_boundary = _profile() | {"host": "safe.example.invalid"}
    save_profile("first", exact_boundary, environment)
    accepted = path.read_bytes()
    assert len(accepted) == maximum_size
    assert load_profile("first", environment)["host"] == "safe.example.invalid"

    with pytest.raises(ConfigurationError, match="size limit"):
        save_profile("second", _profile(), environment)

    assert path.read_bytes() == accepted
    assert load_profile("first", environment)["host"] == "safe.example.invalid"
    save_profile("first", _profile(), environment)
    assert load_profile("first", environment)["host"] == "mqtt.example.invalid"
    assert not tuple(path.parent.glob(".*.tmp"))


def test_profile_store_rejects_symlinks_and_unsafe_permissions(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    path = profile_path(environment)
    path.parent.mkdir(mode=0o700, parents=True)
    target = tmp_path / "target.json"
    target.write_text('{"untouched":true}\n', encoding="utf-8")
    path.symlink_to(target)

    with pytest.raises(ConfigurationError, match="safely"):
        save_profile("sensor", _profile(), environment)
    assert target.read_text(encoding="utf-8") == '{"untouched":true}\n'

    path.unlink()
    save_profile("sensor", _profile(), environment)
    path.chmod(0o644)
    with pytest.raises(ConfigurationError, match="safety checks"):
        load_profile("sensor", environment)


def test_profile_save_preserves_primary_error_when_lock_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    lock_descriptor = 1_000_000
    primary_error = ConfigurationError("primary transaction failure")
    original_close = os.close

    def fail_read(
        _path: Path,
        *,
        missing_ok: bool,
    ) -> profile_module._ProfileDocument:
        del missing_ok
        raise primary_error

    def fail_lock_close(descriptor: int) -> None:
        if descriptor == lock_descriptor:
            raise OSError("synthetic lock close failure")
        original_close(descriptor)

    monkeypatch.setattr(
        profile_module,
        "_acquire_profile_transaction_lock",
        lambda _directory: lock_descriptor,
    )
    monkeypatch.setattr(profile_module, "_read_document", fail_read)
    monkeypatch.setattr(profile_module.os, "close", fail_lock_close)

    with pytest.raises(ConfigurationError) as raised:
        save_profile("sensor", _profile(), environment)

    assert raised.value is primary_error


def test_profile_save_reports_lock_close_failure_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    lock_descriptor = 1_000_000
    original_close = os.close

    def fail_lock_close(descriptor: int) -> None:
        if descriptor == lock_descriptor:
            raise OSError("synthetic lock close failure")
        original_close(descriptor)

    monkeypatch.setattr(
        profile_module,
        "_acquire_profile_transaction_lock",
        lambda _directory: lock_descriptor,
    )
    monkeypatch.setattr(profile_module.os, "close", fail_lock_close)

    with pytest.raises(
        ConfigurationError,
        match="transaction lock could not be released safely",
    ):
        save_profile("sensor", _profile(), environment)


@pytest.mark.skipif(os.name != "posix", reason="named profile persistence is POSIX-only")
def test_profile_transaction_lock_retries_contention_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    directory = tmp_path / "profiles"
    directory.mkdir(mode=0o700)
    now = 0.0
    sleeps: list[float] = []
    operations: list[int] = []

    def monotonic() -> float:
        return now

    def sleeper(delay: float) -> None:
        nonlocal now
        sleeps.append(delay)
        now += delay

    def contend_then_succeed(_descriptor: int, operation: int) -> None:
        operations.append(operation)
        if len(operations) < 3:
            raise BlockingIOError(errno.EAGAIN, "synthetic contention")

    monkeypatch.setattr(fcntl, "flock", contend_then_succeed)

    descriptor = profile_module._acquire_profile_transaction_lock(
        directory,
        _monotonic=monotonic,
        _sleeper=sleeper,
    )
    os.close(descriptor)

    assert operations == [fcntl.LOCK_EX | fcntl.LOCK_NB] * 3
    assert sleeps == [0.05, 0.05]


@pytest.mark.skipif(os.name != "posix", reason="named profile persistence is POSIX-only")
def test_profile_transaction_lock_timeout_is_bounded_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    directory = tmp_path / "profiles"
    directory.mkdir(mode=0o700)
    now = 0.0
    sleeps: list[float] = []
    lock_descriptors: list[int] = []
    closed = _record_descriptor_closes(monkeypatch)

    def monotonic() -> float:
        return now

    def sleeper(delay: float) -> None:
        nonlocal now
        sleeps.append(delay)
        now += delay

    def contend(descriptor: int, operation: int) -> None:
        assert operation == fcntl.LOCK_EX | fcntl.LOCK_NB
        lock_descriptors.append(descriptor)
        raise BlockingIOError(errno.EAGAIN, "synthetic contention")

    monkeypatch.setattr(fcntl, "flock", contend)

    with pytest.raises(
        ConfigurationError,
        match="another process holds the ECN profile transaction lock",
    ):
        profile_module._acquire_profile_transaction_lock(
            directory,
            _monotonic=monotonic,
            _sleeper=sleeper,
        )

    assert sum(sleeps) == pytest.approx(5.0)
    assert sleeps and all(0 < delay <= 0.05 for delay in sleeps)
    assert len(set(lock_descriptors)) == 1
    assert lock_descriptors[0] in closed
    with pytest.raises(OSError):
        os.fstat(lock_descriptors[0])


@pytest.mark.skipif(os.name != "posix", reason="named profile persistence is POSIX-only")
def test_profile_transaction_lock_does_not_retry_noncontention_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    directory = tmp_path / "profiles"
    directory.mkdir(mode=0o700)
    attempts: list[int] = []
    sleeps: list[float] = []
    lock_descriptors: list[int] = []
    closed = _record_descriptor_closes(monkeypatch)

    def fail(descriptor: int, operation: int) -> None:
        attempts.append(operation)
        lock_descriptors.append(descriptor)
        raise OSError(errno.EIO, "synthetic lock failure")

    monkeypatch.setattr(fcntl, "flock", fail)

    with pytest.raises(ConfigurationError, match="transaction lock could not be used safely"):
        profile_module._acquire_profile_transaction_lock(
            directory,
            _monotonic=lambda: 0.0,
            _sleeper=sleeps.append,
        )

    assert attempts == [fcntl.LOCK_EX | fcntl.LOCK_NB]
    assert sleeps == []
    assert lock_descriptors[0] in closed


@pytest.mark.skipif(os.name != "posix", reason="named profile persistence is POSIX-only")
def test_profile_transaction_lock_interrupted_sleep_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    directory = tmp_path / "profiles"
    directory.mkdir(mode=0o700)
    lock_descriptors: list[int] = []
    closed = _record_descriptor_closes(monkeypatch)

    def contend(descriptor: int, _operation: int) -> None:
        lock_descriptors.append(descriptor)
        raise BlockingIOError(errno.EAGAIN, "synthetic contention")

    def interrupt(_delay: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(fcntl, "flock", contend)

    with pytest.raises(KeyboardInterrupt):
        profile_module._acquire_profile_transaction_lock(
            directory,
            _monotonic=lambda: 0.0,
            _sleeper=interrupt,
        )

    assert lock_descriptors[0] in closed
    with pytest.raises(OSError):
        os.fstat(lock_descriptors[0])


@pytest.mark.skipif(os.name != "posix", reason="named profile persistence is POSIX-only")
def test_profile_transaction_lock_preserves_acquisition_error_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    directory = tmp_path / "profiles"
    directory.mkdir(mode=0o700)
    opened: list[int] = []
    close_attempts: list[int] = []
    original_open = os.open
    original_close = os.close
    primary_error = ConfigurationError("primary acquisition failure")

    def tracked_open(*args: object, **kwargs: object) -> int:
        descriptor = original_open(*args, **kwargs)  # type: ignore[arg-type]
        opened.append(descriptor)
        return descriptor

    def fail_acquisition(_descriptor: int, _operation: int) -> None:
        raise primary_error

    def fail_close(descriptor: int) -> None:
        close_attempts.append(descriptor)
        raise OSError("synthetic descriptor close failure")

    monkeypatch.setattr(profile_module.os, "open", tracked_open)
    monkeypatch.setattr(profile_module.os, "close", fail_close)
    monkeypatch.setattr(fcntl, "flock", fail_acquisition)

    try:
        with pytest.raises(ConfigurationError) as raised:
            profile_module._acquire_profile_transaction_lock(directory)
    finally:
        for descriptor in opened:
            with suppress(OSError):
                original_close(descriptor)

    assert raised.value is primary_error
    assert set(opened) <= set(close_attempts)


@pytest.mark.skipif(os.name != "posix", reason="named profile persistence is POSIX-only")
def test_concurrent_profile_saves_are_serialized_and_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = multiprocessing.get_context("fork")
    environment = _environment(tmp_path)
    first_read = context.Event()
    release_first = context.Event()
    second_started = context.Event()
    second_read = context.Event()
    role = {"value": "parent"}
    original_read = profile_module._read_document

    def controlled_read(path: Path, *, missing_ok: bool) -> profile_module._ProfileDocument:
        document = original_read(path, missing_ok=missing_ok)
        if role["value"] == "first":
            first_read.set()
            if not release_first.wait(5):
                raise RuntimeError("timed out while coordinating the first profile writer")
        elif role["value"] == "second":
            second_read.set()
        return document

    def write_profile(writer_role: str, name: str) -> None:
        role["value"] = writer_role
        if writer_role == "second":
            second_started.set()
        save_profile(name, _profile() | {"host": f"{name}.example.invalid"}, environment)

    monkeypatch.setattr(profile_module, "_read_document", controlled_read)
    first = context.Process(target=write_profile, args=("first", "first"))
    second = context.Process(target=write_profile, args=("second", "second"))
    first_exitcode: int | None = None
    second_exitcode: int | None = None
    first.start()
    try:
        assert first_read.wait(5)
        import fcntl

        observer = os.open(profile_path(environment).parent / ".profiles.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(observer)
        second.start()
        assert second_started.wait(5)
        assert not second_read.wait(0.25)
    finally:
        release_first.set()
        first.join(5)
        if second.pid is not None:
            second.join(5)
        for process in (first, second):
            if process.is_alive():
                process.terminate()
                process.join(5)
            if process.is_alive():
                process.kill()
                process.join(5)
        first_exitcode = first.exitcode
        second_exitcode = second.exitcode
        for process in (first, second):
            if not process.is_alive():
                process.close()

    assert first_exitcode == 0
    assert second_exitcode == 0
    assert load_profile("first", environment)["host"] == "first.example.invalid"
    assert load_profile("second", environment)["host"] == "second.example.invalid"
    assert stat_mode(profile_path(environment).parent / ".profiles.lock") == 0o600


@pytest.mark.skipif(os.name != "posix", reason="named profile persistence is POSIX-only")
def test_profile_transaction_lock_rejects_unsafe_file_metadata(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    path = profile_path(environment)
    path.parent.mkdir(mode=0o700, parents=True)
    path.parent.chmod(0o700)
    lock_path = path.parent / ".profiles.lock"
    target = tmp_path / "lock-target"
    target.write_text("unchanged\n", encoding="utf-8")
    target.chmod(0o600)
    lock_path.symlink_to(target)

    with pytest.raises(ConfigurationError, match="transaction lock") as symlink_error:
        save_profile("symlink", _profile(), environment)
    assert target.read_text(encoding="utf-8") == "unchanged\n"
    assert str(lock_path) not in str(symlink_error.value)
    assert str(target) not in str(symlink_error.value)

    lock_path.unlink()
    lock_path.write_text("", encoding="utf-8")
    lock_path.chmod(0o644)
    with pytest.raises(ConfigurationError, match="transaction lock"):
        save_profile("mode", _profile(), environment)

    lock_path.unlink()
    lock_path.write_text("", encoding="utf-8")
    lock_path.chmod(0o600)
    hard_link = tmp_path / "lock-hard-link"
    os.link(lock_path, hard_link)
    with pytest.raises(ConfigurationError, match="transaction lock"):
        save_profile("link-count", _profile(), environment)

    hard_link.unlink()
    lock_path.unlink()
    lock_path.mkdir(mode=0o700)
    with pytest.raises(ConfigurationError, match="transaction lock"):
        save_profile("file-type", _profile(), environment)

    assert not path.exists()


def test_profile_store_rejects_symlinked_application_directory(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    path = profile_path(environment)
    real_directory = tmp_path / "real-profile-directory"
    real_directory.mkdir(mode=0o700)
    path.parent.parent.mkdir(mode=0o700, parents=True)
    path.parent.symlink_to(real_directory)

    with pytest.raises(ConfigurationError, match="safely"):
        save_profile("sensor", _profile(), environment)


def test_profile_store_secures_an_existing_application_directory(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    path = profile_path(environment)
    path.parent.mkdir(mode=0o755, parents=True)
    path.parent.chmod(0o755)

    save_profile("sensor", _profile(), environment)

    assert stat_mode(path.parent) == 0o700
    assert stat_mode(path) == 0o600


def test_malformed_or_duplicate_profile_json_fails_closed(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    path = profile_path(environment)
    path.parent.mkdir(mode=0o700, parents=True)
    path.write_text('{"version":1,"profiles":{},"profiles":{}}', encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ConfigurationError, match="malformed"):
        load_profile("sensor", environment)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("reconnect_policy", "aggressive", "reconnect policy is invalid"),
        ("mqtt_port", True, "ECN MQTT port must be an integer"),
        ("ntp_port", True, "ECN NTP port must be an integer"),
    ],
)
def test_profile_store_rejects_malformed_policy_and_boolean_ports(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    environment = _environment(tmp_path)
    path = profile_path(environment)
    path.parent.mkdir(mode=0o700, parents=True)
    document = {"version": 1, "profiles": {"sensor": _profile() | {field: value}}}
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(ConfigurationError, match=message):
        load_profile("sensor", environment)


def test_deeply_nested_profile_json_fails_closed(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    path = profile_path(environment)
    path.parent.mkdir(mode=0o700, parents=True)
    nesting = 10_000
    path.write_bytes(b'{"version":1,"profiles":' + b"[" * nesting + b"0" + b"]" * nesting + b"}")
    path.chmod(0o600)

    with pytest.raises(ConfigurationError, match="profile store is malformed"):
        load_profile("sensor", environment)


def test_profile_json_nesting_bound_is_exact(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    path = profile_path(environment)
    path.parent.mkdir(mode=0o700, parents=True)

    def _write(nesting: int) -> None:
        path.write_bytes(
            b'{"version":1,"profiles":' + b"[" * nesting + b"0" + b"]" * nesting + b"}"
        )
        path.chmod(0o600)

    # The document object plus 63 arrays reaches the 64-level bound, so the
    # nesting guard admits the payload and structural validation rejects it.
    _write(63)
    with pytest.raises(ConfigurationError, match="version is unsupported"):
        load_profile("sensor", environment)

    # One more level exceeds the bound and fails closed as malformed.
    _write(64)
    with pytest.raises(ConfigurationError, match="profile store is malformed"):
        load_profile("sensor", environment)


def test_oversized_profile_store_fails_closed(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    path = profile_path(environment)
    path.parent.mkdir(mode=0o700, parents=True)
    path.write_bytes(b" " * (1024 * 1024 + 1))
    path.chmod(0o600)

    with pytest.raises(ConfigurationError, match="profile store is too large"):
        load_profile("sensor", environment)


def test_unknown_profile_store_version_fails_closed(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    path = profile_path(environment)
    path.parent.mkdir(mode=0o700, parents=True)
    path.write_text('{"version":2,"profiles":{}}', encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(ConfigurationError, match="version is unsupported"):
        load_profile("sensor", environment)


def test_profile_resolution_prefers_cli_then_environment() -> None:
    environment = {"ECN_PROFILE": "from-environment"}
    assert resolve_profile_name("from-cli", environment) == "from-cli"
    assert resolve_profile_name(None, environment) == "from-environment"
    assert resolve_profile_name(None, {}) is None


def test_serialized_profile_document_has_no_environment_token(tmp_path: Path) -> None:
    environment = _environment(tmp_path) | {"ECN_BEARER_TOKEN": "secret-canary"}
    save_profile("sensor", _profile(), environment)
    document = json.loads(profile_path(environment).read_text(encoding="utf-8"))
    assert "secret-canary" not in json.dumps(document)


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
