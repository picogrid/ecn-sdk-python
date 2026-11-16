# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts import scan_container_image as scanner

IMAGE_ID = "sha256:" + "a" * 64


def report_with(*findings: dict[str, Any]) -> dict[str, Any]:
    return {
        "SchemaVersion": 2,
        "ArtifactType": "container_image",
        "Metadata": {"ImageID": IMAGE_ID, "ImageConfig": {"config": {"Env": ["TEST=private"]}}},
        "Results": [{"Vulnerabilities": list(findings)}],
    }


@pytest.mark.parametrize(
    ("severity", "fixed", "blocked"),
    [
        ("UNKNOWN", "2.0", 0),
        ("LOW", "2.0", 0),
        ("MEDIUM", "2.0", 0),
        ("HIGH", "2.0", 1),
        ("CRITICAL", "2.0", 1),
        ("HIGH", None, 0),
        ("HIGH", "", 0),
        ("HIGH", "  \t", 0),
        ("CRITICAL", "", 0),
    ],
)
def test_gate_requires_both_high_severity_and_a_nonblank_fix(
    severity: str, fixed: str | None, blocked: int
) -> None:
    report = report_with(
        {"VulnerabilityID": "CVE-TEST", "Severity": severity, "FixedVersion": fixed}
    )

    assert scanner.normalize_report(report, IMAGE_ID) == blocked
    assert len(report["Results"][0]["Vulnerabilities"]) == 1
    assert "ImageConfig" not in report["Metadata"]


def test_highest_vendor_severity_raises_but_never_lowers_the_gate() -> None:
    report = report_with(
        {
            "VulnerabilityID": "CVE-RAISED",
            "Severity": "LOW",
            "SeveritySource": "debian",
            "VendorSeverity": {"debian": 1, "nvd": 3},
            "FixedVersion": "2.0",
        },
        {
            "VulnerabilityID": "CVE-NOT-LOWERED",
            "Severity": "CRITICAL",
            "VendorSeverity": {"debian": 1},
            "FixedVersion": "2.0",
        },
    )

    assert scanner.normalize_report(report, IMAGE_ID) == 2
    raised, critical = report["Results"][0]["Vulnerabilities"]
    assert raised["Severity"] == "HIGH"
    assert raised["PicogridSeverity"]["originalSeverity"] == "LOW"
    assert raised["VendorSeverity"] == {"debian": 1, "nvd": 3}
    assert critical["Severity"] == "CRITICAL"


@pytest.mark.parametrize("rank", [True, -1, 5, "HIGH", None])
def test_invalid_vendor_severity_fails_closed(rank: object) -> None:
    report = report_with(
        {"VulnerabilityID": "CVE-TEST", "Severity": "HIGH", "VendorSeverity": {"nvd": rank}}
    )

    with pytest.raises(ValueError, match="Malformed Trivy vulnerability finding"):
        scanner.normalize_report(report, IMAGE_ID)


def test_report_must_identify_the_inspected_local_image() -> None:
    with pytest.raises(ValueError, match="inspected local image ID"):
        scanner.normalize_report(report_with(), "sha256:" + "b" * 64)


@pytest.mark.parametrize(
    "configuration",
    [
        {},
        {
            "TRIVY_CONFIG": ".trivy.yaml",
            "TRIVY_IGNOREFILE": ".trivyignore.yaml",
            "TRIVY_IGNORE_POLICY": ".trivy-policy.rego",
            "TRIVY_VEX": ".vex.json",
            "TRIVY_SECRET_CONFIG": ".trivy-secret.yaml",
            "TRIVY_SKIP_DIRS": "tests",
        },
    ],
)
def test_gate_retains_all_findings_in_json_and_sarif_before_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configuration: dict[str, str]
) -> None:
    report = report_with(
        {"VulnerabilityID": "CVE-FIXED", "Severity": "HIGH", "FixedVersion": "2.0"},
        {"VulnerabilityID": "CVE-UNFIXED", "Severity": "CRITICAL", "FixedVersion": ""},
        {"VulnerabilityID": "CVE-LOW", "Severity": "LOW", "FixedVersion": "2.0"},
    )

    environment = {
        **configuration,
        "TRIVY_OUTPUT": str(tmp_path / "unsafe.json"),
        "TRIVY_FORMAT": "table",
        "TRIVY_IGNORE_STATUS": "fixed",
        "TRIVY_CACHE_DIR": str(tmp_path / "cache"),
    }
    expected_environment = {
        key: value
        for key, value in environment.items()
        if key not in {"TRIVY_OUTPUT", "TRIVY_IGNORE_STATUS"}
    }

    def output(command: list[str], **options: object) -> str:
        if command[0] == "docker":
            return IMAGE_ID
        assert options["cwd"] == Path(scanner.__file__).resolve().parents[1]
        assert options["env"] == expected_environment
        assert command[-1] == IMAGE_ID
        assert command[command.index("--output") + 1] == ""
        assert command[command.index("--format") + 1] == "json"
        assert command[command.index("--exit-code") + 1] == "0"
        assert command[command.index("--severity") + 1] == ",".join(scanner.LEVELS)
        assert command[command.index("--scanners") + 1] == "vuln"
        assert "--ignore-unfixed=false" in command
        assert not {"--config", "--ignorefile", "--ignore-policy", "--vex"} & set(command)
        return json.dumps(report)

    def convert(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        assert options["cwd"] == Path(scanner.__file__).resolve().parents[1]
        assert options["env"] == expected_environment
        scan = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        assert "ImageConfig" not in scan["Metadata"]
        findings = scan["Results"][0]["Vulnerabilities"]
        sarif = {
            "version": "2.1.0",
            "runs": [{"results": [{"ruleId": item["VulnerabilityID"]} for item in findings]}],
        }
        Path(command[command.index("--output") + 1]).write_text(json.dumps(sarif), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(scanner.subprocess, "check_output", output)
    monkeypatch.setattr(scanner.subprocess, "run", convert)

    with pytest.raises(scanner.ImageScanError, match="1 fixable HIGH/CRITICAL"):
        scanner.scan_image("operator:test", tmp_path, environment=environment)

    saved = json.loads((tmp_path / "image.scan.json").read_text(encoding="utf-8"))
    assert "ImageConfig" not in saved["Metadata"]
    assert len(saved["Results"][0]["Vulnerabilities"]) == 3
    assert len(json.loads((tmp_path / "image.sarif").read_text())["runs"][0]["results"]) == 3
