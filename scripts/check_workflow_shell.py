# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Reject workflow `run:` blocks that the shell cannot parse.

A workflow file can be valid YAML while its shell body is not, and the failure
surfaces only when the step runs. For the production deploy that is the worst
place to find out: the step aborts after the Worker is already live, so the
deployment is published without the verification that follows it.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

# PyYAML is provisioned for this check alone, so it stays out of the project
# dependency set and its lock; mypy therefore has no stubs for it here.
import yaml  # type: ignore[import-untyped]

WORKFLOW_DIRECTORY = Path(__file__).resolve().parents[1] / ".github" / "workflows"
# Each shell parses its own dialect; validating `sh` with bash would accept
# bashisms that the step would then fail on.
PARSERS = {"bash": "bash", "sh": "sh"}
# Interpreters GitHub supports that are not shells. Naming them explicitly is
# what lets an unrecognized template fail closed instead of being skipped.
NON_SHELL = frozenset({"python", "python3", "pwsh", "powershell", "cmd", "node"})
# Without an explicit `shell:`, GitHub runs bash everywhere except Windows,
# where it runs pwsh. Windows runners are identified by their label, including
# self-hosted ones by convention, so anything else resolves to bash.
WINDOWS_RUNNER_PREFIX = "windows-"


class UnknownShellError(Exception):
    """A `shell:` value that is neither a known shell nor a known interpreter."""


# `env` options that take a separate operand: the word after them is the
# option's value, not the command. `-S`/`--split-string` is deliberately absent,
# because its operand is the command string itself.
ENV_OPERAND_OPTIONS = frozenset({"-u", "--unset", "-C", "--chdir"})


def _lex(value: str) -> list[str]:
    """Split a shell template the way a shell would, honouring quotes."""
    try:
        return shlex.split(value)
    except ValueError as error:
        raise UnknownShellError(value) from error


def _skip_env_options(words: list[str]) -> list[str]:
    """Drop `env`'s own options so the command it runs is left in front."""
    while words:
        word = words[0]
        if word in ENV_OPERAND_OPTIONS:
            # `env -u bash sh {0}` runs sh: `bash` here is the unset variable.
            words = words[2:]
            continue
        # `-S` splits its operand into arguments, so the command is inside it.
        if word in {"-S", "--split-string"} and len(words) > 1:
            return _lex(words[1]) + words[2:]
        if word.startswith("--split-string="):
            return _lex(word.split("=", 1)[1]) + words[1:]
        if word.startswith("-S") and len(word) > 2:
            return _lex(word[2:]) + words[1:]
        if word.startswith("-"):
            words = words[1:]
            continue
        break
    return words


def _parser_for(shell: str) -> str | None:
    """Map a `shell:` value to the interpreter that parses it, or None to skip.

    A custom template such as `bash --noprofile --norc -e -o pipefail {0}` is a
    valid way to run a step, so matching the whole value would let a step opt out
    of this gate while it still reported success. `env` wrappers and leading
    `VAR=value` assignments are unwrapped for the same reason. Anything left
    unrecognized raises rather than skipping: a gate that quietly covers less
    than it claims is worse than one that fails.
    """
    words = _lex(shell)
    while words:
        word = words[0]
        # `/usr/bin/env bash -e {0}` and `FOO=bar bash {0}` both wrap the shell.
        if "=" in word and not word.startswith("-"):
            words = words[1:]
            continue
        name = PurePosixPath(word).name
        if name == "env":
            words = _skip_env_options(words[1:])
            continue
        if name in PARSERS:
            return PARSERS[name]
        if name in NON_SHELL:
            return None
        raise UnknownShellError(shell)
    raise UnknownShellError(shell or "(empty)")


def _implicit_shell(runs_on: Any) -> str:
    """The shell GitHub uses when a step names none, from the runner label.

    Windows runs pwsh, which this check does not parse; everything else runs
    bash. A self-hosted Windows runner is only recognized if its label carries
    the conventional `windows-` prefix.
    """
    labels = [runs_on] if isinstance(runs_on, str) else runs_on if isinstance(runs_on, list) else []
    for label in labels:
        if isinstance(label, str) and label.startswith(WINDOWS_RUNNER_PREFIX):
            return "pwsh"
    return "bash"


def _default_shell(scope: dict[str, Any] | None) -> str | None:
    """Read `defaults.run.shell` from a workflow or job scope."""
    shell = (((scope or {}).get("defaults") or {}).get("run") or {}).get("shell")
    return shell if isinstance(shell, str) else None


def _run_blocks(document: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    """Yield (job, step label, parser, script) for every shell `run:` block."""
    blocks: list[tuple[str, str, str, str]] = []
    # GitHub resolves the shell as step, then job, then workflow, then the
    # platform default. Reading only the job scope would let a workflow-level
    # `sh` default be parsed as bash, which is the gap this check exists to close.
    workflow_shell = _default_shell(document)
    for job, spec in (document.get("jobs") or {}).items():
        job_shell = _default_shell(spec)
        for index, step in enumerate(spec.get("steps") or []):
            script = step.get("run")
            if not script:
                continue
            label = step.get("name") or f"step {index}"
            selected = (
                step.get("shell")
                or job_shell
                or workflow_shell
                or _implicit_shell(spec.get("runs-on"))
            )
            try:
                parser = _parser_for(selected)
            except UnknownShellError:
                # Reported as a failure, not skipped: an unrecognized template
                # is how a run block would otherwise leave this gate's coverage.
                blocks.append((job, label, "", script))
                continue
            # A `shell:` naming python or pwsh is not ours to parse.
            if parser is None:
                continue

            blocks.append((job, label, parser, script))
    return blocks


def main() -> int:
    failures: list[str] = []
    checked = 0

    # A workflow this check silently skips is a workflow it does not protect.
    workflows = sorted(
        path for pattern in ("*.yml", "*.yaml") for path in WORKFLOW_DIRECTORY.glob(pattern)
    )
    for workflow in workflows:
        document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            continue
        for job, label, parser, script in _run_blocks(document):
            checked += 1
            if not parser:
                failures.append(
                    f"{workflow.name}: {job}: {label}\n"
                    "unrecognized shell; this run block cannot be validated"
                )
                continue
            with tempfile.NamedTemporaryFile("w", suffix=".sh") as handle:
                handle.write(script)
                handle.flush()
                result = subprocess.run(
                    [parser, "-n", handle.name],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            if result.returncode != 0:
                detail = result.stderr.strip().replace(handle.name, "<run block>")
                failures.append(f"{workflow.name}: {job}: {label}\n{detail}")

    for failure in failures:
        print(failure, file=sys.stderr)
    if failures:
        print(f"{len(failures)} workflow run block(s) failed validation", file=sys.stderr)
        return 1

    print(f"Workflow shell check passed: {checked} run blocks parsed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
