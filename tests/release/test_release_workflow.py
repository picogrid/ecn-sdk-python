# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPOSITORY = Path(__file__).parents[2]
RESOLVER = REPOSITORY / "scripts" / "release_resolution.py"


def _resolve(
    tmp_path: Path, releases: list[dict[str, object]], tag_sha: str = "a" * 40
) -> subprocess.CompletedProcess[str]:
    releases_path = tmp_path / "releases.json"
    releases_path.write_text(json.dumps(releases), encoding="utf-8")
    command = [
        sys.executable,
        str(RESOLVER),
        "--tag",
        "v0.1.0",
        "--current-sha",
        "b" * 40,
        "--releases-json",
        str(releases_path),
    ]
    if tag_sha:
        command.extend(("--tag-sha", tag_sha))
    return subprocess.run(command, capture_output=True, text=True, check=False)


def test_release_resolution_resumes_one_draft_from_immutable_tag(tmp_path: Path) -> None:
    draft = {
        "id": 101,
        "draft": True,
        "html_url": "https://github.com/picogrid/ecn-sdk-python/releases/tag/untagged-one",
        "tag_name": "v0.1.0",
    }

    result = _resolve(tmp_path, [draft])

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "action": "resume",
        "html_url": draft["html_url"],
        "release_id": 101,
        "release_created": True,
        "release_sha": "a" * 40,
    }


def test_release_resolution_refuses_duplicate_drafts(tmp_path: Path) -> None:
    drafts = [
        {
            "id": number,
            "draft": True,
            "html_url": f"https://github.com/picogrid/ecn-sdk-python/releases/tag/untagged-{number}",
            "tag_name": "v0.1.0",
        }
        for number in (1, 2)
    ]

    result = _resolve(tmp_path, drafts)

    assert result.returncode != 0
    assert "multiple releases claim v0.1.0" in result.stderr


def test_release_resolution_uses_current_commit_only_before_tag_creation(tmp_path: Path) -> None:
    result = _resolve(tmp_path, [], tag_sha="")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "action": "create-tag-and-draft",
        "html_url": "",
        "release_id": None,
        "release_created": True,
        "release_sha": "b" * 40,
    }


def test_release_resolution_stops_after_published_release(tmp_path: Path) -> None:
    published = {
        "id": 202,
        "draft": False,
        "html_url": "https://github.com/picogrid/ecn-sdk-python/releases/tag/v0.1.0",
        "tag_name": "v0.1.0",
    }

    result = _resolve(tmp_path, [published])

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "action": "published",
        "release_id": 202,
        "html_url": published["html_url"],
        "release_created": False,
        "release_sha": "a" * 40,
    }


def test_release_workflow_normalizes_boolean_output_before_job_conditions() -> None:
    workflow = (REPOSITORY / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "steps.resolved.outputs.release_created" in workflow
    assert workflow.count("fromJSON(needs.resolve-release.outputs.release_created || 'false')") == 4
    assert "steps.resolved.outputs.body" not in workflow
    assert "PASSTHROUGH_EOF" not in workflow
    assert "RESOLVED_BODY_EOF" not in workflow


def test_release_workflow_promotes_and_replaces_draft_assets_by_release_id() -> None:
    workflow = (REPOSITORY / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "release_id: ${{ steps.resolved.outputs.release_id }}" in workflow
    assert "RELEASE_ID: ${{ needs.resolve-release.outputs.release_id }}" in workflow
    assert 'gh release view "$RELEASE_TAG"' not in workflow
    assert 'gh release upload "$RELEASE_TAG"' not in workflow
    assert 'gh release edit "$RELEASE_TAG"' not in workflow
    assert '"repos/${GITHUB_REPOSITORY}/releases/${RELEASE_ID}"' in workflow
    assert '"repos/${GITHUB_REPOSITORY}/releases/assets/${asset_id}"' in workflow
    assert "unexpected existing draft asset" in workflow
    assert "uploads.github.com/repos/${GITHUB_REPOSITORY}/releases/${RELEASE_ID}/assets" in workflow
    policy = json.loads(
        (REPOSITORY / "scripts" / "release-policy.json").read_text(encoding="utf-8")
    )
    assert any(host == "uploads.github.com" for host in policy["approved_public_hostnames"])


def _job_block(workflow: str, job: str) -> str:
    # Slice a single job's YAML block, from its two-space-indented header to the
    # next top-level job header, so conditions are checked against that job alone
    # rather than the whole file.
    marker = f"\n  {job}:\n"
    start = workflow.index(marker) + len(marker)
    rest = workflow[start:]
    nxt = re.search(r"\n  \S", rest)
    return rest[: nxt.start()] if nxt else rest


def test_downstream_jobs_survive_the_skipped_release_please() -> None:
    # release-please is always skipped on the public distribution repository,
    # and GitHub Actions propagates that skip transitively through the needs
    # graph. Every job downstream of resolve-release must override the skip with
    # !cancelled() or the entire publish pipeline silently skips and no
    # artifacts are ever attached. Assertions are bound to each job's own block
    # so one job cannot lose a condition while another supplies the same text.
    workflow = (REPOSITORY / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    # resolve-release plus every job downstream of it carries the !cancelled()
    # override that defeats the propagated skip.
    for job in (
        "resolve-release",
        "build-candidate",
        "publication-reachability",
        "promote-release",
        "publish-pypi",
    ):
        assert "!cancelled() &&" in _job_block(workflow, job), job

    # the four consumers of resolve-release also gate on it actually succeeding.
    for job in (
        "build-candidate",
        "publication-reachability",
        "promote-release",
        "publish-pypi",
    ):
        assert "needs.resolve-release.result == 'success'" in _job_block(workflow, job), job


def test_heavy_runner_is_limited_to_trusted_main_jobs() -> None:
    expected = "${{ github.ref == 'refs/heads/main' && vars.HEAVY_RUNNER || 'ubuntu-latest' }}"

    for name in ("documentation.yml", "release.yml", "verify.yml"):
        workflow = (REPOSITORY / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert expected in workflow
        assert "runs-on: ${{ vars.HEAVY_RUNNER || 'ubuntu-latest' }}" not in workflow

    release_workflow = (REPOSITORY / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    worker_count = "${{ github.ref == 'refs/heads/main' && vars.HEAVY_RUNNER && '4' || '1' }}"
    assert f"PLAYWRIGHT_WORKERS: {worker_count}" in release_workflow

    # RELEASING.md is the internal release custody runbook and is excluded from
    # the public export, so only assert its contents when the file is present.
    releasing_md = REPOSITORY / "RELEASING.md"
    if releasing_md.exists():
        releasing = releasing_md.read_text(encoding="utf-8")
        assert "An existing tag with no matching release creates a new draft" in releasing
