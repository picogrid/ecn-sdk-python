# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Retain a complete local-image scan before rejecting patched HIGH/CRITICAL CVEs."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

LEVELS = ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")
POLICY = "picogrid-highest-reported-v1"


class ImageScanError(RuntimeError):
    """Raised when a local image has fixable high-severity vulnerabilities."""


def normalize_report(report: dict[str, Any], image_id: str) -> int:
    """Apply the highest reported vendor severity and count fixable blockers."""
    if (
        report.get("SchemaVersion") != 2
        or report.get("ArtifactType") != "container_image"
        or not isinstance(report.get("Metadata"), dict)
        or report["Metadata"].get("ImageID") != image_id
        or not isinstance(report.get("Results", []), list)
    ):
        raise ValueError("Expected a Trivy report for the inspected local image ID")
    report["Metadata"].pop("ImageConfig", None)
    blocked = 0
    for result in report.get("Results", []):
        if not isinstance(result, dict) or not isinstance(result.get("Vulnerabilities", []), list):
            raise ValueError("Malformed Trivy vulnerability results")
        for finding in result.get("Vulnerabilities", []):
            if not isinstance(finding, dict) or finding.get("Severity") not in LEVELS:
                raise ValueError("Malformed Trivy vulnerability severity")
            original = finding["Severity"]
            ranks = finding.get("VendorSeverity", {})
            fixed = finding.get("FixedVersion")
            if (
                not isinstance(ranks, dict)
                or any(
                    type(rank) is not int or rank not in range(len(LEVELS))
                    for rank in ranks.values()
                )
                or not isinstance(finding.get("VulnerabilityID"), str)
                or not isinstance(fixed, (str, type(None)))
            ):
                raise ValueError("Malformed Trivy vulnerability finding")
            highest = max([LEVELS.index(original), *ranks.values()])
            sources = [source for source, rank in ranks.items() if rank == highest]
            if LEVELS.index(original) == highest:
                sources.append(finding.get("SeveritySource") or "trivy-selected")
            finding["PicogridSeverity"] = {
                "policy": POLICY,
                "originalSeverity": original,
                "originalSeveritySource": finding.get("SeveritySource"),
                "highestSources": sorted(set(sources)),
            }
            finding["Severity"] = LEVELS[highest]
            finding["SeveritySource"] = POLICY
            blocked += bool(
                isinstance(fixed, str) and fixed.strip() and highest >= LEVELS.index("HIGH")
            )
    return blocked


def scan_image(image: str, report_directory: Path, *, environment: Mapping[str, str]) -> str:
    """Scan one immutable Docker ID; return it only after writing JSON and SARIF."""
    report_directory = report_directory.resolve()
    report_directory.mkdir(parents=True, exist_ok=True)
    scan_path = report_directory / "image.scan.json"
    sarif_path = report_directory / "image.sarif"
    for path in (scan_path, sarif_path):
        path.unlink(missing_ok=True)
    # Keep repository exceptions, but prevent ambient output/status gate bypasses.
    scanner_environment = {
        key: value
        for key, value in environment.items()
        if key not in {"TRIVY_OUTPUT", "TRIVY_IGNORE_STATUS"}
    }
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", "--", image],
        env=scanner_environment,
        text=True,
        timeout=30,
    ).strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("Expected one immutable local Docker image ID")
    root = Path(__file__).resolve().parents[1]
    report = json.loads(
        subprocess.check_output(
            [
                "trivy",
                "image",
                "--image-src",
                "docker",
                "--scanners",
                "vuln",
                "--severity",
                ",".join(LEVELS),
                "--ignore-unfixed=false",
                "--list-all-pkgs",
                "--exit-code",
                "0",
                "--format",
                "json",
                "--output",
                "",
                image_id,
            ],
            cwd=root,
            env=scanner_environment,
            text=True,
            timeout=900,
        )
    )
    if not isinstance(report, dict):
        raise ValueError("Expected a Trivy JSON object")
    blocked = normalize_report(report, image_id)
    scan_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    subprocess.run(
        [
            "trivy",
            "convert",
            "--format",
            "sarif",
            "--severity",
            ",".join(LEVELS),
            "--exit-code",
            "0",
            "--output",
            str(sarif_path),
            str(scan_path),
        ],
        cwd=root,
        env=scanner_environment,
        check=True,
        timeout=300,
    )
    sarif = json.loads(sarif_path.read_text(encoding="utf-8"))
    if (
        sarif.get("version") != "2.1.0"
        or not isinstance(sarif.get("runs"), list)
        or len(sarif["runs"]) != 1
    ):
        raise ValueError("Expected one Trivy SARIF run")
    if blocked:
        raise ImageScanError(
            f"{blocked} fixable HIGH/CRITICAL image findings; reports: {report_directory}"
        )
    return image_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--report-directory", type=Path, required=True)
    arguments = parser.parse_args()
    scan_image(arguments.image, arguments.report_directory, environment=os.environ)


if __name__ == "__main__":
    main()
