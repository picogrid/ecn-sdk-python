#!/usr/bin/env python3
# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Validate the repository's deterministic MPL-2.0 license policy."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import cast

REPOSITORY = Path(__file__).resolve().parents[1]
POLICY_PATH = REPOSITORY / "scripts" / "license-policy.json"
LICENSE_EXPRESSION = "MPL-2.0"
SPDX_MARKER = "SPDX-License-" + "Identifier:"
SPDX_COPYRIGHT_MARKER = "SPDX-File" + "CopyrightText:"
COPYRIGHT_MARKER = "Copy" + "right"
# A copyright notice, not merely the word. The non-SPDX forms require a legal
# indicator — `(c)`, `(C)`, `©`, or a four-digit year — so ordinary prose keeps out
# of the provenance registry: "copyright is granted under section 2.1" was already
# excluded by case, but a heading such as "Copyright Policy" is not, and accepting
# any capital letter as the indicator reported it as a third-party notice.
# The pattern is matched against a line whose comment and markup wrappers have been
# removed by `_strip_wrappers`, so every caller agrees on what a wrapper is; it stays
# anchored to the start of that normalized content because an unanchored search also
# matches source that merely discusses a notice, including this module and its tests.
# The REUSE form is recognized so an explicitly identified upstream holder cannot
# pass through a category rule unregistered.
COPYRIGHT_NOTICE_RE = re.compile(
    rf"^(?:{re.escape(SPDX_COPYRIGHT_MARKER)}\s*(?:\([cC]\)|\u00a9)?\s*(?:\d{{4}}|[A-Z])"
    rf"|\b(?:{re.escape(COPYRIGHT_MARKER)}|{re.escape(COPYRIGHT_MARKER.upper())})\b"
    r"\s*(?:(?:\([cC]\)|\u00a9)\s*(?:\d{4}|[A-Z])|(?=\d{4}))"
    r"|\u00a9\s*(?:\d{4}|[A-Z]))"
)
POLICY_KEYS = frozenset(
    {
        "category_rules",
        "exception_categories",
        "exceptions",
        "license_expression",
        "license_text_sha256",
        "notice_copyright",
        "notice_required_extensions",
        "notice_required_paths",
        "third_party_notices",
        "third_party_scan_exclusions",
    }
)


class LicensePolicyError(RuntimeError):
    """Raised when the committed license policy cannot be interpreted safely."""


@dataclass(frozen=True, order=True)
class Finding:
    """One deterministic, file-level license policy failure."""

    path: str
    check: str
    message: str


@dataclass(frozen=True)
class PolicyReport:
    """Normalized result of evaluating the license policy against a tracked tree."""

    files_scanned: int
    inline_notices: int
    exceptions: int
    category_rule_files: int
    findings: tuple[Finding, ...]

    def as_json(self) -> dict[str, object]:
        """Return a stable machine-readable representation of this report."""

        return {
            "counts": {
                "category_rule_files": self.category_rule_files,
                "exceptions": self.exceptions,
                "files_scanned": self.files_scanned,
                "inline_notices": self.inline_notices,
            },
            "findings": [asdict(finding) for finding in self.findings],
            "ok": not self.findings,
        }


@dataclass(frozen=True)
class CategoryRule:
    """A documented coverage rule for formats that do not carry inline notices."""

    name: str
    extensions: tuple[str, ...]
    paths: tuple[str, ...]
    reason: str

    def matches(self, path: str) -> bool:
        """Return whether this rule accounts for the repository-relative path.

        A ``paths`` entry matches the identical path, or every path beneath it when
        the entry names a directory by ending with ``/``. Bare prefix matching is
        deliberately not supported: it would let ``Makefile.py`` inherit the
        ``Makefile`` exemption and ship without a notice.
        """

        if PurePosixPath(path).suffix in self.extensions:
            return True
        return any(
            path == entry or (entry.endswith("/") and path.startswith(entry))
            for entry in self.paths
        )


@dataclass(frozen=True)
class ExceptionEntry:
    """A documented exact-path exception to an inline or category notice."""

    path: str
    category: str
    reason: str


@dataclass(frozen=True)
class ThirdPartyNotice:
    """A digest-pinned registration of third-party notice content."""

    path: str
    license: str
    origin: str
    sha256: str


@dataclass(frozen=True)
class ScanExclusion:
    """An exact-path exclusion from notice discovery with a recorded reason."""

    path: str
    reason: str


@dataclass(frozen=True)
class LicensePolicy:
    """Validated in-memory form of ``scripts/license-policy.json``."""

    license_expression: str
    license_text_sha256: str
    notice_copyright: str
    notice_required_extensions: tuple[str, ...]
    notice_required_paths: tuple[str, ...]
    exception_categories: frozenset[str]
    exceptions: tuple[ExceptionEntry, ...]
    category_rules: tuple[CategoryRule, ...]
    third_party_notices: tuple[ThirdPartyNotice, ...]
    third_party_scan_exclusions: tuple[ScanExclusion, ...]


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise LicensePolicyError(f"{label} must be a JSON object with string keys")
    return cast(dict[str, object], value)


def _exact_keys(mapping: Mapping[str, object], expected: set[str], label: str) -> None:
    actual = set(mapping)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise LicensePolicyError(
            f"{label} keys changed (missing={missing or 'none'}; unexpected={unexpected or 'none'})"
        )


def _string(mapping: Mapping[str, object], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LicensePolicyError(f"{label}.{key} must be a non-empty string")
    return value


def _string_list(mapping: Mapping[str, object], key: str, label: str) -> tuple[str, ...]:
    value = mapping.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and bool(item.strip()) for item in value
    ):
        raise LicensePolicyError(f"{label}.{key} must be a list of non-empty strings")
    entries = cast(list[str], value)
    if entries != sorted(set(entries)):
        raise LicensePolicyError(f"{label}.{key} must be sorted and contain no duplicates")
    return tuple(entries)


def _object_list(mapping: Mapping[str, object], key: str, label: str) -> list[dict[str, object]]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise LicensePolicyError(f"{label}.{key} must be a JSON array")
    return [_mapping(item, f"{label}.{key}[{index}]") for index, item in enumerate(value)]


