# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).parents[2]
CHECKER = REPOSITORY / "scripts" / "check_dco.py"
ALICE = ("Alice Example", "alice@example.invalid")
BOB = ("Bob Contributor", "bob@example.invalid")
MAINTAINER = ("Release Maintainer", "maintainer@example.invalid")
BOT = ("Dependency Bot[bot]", "bot@example.invalid")


def _git(
    repository: Path,
    *arguments: str,
    identity: tuple[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if arguments[0] in {"merge", "commit-tree"} and identity is None:
        raise AssertionError(f"{arguments[0]} fixture must supply an explicit identity")
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        }
    )
    if identity is not None:
        name, email = identity
        environment.update(
            {
                "GIT_AUTHOR_NAME": name,
                "GIT_AUTHOR_EMAIL": email,
                "GIT_AUTHOR_DATE": "2024-01-01T00:00:00+00:00",
                "GIT_COMMITTER_NAME": name,
                "GIT_COMMITTER_EMAIL": email,
                "GIT_COMMITTER_DATE": "2024-01-01T00:00:00+00:00",
            }
        )
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )


def _new_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "commit.gpgsign", "false")
    _git(repository, "config", "core.hooksPath", os.devnull)
    return repository


def _commit_staged(
    repository: Path,
    subject: str,
    *,
    identity: tuple[str, str] = ALICE,
    signoff: str | None = None,
    raw_trailer: str | None = None,
) -> str:
    message = subject
    if signoff is not None:
        message += f"\n\nSigned-off-by: {signoff}"
    if raw_trailer is not None:
        message += f"\n\n{raw_trailer}"
    _git(repository, "commit", "-q", "-m", message, identity=identity)
    return _git(repository, "rev-parse", "HEAD").stdout.strip()


def _commit(
    repository: Path,
    filename: str,
    *,
    identity: tuple[str, str] = ALICE,
    signoff: str | None = None,
    raw_trailer: str | None = None,
) -> str:
    (repository / filename).write_text(f"content for {filename}\n", encoding="utf-8")
    _git(repository, "add", filename)
    return _commit_staged(
        repository,
        f"Add {filename}",
        identity=identity,
        signoff=signoff,
        raw_trailer=raw_trailer,
    )


def _signed_commit(
    repository: Path,
    filename: str,
    identity: tuple[str, str] = ALICE,
) -> str:
    name, email = identity
    return _commit(repository, filename, identity=identity, signoff=f"{name} <{email}>")


