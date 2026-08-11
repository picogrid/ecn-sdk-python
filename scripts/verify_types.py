# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Verify consumer typing fixtures against source or pristine built wheels."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MARKER = "# expect-type-error"
_PYTHON_MINORS = ("3.11", "3.12", "3.13", "3.14")
_MYPY_DIAGNOSTIC = re.compile(r"^(?P<path>.+\.py):(?P<line>\d+)(?::\d+)?: error: (?P<message>.*)$")


@dataclass(frozen=True, order=True)
class Diagnostic:
    """One checker error at a consumer-fixture source line."""

    path: str
    line: int
    message: str


@dataclass(frozen=True)
class Reconciliation:
    """Difference between expected-error markers and checker diagnostics."""

    missing: tuple[tuple[str, int], ...]
    unexpected: tuple[Diagnostic, ...]

    @property
    def matches(self) -> bool:
        return not self.missing and not self.unexpected


def _statement_spans(source: str) -> list[tuple[int, int]]:
    """Return (start, end) line spans for every statement in a fixture module."""

    spans: list[tuple[int, int]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.stmt):
            continue
        end = getattr(node, "end_lineno", None)
        if end is not None:
            spans.append((node.lineno, end))
    return spans


def _statement_start(spans: Sequence[tuple[int, int]], line: int) -> int:
    """Map a line to the start of the innermost statement containing it.

    mypy and Pyright report a rejected call at different positions -- the call
    expression, one argument, or the assignment target. Attributing every
    diagnostic to its enclosing statement keeps the contract "this statement is
    rejected" instead of pinning checker-specific column/line behavior.
    """

    enclosing = [span for span in spans if span[0] <= line <= span[1]]
    if not enclosing:
        return line
    return max(enclosing, key=lambda span: span[0])[0]


def _fixture_spans(root: Path) -> dict[str, list[tuple[int, int]]]:
    return {
        path.relative_to(root).as_posix(): _statement_spans(path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("*.py"))
    }


def expected_error_markers(root: Path) -> set[tuple[str, int]]:
    """Return fixture-relative statements carrying the exact expected-error marker."""

    spans = _fixture_spans(root)
    markers: set[tuple[str, int]] = set()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.rstrip().endswith(_MARKER):
                markers.add((relative, _statement_start(spans.get(relative, []), line_number)))
    return markers


def reconcile_diagnostics(
    markers: set[tuple[str, int]],
    diagnostics: Iterable[Diagnostic],
    spans: Mapping[str, Sequence[tuple[int, int]]] | None = None,
) -> Reconciliation:
    """Require at least one error per marked statement and reject all others."""

    spans = spans or {}
    diagnostics_tuple = tuple(diagnostics)

    def location(diagnostic: Diagnostic) -> tuple[str, int]:
        return (
            diagnostic.path,
            _statement_start(spans.get(diagnostic.path, []), diagnostic.line),
        )

    actual = {location(diagnostic) for diagnostic in diagnostics_tuple}
    return Reconciliation(
        missing=tuple(sorted(markers - actual)),
        unexpected=tuple(
            sorted(
                diagnostic
                for diagnostic in diagnostics_tuple
                if location(diagnostic) not in markers
            )
        ),
    )


def normalize_report(value: Any) -> Any:
    """Remove unstable timing and path fields and sort all report mappings."""

    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key in sorted(value):
            if key in {
                "time",
                "timeInSec",
                "timing",
                "packageRootDirectory",
                "moduleRootDirectory",
                "pyTypedPath",
            }:
                continue
            normalized[str(key)] = normalize_report(value[key])
        return normalized
    if isinstance(value, list):
        return [normalize_report(item) for item in value]
    if isinstance(value, Path):
        return value.name
    if isinstance(value, str) and Path(value).is_absolute():
        return Path(value).name
    return value


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        check=False,
        capture_output=True,
        text=True,
    )


def _fixture_path(raw_path: str, fixture_root: Path) -> str:
    path = Path(raw_path)
    try:
        return path.resolve().relative_to(fixture_root.resolve()).as_posix()
    except ValueError:
        parts = path.parts
        if "positive" in parts:
            return Path(*parts[parts.index("positive") :]).as_posix()
        if "negative" in parts:
            return Path(*parts[parts.index("negative") :]).as_posix()
        return path.name