def _safe_path(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or path.as_posix() != value:
        raise LicensePolicyError(f"{label} must be a normalized repository-relative path")
    return value


def _digest(value: str, label: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise LicensePolicyError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _unique_paths(paths: Sequence[str], label: str) -> None:
    duplicates = sorted(path for path in set(paths) if paths.count(path) > 1)
    if duplicates:
        raise LicensePolicyError(f"duplicate {label} paths: {duplicates}")


def load_policy(path: Path) -> LicensePolicy:
    """Load and strictly validate the committed license policy."""

    try:
        raw_value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LicensePolicyError(f"could not read {path.name}: {error}") from error
    raw = _mapping(raw_value, "license policy")
    _exact_keys(raw, set(POLICY_KEYS), "license policy")

    license_expression = _string(raw, "license_expression", "license policy")
    if license_expression != LICENSE_EXPRESSION:
        raise LicensePolicyError(
            f"license policy license_expression must be {LICENSE_EXPRESSION!r}"
        )
    license_digest = _digest(
        _string(raw, "license_text_sha256", "license policy"),
        "license policy.license_text_sha256",
    )
    notice_copyright = _string(raw, "notice_copyright", "license policy")
    extensions = _string_list(raw, "notice_required_extensions", "license policy")
    if any(not extension.startswith(".") for extension in extensions):
        raise LicensePolicyError("notice_required_extensions entries must begin with '.'")
    required_paths = tuple(
        _safe_path(item, "notice_required_paths entry")
        for item in _string_list(raw, "notice_required_paths", "license policy")
    )
    categories = frozenset(_string_list(raw, "exception_categories", "license policy"))

    exceptions: list[ExceptionEntry] = []
    for index, item in enumerate(_object_list(raw, "exceptions", "license policy")):
        label = f"license policy.exceptions[{index}]"
        _exact_keys(item, {"category", "path", "reason"}, label)
        category = _string(item, "category", label)
        if category not in categories:
            raise LicensePolicyError(f"{label}.category is not in exception_categories")
        exceptions.append(
            ExceptionEntry(
                path=_safe_path(_string(item, "path", label), f"{label}.path"),
                category=category,
                reason=_string(item, "reason", label),
            )
        )
    if exceptions != sorted(exceptions, key=lambda item: item.path):
        raise LicensePolicyError("license policy.exceptions must be sorted by path")
    _unique_paths([item.path for item in exceptions], "exception")

    category_rules: list[CategoryRule] = []
    for index, item in enumerate(_object_list(raw, "category_rules", "license policy")):
        label = f"license policy.category_rules[{index}]"
        _exact_keys(item, {"extensions", "name", "paths", "reason"}, label)
        rule_extensions = _string_list(item, "extensions", label)
        if any(not extension.startswith(".") for extension in rule_extensions):
            raise LicensePolicyError(f"{label}.extensions entries must begin with '.'")
        rule_paths = tuple(
            _safe_path(entry.rstrip("/"), f"{label}.paths entry")
            + ("/" if entry.endswith("/") else "")
            for entry in _string_list(item, "paths", label)
        )
        if not rule_extensions and not rule_paths:
            raise LicensePolicyError(f"{label} must declare an extension or path")
        category_rules.append(
            CategoryRule(
                name=_string(item, "name", label),
                extensions=rule_extensions,
                paths=rule_paths,
                reason=_string(item, "reason", label),
            )
        )
    if category_rules != sorted(category_rules, key=lambda item: item.name):
        raise LicensePolicyError("license policy.category_rules must be sorted by name")
    names = [item.name for item in category_rules]
    if len(names) != len(set(names)):
        raise LicensePolicyError("license policy.category_rules contains duplicate names")

    notices: list[ThirdPartyNotice] = []
    for index, item in enumerate(_object_list(raw, "third_party_notices", "license policy")):
        label = f"license policy.third_party_notices[{index}]"
        _exact_keys(item, {"license", "origin", "path", "sha256"}, label)
        notices.append(
            ThirdPartyNotice(
                path=_safe_path(_string(item, "path", label), f"{label}.path"),
                license=_string(item, "license", label),
                origin=_string(item, "origin", label),
                sha256=_digest(_string(item, "sha256", label), f"{label}.sha256"),
            )
        )
    if notices != sorted(notices, key=lambda item: item.path):
        raise LicensePolicyError("license policy.third_party_notices must be sorted by path")
    _unique_paths([item.path for item in notices], "third-party notice")
    registered_notice_paths = {item.path for item in notices}
    unbacked = sorted(
        item.path
        for item in exceptions
        if item.category == "third-party-notice" and item.path not in registered_notice_paths
    )
    if unbacked:
        raise LicensePolicyError(
            "license policy third-party-notice exceptions require a matching "
            f"third_party_notices registration recording origin, license, and digest: "
            f"{', '.join(unbacked)}"
        )

    exclusions: list[ScanExclusion] = []
    for index, item in enumerate(
        _object_list(raw, "third_party_scan_exclusions", "license policy")
    ):
        label = f"license policy.third_party_scan_exclusions[{index}]"
        _exact_keys(item, {"path", "reason"}, label)
        exclusions.append(
            ScanExclusion(
                path=_safe_path(_string(item, "path", label), f"{label}.path"),
                reason=_string(item, "reason", label),
            )
        )
    if exclusions != sorted(exclusions, key=lambda item: item.path):
        raise LicensePolicyError(
            "license policy.third_party_scan_exclusions must be sorted by path"
        )
    _unique_paths([item.path for item in exclusions], "third-party scan exclusion")

    return LicensePolicy(
        license_expression=license_expression,
        license_text_sha256=license_digest,
        notice_copyright=notice_copyright,
        notice_required_extensions=extensions,
        notice_required_paths=required_paths,
        exception_categories=categories,
        exceptions=tuple(exceptions),
        category_rules=tuple(category_rules),
        third_party_notices=tuple(notices),
        third_party_scan_exclusions=tuple(exclusions),
    )


def _tracked_files(repository: Path) -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repository,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise LicensePolicyError(f"git ls-files failed: {detail or 'no diagnostic'}")
    try:
        paths = completed.stdout.decode("utf-8").split("\0")
    except UnicodeDecodeError as error:
        raise LicensePolicyError("git ls-files returned a non-UTF-8 path") from error
    normalized = tuple(sorted(path for path in paths if path))
    if len(normalized) != len(set(normalized)):
        raise LicensePolicyError("git ls-files returned duplicate paths")
    for path in normalized:
        _safe_path(path, "tracked path")
    return normalized


def _text(data: bytes) -> str | None:
    if b"\0" in data:
        return None
    try:
        # A byte-order mark is an encoding artifact, not content. Left in place it
        # detaches the first line from its comment prefix, which reports a
        # first-party header as missing its notice.
        return data.decode("utf-8").lstrip("\ufeff")
    except UnicodeDecodeError:
        return None


# A bare `*` opens a comment continuation only when whitespace follows: ` * text` is
# a C-style banner line, while `**text**` is Markdown emphasis. Treating the second
# as a comment ran the notice region into document body content.
COMMENT_LINE_RE = re.compile(r"^(?://|\#|/\*|<!--|;|\*/|\*(?=\s))")
# Directives a format requires to come first, ahead of any license banner. A CSS
# `@charset` rule must be the very first thing in a stylesheet, so treating it as
# content would report a correctly licensed file as missing its notice.
# A CSS `@charset` rule is exact: lowercase, double-quoted, terminated, and the very
# first bytes of the stylesheet. A `'use strict'` prologue is a statement, so ordinary
# leading whitespace is fine there. Applying either to every format let a directive
# precede a header in a language that does not define it.
# Only UTF-8: the scanner decodes UTF-8, so any other declared label describes a
# file this gate did not read, and an invalid label declares nothing at all.
CSS_CHARSET_RE = re.compile(r'^@charset "(?i:utf-8)";$')
JS_DIRECTIVE_RE = re.compile(r"""^['"]use strict['"];?$""")
JS_DIRECTIVE_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".ts", ".tsx"})