def _run_checker(
    repository: Path,
    base: str,
    head: str,
    *,
    exclude_head: str | None = None,
) -> subprocess.CompletedProcess[str]:
    assert CHECKER.is_file(), "scripts.check_dco CLI is not implemented"
    command = [
        sys.executable,
        "-m",
        "scripts.check_dco",
        "--base",
        base,
        "--head",
        head,
    ]
    if exclude_head is not None:
        command.extend(["--exclude-head", exclude_head])
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPOSITORY)
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["LC_ALL"] = "C"
    return subprocess.run(
        command,
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _diagnostic(result: subprocess.CompletedProcess[str]) -> str:
    return f"{result.stdout}\n{result.stderr}".lower()


def test_accepts_signed_single_commit(tmp_path: Path) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    head = _signed_commit(repository, "change.txt")

    result = _run_checker(repository, base, head)

    assert result.returncode == 0, _diagnostic(result)


def test_unsigned_commit_reports_actionable_failure(tmp_path: Path) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    head = _commit(repository, "unsigned.txt", identity=BOB)

    result = _run_checker(repository, base, head)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert head[:12] in diagnostic
    assert BOB[1] in diagnostic
    assert "signed-off-by" in diagnostic


def test_rejects_earlier_unsigned_commit_in_multi_commit_range(tmp_path: Path) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    unsigned = _commit(repository, "unsigned.txt", identity=BOB)
    head = _signed_commit(repository, "later-signed.txt", identity=MAINTAINER)

    result = _run_checker(repository, base, head)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert unsigned[:12] in diagnostic


def test_rejects_malformed_signed_off_by_trailer(tmp_path: Path) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    head = _commit(
        repository,
        "malformed.txt",
        identity=BOB,
        raw_trailer="Signed-off-by Bob Contributor <bob@example.invalid>",
    )

    result = _run_checker(repository, base, head)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert head[:12] in diagnostic
    assert "signed-off-by" in diagnostic


def test_rejects_malformed_signoff_like_line_alongside_valid_trailer(
    tmp_path: Path,
) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    head = _commit(
        repository,
        "malformed-alongside-valid.txt",
        identity=BOB,
        raw_trailer=(
            "Signed-off-by Bob Contributor <bob@example.invalid>\n"
            f"Signed-off-by: {BOB[0]} <{BOB[1]}>"
        ),
    )

    result = _run_checker(repository, base, head)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert head[:12] in diagnostic
    assert "malformed signed-off-by" in diagnostic


@pytest.mark.parametrize(
    ("leading_indent", "label"),
    [
        ("\N{NO-BREAK SPACE}", "nbsp"),
        ("\N{NO-BREAK SPACE}" * 2, "double-nbsp"),
        ("\N{EM SPACE}", "em-space"),
        ("\N{EM SPACE}" * 2, "double-em-space"),
        ("\N{THIN SPACE}", "thin-space"),
    ],
)
def test_rejects_non_ascii_indented_signoff_like_line_alongside_valid_trailer(
    tmp_path: Path,
    leading_indent: str,
    label: str,
) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    malformed_line = f"{leading_indent}Signed-off-by: {BOB[0]} <{BOB[1]}>"
    head = _commit(
        repository,
        f"{label}-indented-signoff.txt",
        identity=BOB,
        raw_trailer=(f"{malformed_line}\nSigned-off-by: {BOB[0]} <{BOB[1]}>"),
    )

    result = _run_checker(repository, base, head)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert head[:12] in diagnostic
    assert "malformed signed-off-by" in diagnostic
    assert (
        f"malformed Signed-off-by line {malformed_line!r}; expected Name <email>" in result.stderr
    )


def test_rejects_valid_shaped_signoff_in_body_alongside_final_trailer(
    tmp_path: Path,
) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    head = _commit(
        repository,
        "body-signoff-alongside-valid.txt",
        identity=BOB,
        raw_trailer=(
            f"Signed-off-by: {BOB[0]} <{BOB[1]}>\n\n"
            "Additional commit message body.\n\n"
            f"Signed-off-by: {BOB[0]} <{BOB[1]}>"
        ),
    )

    result = _run_checker(repository, base, head)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert head[:12] in diagnostic
    assert "exactly one signed-off-by" in diagnostic


def test_invalid_utf8_commit_metadata_fails_closed(tmp_path: Path) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    (repository / "invalid-utf8.txt").write_text("invalid metadata\n", encoding="utf-8")
    _git(repository, "add", "invalid-utf8.txt")
    tree = _git(repository, "write-tree").stdout.strip()
    identity = f"{BOB[0]} <{BOB[1]}> 1704067200 +0000"
    raw_commit = (
        f"tree {tree}\nparent {base}\nauthor {identity}\ncommitter {identity}\n\n"
    ).encode("ascii") + (
        b"Invalid UTF-8 \xff metadata\n\nSigned-off-by: Bob Contributor <bob@example.invalid>\n"
    )
    created = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
        cwd=repository,
        input=raw_commit,
        check=True,
        capture_output=True,
    )
    head = created.stdout.decode("ascii").strip()

    result = _run_checker(repository, base, head)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert head[:12] in diagnostic
    assert "utf-8" in diagnostic


def test_rejects_signoff_matching_neither_author_nor_committer(tmp_path: Path) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    head = _commit(
        repository,
        "mismatched.txt",
        identity=BOB,
        signoff="Unrelated Person <unrelated@example.invalid>",
    )

    result = _run_checker(repository, base, head)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert head[:12] in diagnostic
    assert BOB[1] in diagnostic
    assert "unrelated@example.invalid" in diagnostic