def _parse_mypy(output: str, fixture_root: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for line in output.splitlines():
        match = _MYPY_DIAGNOSTIC.match(line)
        if match is None:
            continue
        diagnostics.append(
            Diagnostic(
                _fixture_path(match.group("path"), fixture_root),
                int(match.group("line")),
                match.group("message"),
            )
        )
    return diagnostics


def _parse_pyright(output: str, fixture_root: Path) -> tuple[list[Diagnostic], dict[str, Any]]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"pyright did not emit JSON: {error.msg}") from error
    diagnostics = []
    for item in payload.get("generalDiagnostics", []):
        if item.get("severity") != "error":
            continue
        diagnostics.append(
            Diagnostic(
                _fixture_path(str(item.get("file", "")), fixture_root),
                int(item["range"]["start"]["line"]) + 1,
                str(item.get("message", "")),
            )
        )
    return diagnostics, payload


def _checker_version(command: Sequence[str], cwd: Path) -> str:
    result = _run([*command, "--version"], cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "checker version command failed")
    return result.stdout.strip()


def _resolve_pyright(repository_root: Path, explicit: Path | None = None) -> Path:
    """Locate the pinned pyright executable.

    The documentation workspace's `docs/node_modules/.bin/pyright` is preferred
    over anything on PATH so the advertised gate uses the package-lock-pinned
    checker rather than whatever global version a contributor happens to have
    installed.
    """

    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    candidates.append(repository_root / "docs" / "node_modules" / ".bin" / "pyright")
    discovered = shutil.which("pyright")
    if discovered is not None:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError(
        "the pinned pyright executable was not found; install the locked Node "
        "toolchain with 'npm --prefix docs ci' or pass --pyright"
    )


def _resolve_mypy(repository_root: Path) -> Path:
    """Locate mypy for the running gate.

    `make verify-types` runs under `uv run --isolated`, so the lock-resolved
    checker lives beside the running interpreter rather than in a project
    `.venv`. The active environment therefore wins; a project `.venv` is only a
    fallback for direct invocations, and PATH is the last resort.
    """

    interpreter_mypy = Path(sys.executable).with_name("mypy")
    if interpreter_mypy.exists():
        return interpreter_mypy
    project_mypy = repository_root / ".venv" / "bin" / "mypy"
    if project_mypy.exists():
        return project_mypy
    discovered = shutil.which("mypy")
    if discovered is not None:
        return Path(discovered)
    raise RuntimeError("mypy was not found in the active environment")


def strict_pyright_project(directory: Path, python_minor: str) -> Path:
    """Write a throwaway Pyright project that forces strict consumer checking.

    Pyright exposes no strict command-line flag, so release fixtures would
    otherwise run in basic mode and miss strict-only diagnostics.
    """

    project = directory / "pyrightconfig.json"
    project.write_text(
        json.dumps({"typeCheckingMode": "strict", "pythonVersion": python_minor}),
        encoding="utf-8",
    )
    return project


def _run_fixture_battery(
    *,
    repository_root: Path,
    fixture_root: Path,
    cwd: Path,
    python_minor: str,
    python_executable: Path,
    source: bool,
    pyright: Path,
) -> tuple[dict[str, Any], list[str]]:
    mypy = _resolve_mypy(repository_root)
    failures: list[str] = []
    checker_counts: dict[str, Any] = {}
    base_env = os.environ.copy()
    if source:
        base_env["MYPYPATH"] = str(repository_root / "src")
        base_env["PYTHONPATH"] = str(repository_root / "src")
    else:
        base_env.pop("PYTHONPATH", None)
        base_env.pop("MYPYPATH", None)
        venv = python_executable.parent.parent
        base_env["VIRTUAL_ENV"] = str(venv)
        base_env["PATH"] = os.pathsep.join((str(venv / "bin"), base_env["PATH"]))

    marker_root = fixture_root
    markers = expected_error_markers(marker_root)
    spans = _fixture_spans(marker_root)
    positive = fixture_root / "positive"
    negative = fixture_root / "negative"

    mypy_common = [
        str(mypy),
        "--strict",
        "--show-column-numbers",
        "--no-pretty",
        "--no-color-output",
        "--python-version",
        python_minor,
    ]
    if not source:
        mypy_common.extend(
            ["--config-file=/dev/null", "--python-executable", str(python_executable)]
        )
    for label, target in (("positive", positive), ("negative", negative)):
        result = _run([*mypy_common, str(target)], cwd=cwd, env=base_env)
        diagnostics = _parse_mypy(result.stdout + result.stderr, marker_root)
        checker_counts[f"mypy_{label}_errors"] = len(diagnostics)
        if label == "positive":
            if diagnostics or result.returncode != 0:
                failures.append(f"mypy positive fixtures produced {len(diagnostics)} errors")
        else:
            reconciliation = reconcile_diagnostics(markers, diagnostics, spans)
            if not reconciliation.matches:
                failures.append(_format_reconciliation("mypy", reconciliation))

    for label, target in (("positive", positive), ("negative", negative)):
        # Pyright has no strict CLI flag and rejects absolute paths in a config
        # "include" array, so the battery writes a throwaway strict project and
        # names the target on the command line, which overrides include only.
        with tempfile.TemporaryDirectory(prefix="ecn-sdk-pyright-") as project_directory:
            project = strict_pyright_project(Path(project_directory), python_minor)
            result = _run(
                [str(pyright), "--outputjson", "--project", str(project), str(target)],
                cwd=cwd,
                env=base_env,
            )
        diagnostics, _ = _parse_pyright(result.stdout, marker_root)
        checker_counts[f"pyright_{label}_errors"] = len(diagnostics)
        if label == "positive":
            if diagnostics or result.returncode != 0:
                failures.append(f"pyright positive fixtures produced {len(diagnostics)} errors")
        else:
            reconciliation = reconcile_diagnostics(markers, diagnostics, spans)
            if not reconciliation.matches:
                failures.append(_format_reconciliation("pyright", reconciliation))
    checker_counts["expected_error_markers"] = len(markers)
    return checker_counts, failures