def _is_pre_header_directive(raw: str, stripped: str, suffix: str, index: int) -> bool:
    """Return whether this line is a directive the format requires to come first."""

    if suffix == ".css":
        return index == 0 and CSS_CHARSET_RE.match(raw) is not None
    if suffix in JS_DIRECTIVE_SUFFIXES:
        return JS_DIRECTIVE_RE.match(stripped) is not None
    return False


def _leading_comment_block(text: str, suffix: str = "") -> list[str]:
    """Return the comment lines that open a file, before any other content.

    Scanning stops at the first line that is neither blank, a shebang, nor a
    comment. A notice found after that point is data rather than a file notice:
    without this boundary a source file could satisfy the gate merely by storing
    the marker text inside a string literal.
    """

    block: list[str] = []
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if index == 0 and stripped.startswith("#!"):
            continue
        if _is_pre_header_directive(line, stripped, suffix, index):
            continue
        if not COMMENT_LINE_RE.match(stripped):
            break
        block.append(stripped)
    return block


# One shared definition of a wrapper, used by every notice check. Openers cover
# source comments (`//`, `#`, `;`, `/*`, `/*!`, `<!--`) and the markup that imported
# documents wrap notices in (blockquotes, list bullets, table cells, HTML elements).
# `/*!` matters on its own: it is the conventional "preserved license" banner that
# minifiers keep, and treating it as `/*` plus a stray `!` would reject a valid CSS
# notice.
# `\d+[.)]` covers ordered-list markers such as `1.` and `10)`. Markers that are
# also diff syntax — `+` and `-` — are stripped only when whitespace follows, so a
# fenced diff line such as `+Copyright (c) 2026 Other Author` inside documentation
# is left intact rather than normalized into an apparent file-level notice. The
# same whitespace requirement keeps a notice opening with a bare year untouched.
# `[^>"']|"…"|'…'` keeps a `>` inside a quoted attribute from ending the tag early,
# which would otherwise leave the quote fragment behind and reject a valid notice.
_TAG_BODY = r"""(?:[^>"']|"[^"]*"|'[^']*')*"""
_WRAPPER_OPEN_RE = re.compile(
    rf"^(?:[\s>|*#;]|[-+](?=\s)|//+|/\*+!?|<!--+|<[a-zA-Z]{_TAG_BODY}>|\d{{1,9}}[.)](?=\s))+"
)
# The closing class mirrors the opening one, including the table-cell `|`, so a
# wrapper the opener removes cannot be left behind by the closer.
_WRAPPER_CLOSE_RE = re.compile(rf"(?:[\s*|]|--!>|-->|\*/|#\}}|\?>|</[a-zA-Z]{_TAG_BODY}>)+$")


# Markup can also sit *between* the legal indicator and the holder, as in
# `Copyright (c) <b>2026</b> Other Author`. Stripping only the outer wrappers would
# leave a tag where the pattern expects a year or holder, so the notice would go
# undetected and inherit category coverage unregistered.
# `<!DOCTYPE …>` declarations and `<?xml …?>` processing instructions open a
# document and can never be a notice or an example of one, so they are markup too.
_INLINE_TAG_RE = re.compile(rf"</?(?:[a-zA-Z]|![a-zA-Z]){_TAG_BODY}>|<\?{_TAG_BODY}\?>")
# Inline Markdown emphasis and link labels wrap holders the same way tags do.
_INLINE_MARKDOWN_RE = re.compile(r"\*{1,3}|_{2,3}|`+|\[|\]\([^)]*\)|\]")
# YAML and TOML front matter, which opens a document before any header notice.
FRONT_MATTER_DELIMITERS = ("---", "+++")
_MAX_NORMALIZATION_PASSES = 8


