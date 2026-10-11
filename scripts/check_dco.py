# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Check every commit in a Git range for a valid DCO sign-off."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn


class DCOCheckError(RuntimeError):
    """Raised when the requested history cannot be checked safely."""


@dataclass(frozen=True, slots=True)
class Commit:
    """Identity and message fields used by the DCO check."""

    sha: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    message: str


_IDENTITY = re.compile(
    r"^[ \t\n\r\f\v]*(?P<name>[^<>]+?)[ \t\n\r\f\v]*"
    r"<(?P<email>[^\s<>@]+@[^\s<>@]+)>[ \t\n\r\f\v]*$"
)
_ASCII_WHITESPACE = re.compile(r"[ \t\n\r\f\v]+")
_ASCII_TRIM = " \t\n\r\f\v"


def _git(
    repository: Path,
    *arguments: str,
    input_text: str | None = None,
    accepted_returncodes: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[str]:
    """Run Git and fail closed with a useful diagnostic."""
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
        )
    except UnicodeDecodeError as error:
        command = " ".join(arguments)
        raise DCOCheckError(
            f"Git output is not valid UTF-8 for `git {command}` "
            f"(invalid byte at offset {error.start})"
        ) from error
    except OSError as error:
        raise DCOCheckError(f"could not run Git: {error}") from error

    if result.returncode not in accepted_returncodes:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        command = " ".join(arguments)
        raise DCOCheckError(
            f"Git command failed (exit {result.returncode}): git {command}\n{detail}"
        )
    return result


def _resolve_commit(repository: Path, revision: str, label: str) -> str:
    if not revision.strip():
        raise DCOCheckError(f"{label} revision is empty")
    result = _git(
        repository,
        "rev-parse",
        "--verify",
        "--quiet",
        "--end-of-options",
        f"{revision}^{{commit}}",
        accepted_returncodes=frozenset({0, 1}),
    )
    sha = result.stdout.strip()
    if result.returncode != 0 or not sha:
        raise DCOCheckError(f"{label} revision is not an available commit: {revision}")
    return sha


def _validate_range(repository: Path, base: str, head: str) -> list[str]:
    if base == head:
        raise DCOCheckError("DCO range is empty: base and head resolve to the same commit")

    merge_bases = _git(
        repository,
        "merge-base",
        "--all",
        base,
        head,
        accepted_returncodes=frozenset({0, 1}),
    )
    common_ancestors = merge_bases.stdout.split()
    effective_base = common_ancestors[0] if common_ancestors else ""
    if merge_bases.returncode != 0 or not effective_base:
        raise DCOCheckError(
            f"invalid DCO range: base and head have no common ancestor ({base[:12]}..{head[:12]})"
        )

    result = _git(
        repository,
        "rev-list",
        "--reverse",
        "--topo-order",
        head,
        "--not",
        base,
    )
    commits = result.stdout.split()
    if not commits:
        raise DCOCheckError(
            "DCO range contains no commits after its effective merge base "
            f"({effective_base[:12]}..{head[:12]})"
        )
    return commits


def _validate_exclusion(
    repository: Path,
    exclude_revision: str,
    head: str,
) -> str:
    excluded = _resolve_commit(repository, exclude_revision, "exclude-head")
    if excluded != head:
        raise DCOCheckError(
            f"exclude-head must resolve to the exact head commit ({excluded[:12]} != {head[:12]})"
        )

    result = _git(repository, "rev-list", "--parents", "-n", "1", head)
    fields = result.stdout.split()
    if len(fields) < 3:
        raise DCOCheckError(
            "exclude-head is permitted only when the exact head is a merge commit "
            f"({head[:12]} has fewer than two parents)"
        )
    return excluded


def _read_commit(repository: Path, sha: str) -> Commit:
    # Read the raw object first: porcelain may replace malformed metadata while
    # rendering it, which would make an undecodable commit appear valid.
    try:
        _git(repository, "cat-file", "--batch", input_text=f"{sha}\n")
    except DCOCheckError as error:
        raise DCOCheckError(f"could not read commit metadata for {sha[:12]}: {error}") from error
    result = _git(
        repository,
        "show",
        "--no-patch",
        "--format=%an%x00%ae%x00%cn%x00%ce%x00%B",
        sha,
        "--",
    )
    fields = result.stdout.split("\0", 4)
    if len(fields) != 5:
        raise DCOCheckError(f"could not parse commit metadata for {sha[:12]}")
    return Commit(sha, *fields)


def _trailers(repository: Path, message: str) -> list[str]:
    result = _git(repository, "interpret-trailers", "--parse", input_text=message)
    values: list[str] = []
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip(_ASCII_TRIM).casefold() == "signed-off-by":
            values.append(value.strip(_ASCII_TRIM))
    return values


def _raw_signoff_lines(message: str) -> list[str]:
    signoff_lines: list[str] = []
    for line in message.splitlines():
        stripped = line.strip(_ASCII_TRIM)
        candidate = stripped.lstrip()
        if candidate.casefold().startswith("signed-off-by"):
            signoff_lines.append(stripped)
    return signoff_lines


