# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Exercise the installed profile CLI without source imports or network access."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(
    command: Path, arguments: list[str], environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(command), *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        timeout=15,
    )


def verify_installed_cli() -> None:
    """Require installed entry-point, private profile storage, and zero-network doctor."""

    command = Path(sys.executable).parent / "picogrid-ecn"
    if not command.is_file():
        raise RuntimeError("installed profile console script was not found")

    base_environment = os.environ.copy()
    base_environment.pop("PYTHONPATH", None)
    for name in tuple(base_environment):
        if name.startswith("ECN_") or name in {"LEGION_AUTH_STORAGE_PATH", "STORAGE_PATH"}:
            base_environment.pop(name)
    canary = "synthetic-installed-cli-token"

    with tempfile.TemporaryDirectory(prefix="picogrid-ecn-installed-cli-") as raw_directory:
        temporary = Path(raw_directory)
        environment = base_environment | {
            "XDG_CONFIG_HOME": str(temporary / "configuration"),
            "ECN_BEARER_TOKEN": canary,
        }

        version = _run(command, ["--version"], environment)
        if version.returncode != 0 or not version.stdout.strip():
            raise RuntimeError("installed profile console version check failed")

        configured = _run(
            command,
            [
                "configure",
                "--profile",
                "installed-check",
                "--host",
                "broker.example.invalid",
                "--integration-name",
                "operator-check",
                "--auth",
                "bearer",
                "--mqtt-username",
                "00000000-0000-4000-8000-000000000001",
                "--non-interactive",
            ],
            environment,
        )
        rendered_configure = configured.stdout + configured.stderr
        if configured.returncode != 0 or canary in rendered_configure:
            raise RuntimeError("installed profile configuration failed secret-safety checks")

        profile_path = temporary / "configuration" / "picogrid" / "ecn-sdk" / "profiles.json"
        if not profile_path.is_file():
            raise RuntimeError("installed profile command did not create its profile")
        if stat.S_IMODE(profile_path.stat().st_mode) != 0o600:
            raise RuntimeError("installed profile file permissions are not restrictive")
        if stat.S_IMODE(profile_path.parent.stat().st_mode) != 0o700:
            raise RuntimeError("installed profile directory permissions are not restrictive")
        profile_document = profile_path.read_text(encoding="utf-8")
        if canary in profile_document or "token" in profile_document.casefold():
            raise RuntimeError("installed profile persisted a secret")

        doctor = _run(command, ["doctor", "--profile", "installed-check"], environment)
        rendered_doctor = doctor.stdout + doctor.stderr
        if doctor.returncode != 0 or canary in rendered_doctor:
            raise RuntimeError("installed profile doctor failed secret-safety checks")
        report = json.loads(doctor.stdout)
        if not isinstance(report, dict) or report.get("ready") is not True:
            raise RuntimeError("installed profile doctor returned an invalid report")


def main() -> int:
    verify_installed_cli()
    print("installed profile CLI passed without network access or secret persistence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