def _strip_comments(line: str) -> str:
    """Return a line's content with only its comment delimiters removed.

    Used when deciding whether a file *asserts* the required notice. That question
    must not tolerate markup: `# <del>Copyright (c) Picogrid, Inc.</del>` marks the
    notice as removed, and normalizing the tags away would accept it, recreating the
    negated-header bypass. Detecting someone *else's* notice is the opposite problem
    and uses `_strip_wrappers`, which removes markup deliberately.
    """

    text = line.strip()
    text = re.sub(r"^(?://+|\#+|/\*+!?|\*+|<!--+|;+)\s*", "", text)
    for terminator in COMMENT_TERMINATORS:
        if text.endswith(terminator):
            text = text[: -len(terminator)]
            break
    return text.strip()


def _strip_wrappers(line: str) -> str:
    """Return a line's content with comment and markup wrappers removed.

    Applied repeatedly until stable, because one wrapper can hide another: a
    minified document may put a declaration and a comment on the same line, so
    removing the declaration is what exposes the comment delimiters beneath it.
    """

    text = line.strip()
    for _ in range(_MAX_NORMALIZATION_PASSES):
        # Order matters twice over. Whole constructs first, because the closing
        # class shares terminators with them and would strand a processing
        # instruction's `?>`. Then comment delimiters, before Markdown emphasis,
        # because `/*!` and `*/` are comment syntax whose asterisks would otherwise
        # be eaten as emphasis.
        reduced = _INLINE_TAG_RE.sub("", text)
        reduced = _WRAPPER_CLOSE_RE.sub("", _WRAPPER_OPEN_RE.sub("", reduced))
        reduced = _INLINE_MARKDOWN_RE.sub("", reduced)
        reduced = re.sub(r"\s+", " ", reduced).strip()
        if reduced == text:
            break
        text = reduced
    return text


# A `<` only opens markup when it is not glued to a preceding word: `x<y` is a
# comparison, not a tag, and treating it as an opener would join unrelated lines.
_RESIDUAL_OPENER_RE = re.compile(r"<!--|/\*|(?<!\w)<\?|(?<!\w)<!?/?[a-zA-Z]")
# Document-head scaffolding, a closed set. These may sit between the start of a
# document and its notice; content-bearing body markup may not.
HEAD_ELEMENT_RE = re.compile(
    r"^</?(?:html|head|title|meta|link|base|style|script)\b", re.IGNORECASE
)
# A document head is a closed container: once it opens, everything inside it is
# scaffolding until it closes, however its content is formatted across lines.
HEAD_OPEN_RE = re.compile(r"<head(?:\s[^>]*)?>", re.IGNORECASE)
HEAD_CLOSE_RE = re.compile(r"</head\s*>", re.IGNORECASE)
# A tag can *be* the notice: `<meta name="copyright" content="Copyright (c) …">`.
# Dropping the element would discard the value, so attribute values are inspected
# before normalization removes their tags.
_ATTRIBUTE_VALUE_RE = re.compile(r"=\s*\"([^\"]*)\"|=\s*'([^']*)'")


_BLOCK_OPENERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("<!--", ("-->", "--!>")),
    ("/*", ("*/",)),
    ("<?", ("?>",)),
)
_TAG_START_RE = re.compile(r"(?<!\w)</?[a-zA-Z!]")
# A tag closes at the first `>` that is not inside a quoted attribute. Quote state
# rides along in the open-construct state, because an attribute value may itself
# wrap across physical lines.
_TAG_MARKER = "\x00tag"


def _advance_markup_state(line: str, state: tuple[str, ...] | None) -> tuple[str, ...] | None:
    """Return the open-construct state after reading one more line.

    Each line is read once and only the state carries forward, so following a
    construct is linear in its size. Rejoining and rescanning the whole accumulated
    group on every line made traversal quadratic, which a large generated document
    or bundled license banner could turn into a stalled release gate.

    `None` means nothing is open; otherwise the value is the set of terminators that
    would close the construct currently open.
    """

    index = 0
    while index <= len(line):
        if state is not None and state[0] == _TAG_MARKER:
            quote = state[1]
            position = index
            while position < len(line):
                char = line[position]
                if quote:
                    if char == quote:
                        quote = ""
                elif char in "\"'":
                    quote = char
                elif char == ">":
                    break
                position += 1
            if position >= len(line):
                # The tag continues on the next line, and so may an open quote.
                return (_TAG_MARKER, quote)
            index = position + 1
            state = None
            continue
        if state is not None:
            found = [(line.find(term, index), term) for term in state]
            closing = (
                min((pos, term) for pos, term in found if pos != -1)
                if any(pos != -1 for pos, _ in found)
                else None
            )
            if closing is None:
                return state
            index = closing[0] + len(closing[1])
            state = None
            continue
        # Ordered by position only. At the same position a block opener must win
        # over the one-character tag candidate: for `<!-- … > … -->` both match at
        # zero, and choosing the tag lets a `>` inside the comment close it.
        candidates: list[tuple[int, int, tuple[str, ...]]] = []
        for opener, terminators in _BLOCK_OPENERS:
            position = line.find(opener, index)
            if position != -1:
                candidates.append((position, len(opener), terminators))
        tag = _TAG_START_RE.search(line, index)
        if tag is not None:
            candidates.append((tag.start(), 1, (_TAG_MARKER, "")))
        if not candidates:
            return None
        position, width, terminators = min(candidates, key=lambda candidate: candidate[0])
        index = position + width
        state = terminators
    return state


def _has_unterminated_markup(text: str) -> bool:
    """Return whether a construct opens in this text without closing in it."""

    state: tuple[str, ...] | None = None
    for line in text.splitlines() or [text]:
        state = _advance_markup_state(line, state)
    return state is not None