@pytest.mark.parametrize(
    ("non_ascii_whitespace", "case_name"),
    [("\N{NO-BREAK SPACE}", "nbsp"), ("\N{EM SPACE}", "em-space")],
)
def test_rejects_non_ascii_whitespace_identity_conflation(
    tmp_path: Path,
    non_ascii_whitespace: str,
    case_name: str,
) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    author = (f"Bob{non_ascii_whitespace}Contributor", BOB[1])
    head = _commit(
        repository,
        f"{case_name}.txt",
        identity=author,
        signoff=f"{BOB[0]} <{BOB[1]}>",
    )

    result = _run_checker(repository, base, head)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert head[:12] in diagnostic
    assert "matches neither" in diagnostic


@pytest.mark.parametrize(
    ("non_ascii_whitespace", "case_name"),
    [("\N{NO-BREAK SPACE}", "nbsp"), ("\N{EM SPACE}", "em-space")],
)
def test_rejects_non_ascii_whitespace_before_email_delimiter(
    tmp_path: Path,
    non_ascii_whitespace: str,
    case_name: str,
) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    head = _commit(
        repository,
        f"{case_name}-delimiter.txt",
        identity=BOB,
        signoff=f"{BOB[0]}{non_ascii_whitespace}<{BOB[1]}>",
    )

    result = _run_checker(repository, base, head)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert head[:12] in diagnostic
    assert "matches neither" in diagnostic


@pytest.mark.parametrize(
    ("non_ascii_whitespace", "case_name"),
    [("\N{NO-BREAK SPACE}", "nbsp"), ("\N{EM SPACE}", "em-space")],
)
def test_rejects_non_ascii_whitespace_around_complete_trailer_identity(
    tmp_path: Path,
    non_ascii_whitespace: str,
    case_name: str,
) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    head = _commit(
        repository,
        f"{case_name}-identity-boundary.txt",
        identity=BOB,
        raw_trailer=(
            f"Signed-off-by: {non_ascii_whitespace}{BOB[0]} <{BOB[1]}>{non_ascii_whitespace}"
        ),
    )

    result = _run_checker(repository, base, head)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert head[:12] in diagnostic
    assert "malformed signed-off-by" in diagnostic


def test_rejects_multi_character_casefold_identity_match(tmp_path: Path) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    identity = ("Straße Contributor", "strasse@example.invalid")
    head = _commit(
        repository,
        "casefold.txt",
        identity=identity,
        signoff="STRASSE CONTRIBUTOR <strasse@example.invalid>",
    )

    result = _run_checker(repository, base, head)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert head[:12] in diagnostic
    assert "matches neither" in diagnostic


def test_failure_directs_contributors_to_amend_or_rebase_procedure(
    tmp_path: Path,
) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    head = _commit(repository, "unsigned.txt", identity=BOB)

    result = _run_checker(repository, base, head)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert "contributing.md" in diagnostic
    assert "amend/rebase" in diagnostic
    assert "git commit --signoff" not in diagnostic


def test_rejects_compatibility_width_identity_matching_ascii_author(
    tmp_path: Path,
) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")

    def compatibility_width(value: str) -> str:
        return "".join(
            chr(ord(character) + 0xFEE0)
            if character.isascii() and character.isalnum()
            else character
            for character in value
        )

    head = _commit(
        repository,
        "compatibility-width.txt",
        identity=BOB,
        signoff=(f"{compatibility_width(BOB[0])} <{compatibility_width(BOB[1])}>"),
    )

    result = _run_checker(repository, base, head)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert head[:12] in diagnostic
    assert "matches neither" in diagnostic


def test_rejects_duplicate_identical_signed_off_by_trailers(tmp_path: Path) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    trailer = f"Signed-off-by: {BOB[0]} <{BOB[1]}>"
    head = _commit(
        repository,
        "duplicate.txt",
        identity=BOB,
        raw_trailer=f"{trailer}\n{trailer}",
    )

    result = _run_checker(repository, base, head)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert head[:12] in diagnostic
    assert "signed-off-by" in diagnostic