def _format_reconciliation(checker: str, reconciliation: Reconciliation) -> str:
    missing = ",".join(f"{path}:{line}" for path, line in reconciliation.missing) or "none"
    unexpected = (
        ",".join(f"{item.path}:{item.line}" for item in reconciliation.unexpected) or "none"
    )
    return f"{checker} negative mismatch (missing={missing}; unexpected={unexpected})"


# D051 restricts the completeness allowlist to framework-generated symbols. This is
# the exact Pydantic BaseModel member set, not a name pattern: a pattern such as
# `model_*` would also admit an SDK-authored `model_export`, which the decision
# forbids. Overriding `model_config` in a public model is still framework-generated
# because the name belongs to Pydantic's own API.
_FRAMEWORK_MEMBERS: frozenset[str] = frozenset(
    {
        "model_computed_fields",
        "model_config",
        "model_extra",
        "model_fields",
        "model_fields_set",
        "model_post_init",
    }
)


def _pydantic_model_owners(repository_root: Path) -> frozenset[str]:
    """Return the fully qualified names of manifest-approved Pydantic models.

    Names are qualified so an allowlist entry cannot match a same-named class in
    a different module.
    """

    manifest = json.loads(
        (repository_root / "scripts" / "public-api-manifest.json").read_text(encoding="utf-8")
    )
    symbols = [*manifest["symbols"], *manifest["testing_symbols"]]
    return frozenset(
        f"{symbol['module']}.{symbol['name']}"
        for symbol in symbols
        if symbol["kind"] == "pydantic-model"
    )


def _require_framework_generated(name: str, owners: frozenset[str]) -> None:
    """Reject an allowlist entry that is not a Pydantic member of a public model.

    Both halves matter: the member must belong to Pydantic's own BaseModel API,
    and its owner must be an approved Pydantic model. A non-model class that
    happens to define `model_fields` therefore cannot be excused.
    """

    owner, _, member = name.rpartition(".")
    if member not in _FRAMEWORK_MEMBERS:
        raise RuntimeError(
            f"type completeness allowlist entry {name!r} is not framework-generated; "
            "SDK-authored methods, fields, aliases, and callbacks must be typed "
            "instead of allowlisted"
        )
    if owner not in owners:
        raise RuntimeError(
            f"type completeness allowlist entry {name!r} does not belong to an approved "
            "Pydantic model; only framework-generated members of manifest models qualify"
        )


def _load_allowlist(path: Path, owners: frozenset[str] | None = None) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise RuntimeError("type completeness allowlist must be a JSON array")
    entries: dict[str, str] = {}
    for item in payload:
        if not isinstance(item, dict) or set(item) != {"name", "reason"}:
            raise RuntimeError("allowlist entries require exactly name and reason")
        name, reason = item["name"], item["reason"]
        if not isinstance(name, str) or not name or not isinstance(reason, str) or not reason:
            raise RuntimeError("allowlist names and reasons must be non-empty strings")
        if name in entries:
            raise RuntimeError(f"duplicate type completeness allowlist entry: {name}")
        _require_framework_generated(name, owners if owners is not None else frozenset())
        entries[name] = reason
    return entries