def _logical_lines(text: str) -> list[list[str]]:
    """Group physical lines, joining any that split a markup construct.

    A tag, comment, or processing instruction may wrap across lines before a header
    notice. Judging each physical line alone ends the notice region on the fragment
    and hides the notice beneath it, so an unterminated construct is grouped with
    the lines that close it.

    A complete construct is followed to its closing delimiter however long it runs,
    with no line cap: a cap ends a valid group early and hides the notice inside it.
    Input that never closes is the opposite case and is not joined at all, so a stray
    `<` cannot swallow the document. State advances one line at a time, so following
    a construct costs one pass over it rather than one per line.

    Groups keep their physical lines. The joined form decides where the region ends;
    the physical lines are what get scanned, because the notice pattern is anchored
    and a flattened comment would bury the notice behind its preceding prose.
    """

    lines = text.splitlines()
    groups: list[list[str]] = []
    index = 0
    # Once a forward scan reaches the end without closing, no terminator of *that
    # kind* exists beyond that point, so a later opener of the same kind cannot close
    # either. The memo is keyed by kind: a file whose `/*` never closes may still
    # contain a tag that does, and a position-only memo would skip that join.
    unterminated_from: dict[tuple[str, ...], int] = {}
    while index < len(lines):
        state = _advance_markup_state(lines[index], None)
        group = [lines[index]]
        cached = unterminated_from.get(state) if state is not None else None
        if state is not None and (cached is None or index < cached):
            probe = index
            while probe + 1 < len(lines) and state is not None:
                probe += 1
                group.append(lines[probe])
                state = _advance_markup_state(lines[probe], state)
            if state is not None:
                group = [lines[index]]
                unterminated_from[state] = index
            else:
                index = probe
        groups.append(group)
        index += 1
    return groups


def _leading_notice_region(text: str) -> list[str]:
    """Return the lines that can carry a file-level notice.

    A copyright notice that governs a file sits in its opening region: a comment
    header, front matter, or the first lines of a document. Prose further down is
    something else — a licensing explanation, a quoted example, a changelog entry —
    and deciding whether such a line *is* a notice rather than a discussion of one
    cannot be done from syntax. Scanning it produced findings in both directions at
    once: emphasis-wrapped holders that had to be detected, and `<code>` examples
    that had to be ignored.

    The boundary ends the class. Scanning stops at the first line that is neither
    blank, a shebang, a comment, nor itself notice-shaped. SPDX identifiers are
    machine syntax, unambiguous anywhere, and are matched over the whole file
    separately, so a tagged third-party file is still caught wherever it declares
    itself.

    Inside the region the gate is deliberately fail-closed: a copyright-shaped line
    is a notice even when it carries code markers. Reviewers asked for the opposite
    treatments of the same position — traverse a `<!DOCTYPE>` declaration, but
    preserve a backtick-quoted example — and no syntactic rule delivers both,
    because whether a line at the top of a document *is* a notice or *shows* one is
    a semantic question. A false positive here names the file and is cleared by
    registering it or excluding it; a false negative admits unlicensed material
    silently. The gate takes the recoverable error.

    Physical lines are joined into logical ones first, so a tag, comment, or
    processing instruction that wraps across lines cannot end the region on its own
    fragment and hide the notice beneath it.
    """

    groups = _logical_lines(text.lstrip("\ufeff"))
    joined = [" ".join(group).strip() for group in groups]
    region: list[str] = []
    index = 0
    if joined and joined[0].startswith("#!"):
        region.extend(groups[0])
        index = 1

    # Conventional front matter opens a document before its header notice, and its
    # delimiter is neither a comment nor notice-shaped, so it would otherwise end
    # the region before the notice it precedes.
    probe = index
    while probe < len(joined) and not joined[probe]:
        probe += 1
    if probe < len(joined) and joined[probe] in FRONT_MATTER_DELIMITERS:
        delimiter = joined[probe]
        closing = next(
            (i for i in range(probe + 1, len(joined)) if joined[i] == delimiter),
            None,
        )
        if closing is not None:
            for group in groups[index : closing + 1]:
                region.extend(group)
            index = closing + 1

    in_head = False

    def _emit(group: list[str], line: str) -> None:
        """Record a group's physical lines, plus its joined form when it spans lines.

        Anchored notice matching needs the physical lines. An attribute value split
        across lines only exists in the joined form, so both are offered.
        """

        region.extend(group)
        if len(group) > 1:
            region.append(line)

    for group, line in zip(groups[index:], joined[index:], strict=True):
        if not line:
            region.extend(group)
            continue
        normalized = _strip_wrappers(line)
        # What may precede a notice is document scaffolding, which is a closed set:
        # markup that carries no content of its own, the head elements, and an
        # unresolved construct whose extent could not be determined. Content-bearing
        # body markup such as `<p>Introduction</p>` is not scaffolding; treating any
        # markup-bearing line as such extended the region through the body and
        # reported examples below it, defeating the position boundary.
        head_open = HEAD_OPEN_RE.search(line)
        if head_open is not None and not _strip_wrappers(line[: head_open.start()]):
            in_head = True
        if (
            in_head
            or not normalized
            or HEAD_ELEMENT_RE.match(line)
            or _has_unterminated_markup(line)
            or COMMENT_LINE_RE.match(line)
            or COPYRIGHT_NOTICE_RE.search(normalized)
        ):
            _emit(group, line)
            # A close written inside a comment is text, not structure.
            if HEAD_CLOSE_RE.search(_BLOCK_COMMENT_RE.sub(" ", line)):
                in_head = False
            continue
        break
    return region


_BLOCK_COMMENT_RE = re.compile(r"/\*+!?(.*?)\*/|<!--+(.*?)(?:-->|--!>)", re.DOTALL)


def _comment_contents(line: str) -> list[str]:
    """Return the content of each comment on a line, independently.

    A valid header may place two block comments on one physical line:

        /* Copyright (c) Picogrid, Inc. */ /* SPDX-License-Identifier: MPL-2.0 */

    Stripping one opener and one terminator leaves `*/ /*` inside the value and the
    exact comparison rejects a file whose notice is present, so each comment is read
    on its own.
    """

    contents = [
        value.strip()
        for match in _BLOCK_COMMENT_RE.finditer(line)
        for value in match.groups()
        if value is not None
    ]
    remainder = _BLOCK_COMMENT_RE.sub(" ", line).strip()
    if remainder:
        contents.append(_strip_comments(remainder))
    return [value for value in contents if value]