def test_rejects_multiple_signed_off_by_identities(tmp_path: Path) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    head = _commit(
        repository,
        "conflicting.txt",
        identity=BOB,
        raw_trailer=(
            f"Signed-off-by: {BOB[0]} <{BOB[1]}>\n"
            "Signed-off-by: Unrelated Person <unrelated@example.invalid>"
        ),
    )

    result = _run_checker(repository, base, head)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert head[:12] in diagnostic
    assert BOB[1] in diagnostic
    assert "unrelated@example.invalid" in diagnostic


def test_rejects_bot_author_even_with_matching_signoff(tmp_path: Path) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    (repository / "bot-author.txt").write_text("bot authored\n", encoding="utf-8")
    _git(repository, "add", "bot-author.txt")
    _git(
        repository,
        "commit",
        "-q",
        f"--author={BOT[0]} <{BOT[1]}>",
        "-m",
        f"Bot-authored change\n\nSigned-off-by: {BOT[0]} <{BOT[1]}>",
        identity=ALICE,
    )
    head_sha = _git(repository, "rev-parse", "HEAD").stdout.strip()

    result = _run_checker(repository, base, head_sha)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert "bot author" in diagnostic


def test_rejects_bot_committer_even_with_matching_signoff(tmp_path: Path) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    (repository / "bot-committer.txt").write_text("bot committed\n", encoding="utf-8")
    _git(repository, "add", "bot-committer.txt")
    _git(
        repository,
        "commit",
        "-q",
        f"--author={ALICE[0]} <{ALICE[1]}>",
        "-m",
        f"Bot-committed change\n\nSigned-off-by: {BOT[0]} <{BOT[1]}>",
        identity=BOT,
    )
    head_sha = _git(repository, "rev-parse", "HEAD").stdout.strip()

    result = _run_checker(repository, base, head_sha)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert "bot committer" in diagnostic


def test_accepts_signed_contributor_created_merge_commit(tmp_path: Path) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt", identity=MAINTAINER)
    _git(repository, "checkout", "-q", "-b", "contributor-change")
    _signed_commit(repository, "contributor.txt", identity=BOB)
    _git(repository, "checkout", "-q", "main")
    _signed_commit(repository, "maintainer.txt", identity=MAINTAINER)
    _git(
        repository,
        "merge",
        "-q",
        "--no-ff",
        "--no-commit",
        "contributor-change",
        identity=BOB,
    )
    head = _commit_staged(
        repository,
        "Merge contributor change",
        identity=BOB,
        signoff=f"{BOB[0]} <{BOB[1]}>",
    )

    result = _run_checker(repository, base, head)

    assert result.returncode == 0, _diagnostic(result)


@pytest.mark.parametrize("range_kind", ["empty", "reversed"])
def test_rejects_empty_or_reversed_range(tmp_path: Path, range_kind: str) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    head = _signed_commit(repository, "head.txt")
    if range_kind == "empty":
        base = head
    else:
        base, head = head, base

    result = _run_checker(repository, base, head)

    assert result.returncode != 0


def test_accepts_diverged_base_and_ignores_upstream_only_commits(
    tmp_path: Path,
) -> None:
    repository = _new_repository(tmp_path)
    common = _signed_commit(repository, "common.txt", identity=MAINTAINER)
    _git(repository, "checkout", "-q", "-b", "proposed-change")
    head = _signed_commit(repository, "proposed.txt", identity=BOB)
    _git(repository, "checkout", "-q", "main")
    base = _commit(repository, "upstream-unsigned.txt", identity=MAINTAINER)
    assert _git(repository, "merge-base", base, head).stdout.strip() == common

    result = _run_checker(repository, base, head)

    assert result.returncode == 0, _diagnostic(result)


