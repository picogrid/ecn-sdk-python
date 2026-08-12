# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Resolve a public release from immutable tag and GitHub release state."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


class ResolutionError(ValueError):
    """Release state is ambiguous or malformed."""


@dataclass(frozen=True)
class Resolution:
    action: Literal["create-draft", "create-tag-and-draft", "published", "resume"]
    html_url: str
    release_id: int | None
    release_created: bool
    release_sha: str


def _sha(value: str, label: str, *, optional: bool = False) -> str:
    normalized = value.strip().lower()
    if optional and not normalized:
        return ""
    if not _SHA_PATTERN.fullmatch(normalized):
        raise ResolutionError(f"{label} must be a full lowercase Git commit SHA")
    return normalized


def resolve_release(
    *,
    tag: str,
    current_sha: str,
    tag_sha: str,
    releases: list[dict[str, Any]],
) -> Resolution:
    """Select the only safe action for one version-bearing release tag."""

    current_sha = _sha(current_sha, "current SHA")
    tag_sha = _sha(tag_sha, "tag SHA", optional=True)
    matching = [release for release in releases if release.get("tag_name") == tag]
    if len(matching) > 1:
        raise ResolutionError(f"multiple releases claim {tag}; remove duplicates before retrying")

    if matching:
        release = matching[0]
        draft = release.get("draft")
        html_url = release.get("html_url")
        release_id = release.get("id")
        if (
            not isinstance(draft, bool)
            or not isinstance(html_url, str)
            or not html_url
            or type(release_id) is not int
            or release_id <= 0
        ):
            raise ResolutionError(f"release record for {tag} is malformed")
        if not tag_sha:
            raise ResolutionError(f"release {tag} exists without its immutable tag")
        if draft:
            return Resolution("resume", html_url, release_id, True, tag_sha)
        return Resolution("published", html_url, release_id, False, tag_sha)

    if tag_sha:
        return Resolution("create-draft", "", None, True, tag_sha)
    return Resolution("create-tag-and-draft", "", None, True, current_sha)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--current-sha", required=True)
    parser.add_argument("--tag-sha", default="")
    parser.add_argument("--releases-json", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    arguments = _parse_args()
    try:
        raw = json.loads(arguments.releases_json.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise ResolutionError("GitHub releases response must be a JSON array of objects")
        resolution = resolve_release(
            tag=arguments.tag,
            current_sha=arguments.current_sha,
            tag_sha=arguments.tag_sha,
            releases=raw,
        )
    except (OSError, json.JSONDecodeError, ResolutionError) as error:
        print(f"release resolution failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(resolution), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