def _is_affirmative_notice(line: str, policy: LicensePolicy) -> bool:
    """Return whether a comment line states exactly the configured notice.

    Substring acceptance would let a header such as
    ``# Previous header: Copyright (c) Picogrid, Inc. removed`` satisfy the
    requirement while explicitly denying it, so the line's content must be the
    notice itself. Only comment delimiters are removed: markup that marks content as
    deleted or quoted, as in ``# <del>Copyright (c) Picogrid, Inc.</del>``, must not
    be normalized away into an assertion the file does not make.
    """

    return any(content == policy.notice_copyright for content in _comment_contents(line))


def _has_inline_notice(data: bytes, policy: LicensePolicy, suffix: str = "") -> bool:
    """Return whether the file opens with a notice for exactly the policy license.

    The notice must appear in the file's leading comment block, and the identifier
    is parsed and compared as a whole value rather than matched as a substring, so
    neither a marker buried in a string literal nor a neighbouring expression such
    as ``MPL-2.0-or-later`` can satisfy a policy that requires ``MPL-2.0``.
    """

    text = _text(data)
    if text is None:
        return False
    block = _leading_comment_block(text, suffix)
    if not any(_is_affirmative_notice(line, policy) for line in block):
        return False
    return any(
        SPDX_MARKER in line
        and _spdx_value(line.split(SPDX_MARKER, 1)[1]) == policy.license_expression
        for line in block
    )


COMMENT_TERMINATORS = ("--!>", "-->", "*/", "#}", "?>")


def _spdx_value(remainder: str) -> str:
    """Return the license expression from the text following an SPDX marker.

    Comment syntax surrounds the identifier in most formats, so one complete
    terminator is removed before the value is compared with a registered license.
    Only whitespace is trimmed afterwards: stripping stray ``*``, ``/`` or ``#``
    characters would normalize a malformed identifier such as ``MPL-2.0#`` into a
    valid one and let it satisfy the exact-license gate.
    """

    value = remainder.strip()
    for terminator in COMMENT_TERMINATORS:
        if value.endswith(terminator):
            value = value[: -len(terminator)]
            break
    return value.strip()


def _names_another_holder(line: str, policy: LicensePolicy) -> bool:
    """Return whether a copyright line names a holder besides Picogrid.

    A combined notice such as ``Copyright (c) Picogrid, Inc. and Other Author``
    contains the Picogrid notice verbatim, so treating any line that contains it as
    exclusively Picogrid-owned would hide the co-holder. Wrappers are removed with
    the same normalizer the detector uses before the remainder is inspected, so
    first-party content wrapped as ``<p>Copyright (c) Picogrid, Inc.</p>`` is not
    reported because of its tag names.
    """

    remainder = _strip_wrappers(line).replace(policy.notice_copyright, " ")
    remainder = re.sub(
        rf"\b(?:and|all rights reserved|copyright|inc|llc|ltd)\b|{re.escape(SPDX_COPYRIGHT_MARKER)}",
        " ",
        remainder,
        flags=re.IGNORECASE,
    )
    remainder = re.sub(r"[\d\W_]+", " ", remainder)
    return bool(remainder.strip())


def _spdx_values(text: str) -> set[str]:
    """Return every SPDX identifier declared in the text, including the policy one."""

    return {
        value
        for line in text.splitlines()
        if SPDX_MARKER in line
        for value in (_spdx_value(line.split(SPDX_MARKER, 1)[1]),)
        if value
    }


def _foreign_notice_values(text: str, policy: LicensePolicy) -> tuple[set[str], bool]:
    """Return foreign SPDX identifiers anywhere, and a foreign holder in the header.

    SPDX identifiers are machine syntax and are matched over the whole file.
    Copyright shapes are matched only in the leading notice region, because prose
    that merely discusses or quotes a notice is not a provenance event.
    """

    licenses = {value for value in _spdx_values(text) if value != policy.license_expression}
    foreign_copyright = False
    for line in _leading_notice_region(text):
        # A tag can carry the notice in an attribute, so candidates are the
        # normalized line plus every quoted attribute value. Both the detection and
        # the first-party comparison run on those normalized forms: testing
        # ownership against the raw line would treat Picogrid's own notice as
        # foreign whenever markup sits inside it.
        candidates = [_strip_wrappers(line)]
        candidates.extend(
            _strip_wrappers(value)
            for match in _ATTRIBUTE_VALUE_RE.finditer(line)
            for value in match.groups()
            if value
        )
        for candidate in candidates:
            if not COPYRIGHT_NOTICE_RE.search(candidate):
                continue
            if policy.notice_copyright not in candidate or _names_another_holder(candidate, policy):
                foreign_copyright = True
    return licenses, foreign_copyright


def _read_tracked_files(
    repository: Path, paths: Sequence[str], findings: list[Finding]
) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}
    for relative in paths:
        try:
            contents[relative] = (repository / relative).read_bytes()
        except OSError as error:
            findings.append(
                Finding(relative, "tracked-file", f"tracked file could not be read: {error}")
            )
    return contents


def _check_license_digest(
    contents: Mapping[str, bytes], policy: LicensePolicy, findings: list[Finding]
) -> None:
    """Require every copy of the authoritative license text to be byte-exact.

    The root ``LICENSE`` is the authoritative text. A build context that cannot see
    it — currently the standalone operator application — carries its own copy, which
    is exempted from an inline notice by a ``license-text`` exception. That category
    is verified rather than trusted: every file in it must hash to the same pinned
    digest, so a second copy cannot silently drift from the terms the packages
    declare.
    """

    license_paths = ["LICENSE"]
    license_paths.extend(
        entry.path
        for entry in policy.exceptions
        if entry.category == "license-text" and entry.path != "LICENSE"
    )
    for relative in license_paths:
        data = contents.get(relative)
        if data is None:
            findings.append(
                Finding(relative, "root-license", "authoritative license text is missing")
            )
            continue
        actual = hashlib.sha256(data).hexdigest()
        if actual != policy.license_text_sha256:
            findings.append(
                Finding(
                    relative,
                    "root-license",
                    "license digest drifted: expected "
                    f"{policy.license_text_sha256}, actual {actual}",
                )
            )