def _completeness_result(
    payload: Mapping[str, Any], allowlist: Mapping[str, str]
) -> tuple[dict[str, Any], list[str]]:
    completeness = payload["typeCompleteness"]
    counts = completeness["exportedSymbolCounts"]
    remaining = sorted(
        symbol["name"]
        for symbol in completeness.get("symbols", [])
        if symbol.get("isExported") and not symbol.get("isTypeKnown")
    )
    allowlist_hits = sorted(name for name in remaining if name in allowlist)
    failures = []
    # The allowlist is an exact exception policy for framework-generated
    # *ambiguity* only. A wholly unknown exported type is never acceptable on
    # the supported surface, so it fails even when the symbol is allowlisted.
    unknown_count = int(counts["withUnknownType"])
    if unknown_count:
        failures.append(
            f"verifytypes reports {unknown_count} exported symbols with wholly unknown type; "
            "the completeness allowlist covers ambiguous symbols only"
        )
    unlisted = sorted(set(remaining) - set(allowlist))
    stale = sorted(set(allowlist) - set(remaining))
    if unlisted:
        failures.append("unlisted incomplete symbols: " + ", ".join(unlisted))
    if stale:
        failures.append("stale completeness allowlist entries: " + ", ".join(stale))
    return (
        {
            "allowlist_hits": allowlist_hits,
            "completeness_score": float(completeness["completenessScore"]),
            "exported_symbols": {
                "ambiguous": int(counts["withAmbiguousType"]),
                "known": int(counts["withKnownType"]),
                "unknown": unknown_count,
            },
        },
        failures,
    )


def _export_worktree(repository_root: Path, destination: Path) -> None:
    """Copy the current working tree into a pristine build directory.

    The gate validates the candidate a contributor is about to commit, so it
    must not archive HEAD and silently test the previously committed tree.
    Tracked and new-but-unignored files are exported; ignored build output and
    the git directory are excluded. Tracked files deleted in the working tree
    are dropped, because the index still lists them and `tar` cannot stat them.
    """

    listing = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--deduplicate"],
        cwd=repository_root,
        check=False,
        capture_output=True,
    )
    if listing.returncode != 0:
        raise RuntimeError("failed to list the working tree for the candidate build")
    present = [
        name
        for name in listing.stdout.split(b"\0")
        if name and os.path.lexists(repository_root / os.fsdecode(name))
    ]
    if not present:
        raise RuntimeError("the working tree contains no files to build")
    archive = subprocess.run(
        ["tar", "-cf", "-", "--null", "-T", "-"],
        cwd=repository_root,
        input=b"\0".join(present) + b"\0",
        check=False,
        capture_output=True,
    )
    if archive.returncode != 0:
        raise RuntimeError("failed to archive the working tree for the candidate build")
    extract = subprocess.run(
        ["tar", "-x", "-C", str(destination)],
        input=archive.stdout,
        check=False,
        capture_output=True,
    )
    if extract.returncode != 0:
        raise RuntimeError("failed to expand the working-tree candidate snapshot")


def _require_success(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"{action} failed: {detail}")