def test_ignores_commits_reachable_from_base_with_multiple_merge_bases(
    tmp_path: Path,
) -> None:
    repository = _new_repository(tmp_path)
    root = _signed_commit(repository, "root.txt", identity=MAINTAINER)
    first_tree = _git(repository, "show", "-s", "--format=%T", root).stdout.strip()
    first_merge_base = _git(
        repository,
        "commit-tree",
        first_tree,
        "-p",
        root,
        "-m",
        "Unsigned first merge base",
        identity=BOB,
    ).stdout.strip()
    second_merge_base = _git(
        repository,
        "commit-tree",
        first_tree,
        "-p",
        root,
        "-m",
        "Unsigned second merge base",
        identity=BOB,
    ).stdout.strip()
    signoff = f"Signed-off-by: {MAINTAINER[0]} <{MAINTAINER[1]}>"
    base = _git(
        repository,
        "commit-tree",
        first_tree,
        "-p",
        first_merge_base,
        "-p",
        second_merge_base,
        "-m",
        f"Signed base merge\n\n{signoff}",
        identity=MAINTAINER,
    ).stdout.strip()
    head = _git(
        repository,
        "commit-tree",
        first_tree,
        "-p",
        second_merge_base,
        "-p",
        first_merge_base,
        "-m",
        f"Signed head merge\n\n{signoff}",
        identity=MAINTAINER,
    ).stdout.strip()
    assert set(_git(repository, "merge-base", "--all", base, head).stdout.split()) == {
        first_merge_base,
        second_merge_base,
    }

    result = _run_checker(repository, base, head)

    assert result.returncode == 0, _diagnostic(result)


def test_rejects_unreachable_range(tmp_path: Path) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    _git(repository, "checkout", "-q", "--orphan", "disconnected")
    _git(repository, "rm", "-q", "-rf", ".")
    head = _signed_commit(repository, "disconnected.txt", identity=BOB)

    result = _run_checker(repository, base, head)

    assert result.returncode != 0


def test_allows_excluding_exact_unsigned_synthetic_merge_head(tmp_path: Path) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt", identity=MAINTAINER)
    _git(repository, "checkout", "-q", "-b", "proposed-change")
    _signed_commit(repository, "proposed.txt", identity=BOB)
    _git(repository, "checkout", "-q", "main")
    _git(
        repository,
        "merge",
        "-q",
        "--no-ff",
        "--no-commit",
        "proposed-change",
        identity=MAINTAINER,
    )
    head = _commit_staged(
        repository,
        "Synthetic merge for proposed change",
        identity=MAINTAINER,
    )
    parents = _git(repository, "show", "-s", "--format=%P", head).stdout.split()
    assert len(parents) == 2

    result = _run_checker(repository, base, head, exclude_head=head)

    assert result.returncode == 0, _diagnostic(result)


def test_rejects_exclusion_that_leaves_no_commits_to_check(tmp_path: Path) -> None:
    repository = _new_repository(tmp_path)
    ancestor = _signed_commit(repository, "ancestor.txt", identity=MAINTAINER)
    base = _signed_commit(repository, "base.txt", identity=MAINTAINER)
    tree = _git(repository, "show", "-s", "--format=%T", base).stdout.strip()
    head = _git(
        repository,
        "commit-tree",
        tree,
        "-p",
        base,
        "-p",
        ancestor,
        "-m",
        "Synthetic merge with no introduced commits",
        identity=MAINTAINER,
    ).stdout.strip()

    result = _run_checker(repository, base, head, exclude_head=head)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert "no commits" in diagnostic


def test_rejects_exclusion_of_unsigned_non_merge_head(tmp_path: Path) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    head = _commit(repository, "unsigned-head.txt", identity=BOB)

    result = _run_checker(repository, base, head, exclude_head=head)

    assert result.returncode != 0


def test_rejects_exclusion_that_is_not_head(tmp_path: Path) -> None:
    repository = _new_repository(tmp_path)
    base = _signed_commit(repository, "base.txt")
    earlier = _signed_commit(repository, "earlier.txt", identity=BOB)
    head = _signed_commit(repository, "head.txt", identity=MAINTAINER)

    result = _run_checker(repository, base, head, exclude_head=earlier)

    diagnostic = _diagnostic(result)
    assert result.returncode != 0
    assert "exclude" in diagnostic
    assert "head" in diagnostic