def _malformed_signoff_line(message: str) -> str | None:
    """Return the first raw sign-off-like line that is not a valid trailer line."""
    for stripped in _raw_signoff_lines(message):
        key, separator, value = stripped.partition(":")
        if (
            not separator
            or key.strip(_ASCII_TRIM).casefold() != "signed-off-by"
            or _identity(value.strip(_ASCII_TRIM)) is None
        ):
            return stripped
    return None


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    return _ASCII_WHITESPACE.sub(" ", normalized).strip(" \t\n\r\f\v").lower()


def _identity(value: str) -> tuple[str, str] | None:
    match = _IDENTITY.fullmatch(value)
    if match is None:
        return None
    return (_normalize(match.group("name")), _normalize(match.group("email")))


def _is_bot_identity(name: str, email: str) -> bool:
    """Return whether a Git identity uses GitHub's bot identity convention."""
    email_local_part = _normalize(email).partition("@")[0]
    return _normalize(name).endswith("[bot]") or email_local_part.endswith("[bot]")


def _commit_failure(repository: Path, commit: Commit) -> str | None:
    prefix = f"commit {commit.sha[:12]} by {commit.author_name} <{commit.author_email}>"
    if _is_bot_identity(commit.author_name, commit.author_email):
        return f"{prefix}: bot author identities cannot certify the DCO"
    if _is_bot_identity(commit.committer_name, commit.committer_email):
        return f"{prefix}: bot committer identities cannot certify the DCO"
    malformed = _malformed_signoff_line(commit.message)
    if malformed is not None:
        return f"{prefix}: malformed Signed-off-by line {malformed!r}; expected Name <email>"
    raw_signoffs = _raw_signoff_lines(commit.message)
    signoffs = _trailers(repository, commit.message)
    if not signoffs:
        return f"{prefix}: missing a Signed-off-by trailer"
    if len(signoffs) != 1 or len(raw_signoffs) != 1:
        rendered = "; ".join(signoffs)
        return (
            f"{prefix}: expected exactly one Signed-off-by trailer and raw line, found "
            f"{len(signoffs)} trailer(s) and {len(raw_signoffs)} raw line(s)"
            f"{f' ({rendered})' if rendered else ''}"
        )

    signoff = _identity(signoffs[0])
    if signoff is None:
        return f"{prefix}: malformed Signed-off-by identity {signoffs[0]!r}; expected Name <email>"

    permitted = {
        (_normalize(commit.author_name), _normalize(commit.author_email)),
        (_normalize(commit.committer_name), _normalize(commit.committer_email)),
    }
    if signoff not in permitted:
        expected = " or ".join(
            (
                f"{commit.author_name} <{commit.author_email}>",
                f"{commit.committer_name} <{commit.committer_email}>",
            )
        )
        return (
            f"{prefix}: Signed-off-by {signoffs[0]!r} matches neither the "
            f"author nor committer; expected {expected}"
        )
    return None


def check_dco(
    repository: Path,
    base_revision: str,
    head_revision: str,
    exclude_head_revision: str | None = None,
) -> int:
    """Check the selected commit range and return the number of checked commits."""
    base = _resolve_commit(repository, base_revision, "base")
    head = _resolve_commit(repository, head_revision, "head")
    commits = _validate_range(repository, base, head)

    excluded: str | None = None
    if exclude_head_revision is not None:
        excluded = _validate_exclusion(repository, exclude_head_revision, head)

    failures: list[str] = []
    checked = 0
    for sha in commits:
        if sha == excluded:
            continue
        checked += 1
        failure = _commit_failure(repository, _read_commit(repository, sha))
        if failure is not None:
            failures.append(failure)

    if checked == 0:
        raise DCOCheckError("DCO range contains no commits to check after applying exclude-head")

    if failures:
        joined = "\n".join(f"- {failure}" for failure in failures)
        raise DCOCheckError(
            "DCO sign-off check failed:\n"
            f"{joined}\n"
            "Follow CONTRIBUTING.md's amend/rebase procedure, then update the branch."
        )
    return checked


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check every commit in base..head for a matching DCO sign-off."
    )
    parser.add_argument("--base", required=True, help="ancestor commit for the range")
    parser.add_argument("--head", required=True, help="inclusive range head")
    parser.add_argument(
        "--exclude-head",
        help="exclude head only when it is the exact synthetic merge commit",
    )
    return parser


def _fail(message: str) -> NoReturn:
    print(f"DCO check failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    arguments = _parser().parse_args()
    try:
        checked = check_dco(
            Path.cwd(),
            arguments.base,
            arguments.head,
            arguments.exclude_head,
        )
    except DCOCheckError as error:
        _fail(str(error))
    print(f"DCO check passed: {checked} commit(s) have a valid Signed-off-by trailer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