def _wheel_gate(
    repository_root: Path, wheel: Path | None = None, explicit_pyright: Path | None = None
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    if os.environ.get("PYTHONPATH"):
        raise RuntimeError("PYTHONPATH must be unset during installed-wheel verification")
    pyright = _resolve_pyright(repository_root, explicit_pyright)
    allowlist = _load_allowlist(
        repository_root / "scripts" / "type-completeness-allowlist.json",
        _pydantic_model_owners(repository_root),
    )
    report: dict[str, Any] = {
        "checkers": {
            "mypy": _checker_version([str(_resolve_mypy(repository_root))], repository_root),
            "pyright": _checker_version([str(pyright)], repository_root),
        },
        "python": {},
    }
    raw_reports: dict[str, Any] = {}
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ecn-sdk-types-") as temporary:
        outside = Path(temporary)
        if wheel is None:
            snapshot = outside / "snapshot"
            snapshot.mkdir()
            _export_worktree(repository_root, snapshot)
            wheelhouse = outside / "wheelhouse"
            build = _run(
                ["uv", "build", "--wheel", "--out-dir", str(wheelhouse)],
                cwd=snapshot,
            )
            _require_success(build, "wheel build")
            wheels = sorted(wheelhouse.glob("*.whl"))
            if len(wheels) != 1:
                raise RuntimeError(f"wheel build produced {len(wheels)} wheels")
            candidate_wheel = wheels[0]
            fixture_source = snapshot / "tests" / "typing"
        else:
            candidate_wheel = wheel.resolve()
            if not candidate_wheel.is_file():
                raise RuntimeError("candidate wheel does not exist")
            fixture_source = repository_root / "tests" / "typing"
        fixtures = outside / "typing"
        shutil.copytree(fixture_source, fixtures)

        for minor in _PYTHON_MINORS:
            venv = outside / f"venv-{minor}"
            _require_success(
                _run(["uv", "venv", "--python", minor, str(venv)], cwd=outside),
                f"Python {minor} venv creation",
            )
            python = venv / "bin" / "python"
            _require_success(
                _run(
                    ["uv", "pip", "install", "--python", str(python), str(candidate_wheel)],
                    cwd=outside,
                ),
                f"Python {minor} wheel installation",
            )
            probe_code = (
                "import os,pathlib,picogrid_ecn_client as p;"
                "assert not os.environ.get('PYTHONPATH');"
                "f=pathlib.Path(p.__file__).resolve();"
                "assert 'site-packages' in f.parts;"
                "assert (f.parent/'py.typed').is_file()"
            )
            probe_env = os.environ.copy()
            probe_env.pop("PYTHONPATH", None)
            _require_success(
                _run([str(python), "-c", probe_code], cwd=outside, env=probe_env),
                f"Python {minor} installed package probe",
            )
            active_env = probe_env.copy()
            active_env["VIRTUAL_ENV"] = str(venv)
            active_env["PATH"] = os.pathsep.join((str(venv / "bin"), active_env["PATH"]))
            verify = _run(
                [
                    str(pyright),
                    "--verifytypes",
                    "picogrid_ecn_client",
                    "--ignoreexternal",
                    "--outputjson",
                ],
                cwd=outside,
                env=active_env,
            )
            _, verify_payload = _parse_pyright(verify.stdout, fixtures)
            raw_reports[minor] = verify_payload
            completeness, completeness_failures = _completeness_result(verify_payload, allowlist)
            fixture_counts, fixture_failures = _run_fixture_battery(
                repository_root=repository_root,
                fixture_root=fixtures,
                cwd=outside,
                python_minor=minor,
                python_executable=python,
                source=False,
                pyright=pyright,
            )
            report["python"][minor] = {**completeness, "fixtures": fixture_counts}
            failures.extend(f"Python {minor}: {failure}" for failure in completeness_failures)
            failures.extend(f"Python {minor}: {failure}" for failure in fixture_failures)
    return report, failures, raw_reports


def _source_gate(
    repository_root: Path, explicit_pyright: Path | None = None
) -> tuple[dict[str, Any], list[str]]:
    pyright = _resolve_pyright(repository_root, explicit_pyright)
    fixture_root = repository_root / "tests" / "typing"
    counts, failures = _run_fixture_battery(
        repository_root=repository_root,
        fixture_root=fixture_root,
        cwd=repository_root,
        python_minor="3.11",
        python_executable=Path(sys.executable),
        source=True,
        pyright=pyright,
    )
    report = {
        "checkers": {
            "mypy": _checker_version([str(_resolve_mypy(repository_root))], repository_root),
            "pyright": _checker_version([str(pyright)], repository_root),
        },
        "source": counts,
    }
    return report, failures


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(normalize_report(report), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="store_true")
    parser.add_argument("--wheel", type=Path)
    parser.add_argument(
        "--report",
        nargs="?",
        const=Path("reports/generated/type-completeness.json"),
        type=Path,
    )
    parser.add_argument("--pyright-report", type=Path)
    parser.add_argument(
        "--pyright",
        type=Path,
        help="explicit pinned pyright executable; defaults to docs/node_modules/.bin then PATH",
    )
    arguments = parser.parse_args(argv)
    if arguments.source and (arguments.wheel is not None or arguments.pyright_report is not None):
        parser.error("--source cannot be combined with --wheel or --pyright-report")
    repository_root = Path(__file__).resolve().parents[1]
    raw_reports: dict[str, Any] | None = None
    try:
        if arguments.source:
            report, failures = _source_gate(repository_root, arguments.pyright)
        else:
            report, failures, raw_reports = _wheel_gate(
                repository_root, arguments.wheel, arguments.pyright
            )
    except (OSError, RuntimeError, KeyError, TypeError, ValueError) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True, separators=(",", ":")))
        return 1
    normalized = normalize_report({**report, "failures": sorted(failures), "ok": not failures})
    if arguments.report is not None:
        _write_report(arguments.report, normalized)
    if arguments.pyright_report is not None:
        assert raw_reports is not None
        _write_report(arguments.pyright_report, raw_reports)
    print(json.dumps(normalized, sort_keys=True, separators=(",", ":")))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