def _check_coverage(
    paths: Sequence[str],
    contents: Mapping[str, bytes],
    policy: LicensePolicy,
    findings: list[Finding],
) -> tuple[int, int, int]:
    tracked = set(paths)
    exception_by_path = {entry.path: entry for entry in policy.exceptions}
    inline_paths = {
        path
        for path, data in contents.items()
        if _has_inline_notice(data, policy, PurePosixPath(path).suffix)
    }
    for exception in policy.exceptions:
        if exception.path not in tracked:
            findings.append(
                Finding(exception.path, "coverage", "stale exception does not name a tracked file")
            )
        elif exception.path in inline_paths:
            findings.append(
                Finding(
                    exception.path,
                    "coverage",
                    "redundant exception: the file now carries a valid inline notice",
                )
            )

    rule_hits: dict[str, list[str]] = {rule.name: [] for rule in policy.category_rules}
    category_rule_files = 0
    exception_files = 0
    for path in paths:
        required = (
            PurePosixPath(path).suffix in policy.notice_required_extensions
            or path in policy.notice_required_paths
        )
        rules = [rule for rule in policy.category_rules if rule.matches(path)]
        for rule in rules:
            rule_hits[rule.name].append(path)
        # Precedence, most specific first: an inline notice, then an explicit
        # exception, then a blanket category rule. A notice-required path is
        # satisfied only by the first two, because category rules describe formats
        # that cannot carry a notice and letting one cover a required path would
        # silently relax the stricter rule. An explicit exception also wins over a
        # category rule that happens to match the same path, so a documented,
        # reasoned decision is not reported as double coverage.
        mechanisms: list[str] = []
        if path in inline_paths:
            mechanisms.append("inline notice")
        if path in exception_by_path:
            mechanisms.append("exception")
            exception_files += 1
        elif not required:
            mechanisms.extend(f"category rule {rule.name!r}" for rule in rules)
            if rules:
                category_rule_files += 1
        if not mechanisms:
            message = (
                "notice-required file is missing a valid inline notice"
                if required
                else "tracked file is not accounted for by an inline notice, exception, "
                "or category rule"
            )
            findings.append(Finding(path, "coverage", message))
        elif len(mechanisms) > 1:
            findings.append(
                Finding(
                    path,
                    "coverage",
                    "file is accounted for more than once: " + ", ".join(mechanisms),
                )
            )
    for rule in policy.category_rules:
        if not rule_hits[rule.name]:
            findings.append(
                Finding(
                    "scripts/license-policy.json",
                    "coverage",
                    f"category rule {rule.name!r} matches no tracked files",
                )
            )
    return len(inline_paths), exception_files, category_rule_files


def _check_third_party_notices(
    paths: Sequence[str],
    contents: Mapping[str, bytes],
    policy: LicensePolicy,
    findings: list[Finding],
) -> None:
    tracked = set(paths)
    registry = {entry.path: entry for entry in policy.third_party_notices}
    exclusions = {entry.path for entry in policy.third_party_scan_exclusions}
    for exclusion in policy.third_party_scan_exclusions:
        if exclusion.path not in tracked:
            findings.append(
                Finding(
                    exclusion.path,
                    "third-party-notice",
                    "stale third-party scan exclusion does not name a tracked file",
                )
            )
    for entry in policy.third_party_notices:
        data = contents.get(entry.path)
        if entry.path not in tracked or data is None:
            findings.append(
                Finding(
                    entry.path,
                    "third-party-notice",
                    "registered third-party notice is missing from the tracked tree",
                )
            )
            continue
        actual = hashlib.sha256(data).hexdigest()
        if actual != entry.sha256:
            findings.append(
                Finding(
                    entry.path,
                    "third-party-notice",
                    f"third-party notice digest changed: expected {entry.sha256}, actual {actual}",
                )
            )

    # Only a `third-party-notice` exception satisfies this check. `load_policy`
    # requires those to carry an origin, license, and pinned digest, so provenance is
    # still recorded. Any other category — `generated`, `brand-asset` — asserts
    # nothing about provenance, and accepting it would let a miscategorized entry
    # bypass the registry entirely.
    excepted = {entry.path for entry in policy.exceptions if entry.category == "third-party-notice"}
    # Notice-required formats are text too. Limiting the fail-closed scan to
    # category-rule matches let an undecodable `.css` or `.py` with a non-provenance
    # exception satisfy coverage while its bytes went unread.
    text_categories = {
        path
        for path in contents
        if any(rule.matches(path) for rule in policy.category_rules)
        or PurePosixPath(path).suffix in policy.notice_required_extensions
        or path in policy.notice_required_paths
    }
    for path, data in contents.items():
        if path in exclusions:
            continue
        text = _text(data)
        if text is None:
            # A category rule asserts the file is text covered by the root license.
            # Skipping undecodable content would let a Latin-1 file carrying a
            # foreign notice inherit that coverage unscanned, so it must be
            # declared explicitly instead.
            if path in text_categories and path not in registry and path not in excepted:
                findings.append(
                    Finding(
                        path,
                        "third-party-notice",
                        "category-covered file is not UTF-8 text and cannot be scanned "
                        "for provenance; declare a third-party-notice exception with a "
                        "registration, or a scan exclusion",
                    )
                )
            continue
        foreign_licenses, foreign_copyright = _foreign_notice_values(text, policy)
        if not foreign_licenses and not foreign_copyright:
            continue
        registered = registry.get(path)
        if registered is None:
            findings.append(
                Finding(
                    path,
                    "third-party-notice",
                    "unregistered third-party notice or copyright statement",
                )
            )
        else:
            # Compare against every identifier the file declares, not only the
            # foreign ones: an upstream file may itself be MPL-2.0, and dropping
            # that value would let a registration claim an unrelated license.
            declared = _spdx_values(text)
            if declared and registered.license not in declared:
                findings.append(
                    Finding(
                        path,
                        "third-party-notice",
                        f"registered license {registered.license!r} does not match declared "
                        f"identifiers {sorted(declared)}",
                    )
                )
    # A registration whose file no longer carries a third-party notice is stale and
    # must be removed - unless the file is also a documented `third-party-notice`
    # exception, which is precisely how an upstream file with an absent or
    # unrecognized header records its provenance.
    documented_headerless = {
        entry.path for entry in policy.exceptions if entry.category == "third-party-notice"
    }
    for entry in policy.third_party_notices:
        data = contents.get(entry.path)
        if data is None or entry.path in exclusions or entry.path in documented_headerless:
            continue
        text = _text(data)
        if text is None:
            findings.append(
                Finding(
                    entry.path,
                    "third-party-notice",
                    "registered third-party notice is not UTF-8 text",
                )
            )
            continue
        foreign_licenses, foreign_copyright = _foreign_notice_values(text, policy)
        if not foreign_licenses and not foreign_copyright:
            findings.append(
                Finding(
                    entry.path,
                    "third-party-notice",
                    "registered third-party notice contains no foreign notice",
                )
            )


