# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Run structured public ECN connectivity and authorization diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime

from picogrid_ecn_client import (
    CheckStatus,
    ECNClient,
    PreflightCheck,
    PreflightCheckName,
    PreflightReport,
)
from picogrid_ecn_client.workflows import preflight

if __package__:
    from ._common import ExampleConfigurationError, emit, load_config, run_example
else:
    from _common import (  # type: ignore[import-not-found,no-redef]
        ExampleConfigurationError,
        emit,
        load_config,
        run_example,
    )


async def main() -> None:
    client = ECNClient(load_config())
    try:
        result = await preflight(client)
    finally:
        await client.close()
    emit(result.report)
    if not result.report.successful:
        raise ExampleConfigurationError("one or more required preflight checks failed")


def _check() -> None:
    generated_at = datetime(2026, 1, 1, tzinfo=UTC)
    check = PreflightCheck(
        name=PreflightCheckName.CONFIGURATION,
        status=CheckStatus.PASS,
        duration_ms=0,
        detail="offline example validation",
    )
    report = PreflightReport(
        generated_at=generated_at,
        successful=True,
        ready=True,
        checks=(check,),
    )
    if not report.successful:
        raise AssertionError("preflight model validation failed")


if __name__ == "__main__":
    run_example("preflight", main, _check)
