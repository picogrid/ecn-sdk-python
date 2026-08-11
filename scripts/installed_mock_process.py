# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Start and stop the installed mock CLI, proving process and socket cleanup."""

from __future__ import annotations

import argparse
import os
import queue
import re
import socket
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import IO

_MQTT_LINE = re.compile(r"^Mock ECN MQTT v5 listening on 127\.0\.0\.1:(\d+)$")


def _read_lines(stream: IO[str], output: queue.Queue[str]) -> None:
    try:
        for line in stream:
            output.put(line.rstrip("\n"))
    finally:
        stream.close()


def _next_line(output: queue.Queue[str]) -> str:
    try:
        return output.get(timeout=10)
    except queue.Empty as exc:
        raise RuntimeError("installed mock did not report readiness within 10 seconds") from exc


def _assert_connectable(port: int) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        return


def _assert_released(port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()


def verify_installed_mock() -> None:
    """Launch the console script on ephemeral ports and require clean termination."""

    command = Path(sys.executable).parent / "picogrid-mock-ecn"
    if not command.is_file():
        raise RuntimeError("installed mock console script was not found")
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    process = subprocess.Popen(
        [str(command), "--host", "127.0.0.1", "--mqtt-port", "0"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("could not capture installed mock output")
    output: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(target=_read_lines, args=(process.stdout, output), daemon=True)
    reader.start()
    mqtt_port = 0
    try:
        mqtt_match = _MQTT_LINE.fullmatch(_next_line(output))
        if mqtt_match is None:
            raise RuntimeError("installed mock emitted an unexpected readiness contract")
        mqtt_port = int(mqtt_match.group(1))
        _assert_connectable(mqtt_port)
    finally:
        process.terminate()
        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise RuntimeError("installed mock did not stop after termination") from None
        stderr = process.stderr.read()
        reader.join(timeout=2)
        if return_code != 0:
            raise RuntimeError(f"installed mock exited with {return_code}: {stderr[:500]}")
    if not mqtt_port:
        raise RuntimeError("installed mock did not bind its MQTT endpoint")
    _assert_released(mqtt_port)


def main(argv: Sequence[str] | None = None) -> int:
    argparse.ArgumentParser().parse_args(argv)
    verify_installed_mock()
    print("installed mock process and socket cleaned up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