def _toml_document(path: Path, relative: str, findings: list[Finding]) -> dict[str, object] | None:
    try:
        with path.open("rb") as stream:
            value: object = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        findings.append(Finding(relative, "metadata", f"could not parse TOML metadata: {error}"))
        return None
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        findings.append(Finding(relative, "metadata", "TOML metadata root must be a table"))
        return None
    return cast(dict[str, object], value)


def _json_document(path: Path, relative: str, findings: list[Finding]) -> dict[str, object] | None:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        findings.append(Finding(relative, "metadata", f"could not parse JSON metadata: {error}"))
        return None
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        findings.append(Finding(relative, "metadata", "JSON metadata root must be an object"))
        return None
    return cast(dict[str, object], value)


def _check_metadata(repository: Path, policy: LicensePolicy, findings: list[Finding]) -> None:
    for relative in ("pyproject.toml", "operator-app/pyproject.toml"):
        document = _toml_document(repository / relative, relative, findings)
        if document is None:
            continue
        project_value = document.get("project")
        if not isinstance(project_value, dict) or not all(
            isinstance(key, str) for key in project_value
        ):
            findings.append(Finding(relative, "metadata", "project must be a TOML table"))
            continue
        project = cast(dict[str, object], project_value)
        actual = project.get("license")
        if actual != policy.license_expression:
            findings.append(
                Finding(
                    relative,
                    "metadata",
                    f"project.license must equal {policy.license_expression!r}; found {actual!r}",
                )
            )
        if relative == "pyproject.toml":
            license_files = project.get("license-files")
            if not isinstance(license_files, list) or "LICENSE" not in license_files:
                findings.append(
                    Finding(
                        relative,
                        "metadata",
                        "project.license-files must include 'LICENSE'",
                    )
                )

    for relative in ("docs/package.json", "operator-app/package.json"):
        document = _json_document(repository / relative, relative, findings)
        if document is None:
            continue
        actual = document.get("license")
        if actual != policy.license_expression:
            findings.append(
                Finding(
                    relative,
                    "metadata",
                    f"license must equal {policy.license_expression!r}; found {actual!r}",
                )
            )

    relative = "scripts/release-policy.json"
    release_policy = _json_document(repository / relative, relative, findings)
    if release_policy is not None:
        actual = release_policy.get("license_expression")
        if actual != policy.license_expression:
            findings.append(
                Finding(
                    relative,
                    "metadata",
                    f"license_expression must equal {policy.license_expression!r}; "
                    f"found {actual!r}",
                )
            )
        digest = release_policy.get("license_text_sha256")
        if digest != policy.license_text_sha256:
            findings.append(
                Finding(
                    relative,
                    "metadata",
                    "license_text_sha256 must equal the license policy digest "
                    f"{policy.license_text_sha256!r}; found {digest!r}",
                )
            )


def evaluate_policy(
    repository: Path,
    policy_path: Path,
    *,
    tracked_files: Sequence[str] | None = None,
) -> PolicyReport:
    """Evaluate all license checks for a repository and return a normalized report."""

    policy = load_policy(policy_path)
    paths = (
        tuple(sorted(tracked_files)) if tracked_files is not None else _tracked_files(repository)
    )
    if len(paths) != len(set(paths)):
        raise LicensePolicyError("tracked file inventory contains duplicate paths")
    for path in paths:
        _safe_path(path, "tracked path")

    findings: list[Finding] = []
    contents = _read_tracked_files(repository, paths, findings)
    _check_license_digest(contents, policy, findings)
    inline_notices, exceptions, category_rule_files = _check_coverage(
        paths, contents, policy, findings
    )
    _check_third_party_notices(paths, contents, policy, findings)
    _check_metadata(repository, policy, findings)
    return PolicyReport(
        files_scanned=len(paths),
        inline_notices=inline_notices,
        exceptions=exceptions,
        category_rule_files=category_rule_files,
        findings=tuple(sorted(set(findings))),
    )


def _print_human(report: PolicyReport) -> None:
    if report.findings:
        print("License policy check failed:", file=sys.stderr)
        for finding in report.findings:
            print(
                f"  - {finding.path}: [{finding.check}] {finding.message}",
                file=sys.stderr,
            )
        return
    print(
        "License policy passed: "
        f"files scanned={report.files_scanned}, inline notices={report.inline_notices}, "
        f"exceptions={report.exceptions}, category rules={report.category_rule_files}."
    )


def main() -> int:
    """Run the deterministic license policy gate."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a normalized machine-readable report",
    )
    arguments = parser.parse_args()
    try:
        report = evaluate_policy(REPOSITORY, POLICY_PATH)
    except (LicensePolicyError, OSError, ValueError) as error:
        if arguments.json:
            payload = {
                "counts": {
                    "category_rule_files": 0,
                    "exceptions": 0,
                    "files_scanned": 0,
                    "inline_notices": 0,
                },
                "findings": [
                    {
                        "check": "policy",
                        "message": str(error),
                        "path": "scripts/license-policy.json",
                    }
                ],
                "ok": False,
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"License policy check failed: {error}", file=sys.stderr)
        return 1
    if arguments.json:
        print(json.dumps(report.as_json(), indent=2, sort_keys=True))
    else:
        _print_human(report)
    return 1 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
