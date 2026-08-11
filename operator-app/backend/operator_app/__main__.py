# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Run the operator backend with conservative loopback defaults."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

import uvicorn


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="picogrid-ecn-operator")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--profile", help="named ECN profile for live operation")
    selection.add_argument("--demo", action="store_true", help="run the offline synthetic map")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    if arguments.profile is not None:
        os.environ["ECN_PROFILE"] = arguments.profile
        os.environ["OPERATOR_MODE"] = "live"
    elif arguments.demo:
        os.environ.pop("ECN_PROFILE", None)
        os.environ["OPERATOR_MODE"] = "mock"
        os.environ["OPERATOR_ECN_CLIENT_INTEGRATION"] = "operator-console"
        os.environ["OPERATOR_ECN_INTEGRATION_ALLOWLIST"] = "mock-sensor,mock-target"
        os.environ["OPERATOR_ECN_CATEGORY_ALLOWLIST"] = "TRACK,DETECTION,DEVICE"
        os.environ["OPERATOR_ECN_WIRE_FORMAT"] = "json"
        os.environ["OPERATOR_TASKING_ENABLED"] = "false"
        os.environ.pop("OPERATOR_COMMANDS_FILE", None)
        os.environ.pop("OPERATOR_TASK_ENTITY_ALLOWLIST", None)
        os.environ.pop("OPERATOR_BASEMAP_URL_TEMPLATE", None)
        os.environ.pop("OPERATOR_BASEMAP_ATTRIBUTION", None)

    host = os.environ.get("OPERATOR_HTTP_BIND", "127.0.0.1")
    container_bind = os.environ.get("OPERATOR_ALLOW_CONTAINER_BIND", "false").casefold() == "true"
    if host != "127.0.0.1" and not (host == "0.0.0.0" and container_bind):
        raise RuntimeError(
            "OPERATOR_HTTP_BIND must be loopback unless container wildcard binding is explicit"
        )
    uvicorn.run(
        "operator_app.app:app",
        host=host,
        port=8080,
        log_level="info",
        access_log=False,
        ws_max_size=1_024,
        ws_max_queue=4,
    )


if __name__ == "__main__":
    main()
