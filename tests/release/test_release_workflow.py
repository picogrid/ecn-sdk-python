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


def _step_block(workflow: str, step: str) -> str:
    marker = f"      - name: {step}\n"
    start = workflow.index(marker) + len(marker)
    rest = workflow[start:]
    nxt = re.search(r"\n      - name:", rest)
    return rest[: nxt.start()] if nxt else rest


def test_dco_workflow_runs_only_in_public_distribution_repository() -> None:
    workflow = (REPOSITORY / ".github" / "workflows" / "dco.yml").read_text(encoding="utf-8")
    dco_job = _job_block(workflow, "dco")

    assert "    if: ${{ github.repository == 'picogrid/ecn-sdk-python' }}\n" in dco_job
    assert "github.actor" not in workflow
    assert "github.triggering_actor" not in workflow
    assert "author_association" not in workflow
    assert "organization" not in workflow
    assert "membership" not in workflow


def test_dco_workflow_executes_only_the_trusted_base_checker() -> None:
    workflow = (REPOSITORY / ".github" / "workflows" / "dco.yml").read_text(encoding="utf-8")
    checkout = _step_block(workflow, "Check out the trusted base history")
    fetch = _step_block(workflow, "Fetch the immutable head history as data")
    checker_execution = _step_block(workflow, "Check DCO sign-offs")

    assert workflow.count("uses: actions/checkout@") == 1
    assert "ref: ${{ env.DCO_BASE }}" in checkout
    assert "ref: ${{ env.DCO_HEAD }}" not in checkout
    assert "path: repository" in checkout
    assert "fetch-depth: 0" in checkout
    assert "persist-credentials: false" in checkout
    assert "trusted-base" not in workflow
    assert "working-directory: repository" in fetch
    assert 'git fetch --no-tags origin "$DCO_HEAD"' in fetch
    assert "working-directory: repository" in checker_execution
    assert "python scripts/check_dco.py" in checker_execution
    assert "make check-dco" not in checker_execution


def test_dco_workflow_rejects_bot_pull_request_events_in_the_named_step() -> None:
    workflow = (REPOSITORY / ".github" / "workflows" / "dco.yml").read_text(encoding="utf-8")
    rejection_step = _step_block(workflow, "Reject bot-authored pull requests")

    assert "github.event.pull_request.user.type == 'Bot'" in rejection_step
    assert "exit 1" in rejection_step


def test_dco_workflow_uses_only_required_workflow_compatible_triggers() -> None:
    workflow = (REPOSITORY / ".github" / "workflows" / "dco.yml").read_text(encoding="utf-8")

    assert "\n  pull_request:\n" in workflow
    assert "\n  merge_group:\n" in workflow
    assert "pull_request_target" not in workflow
    assert "workflow_dispatch" not in workflow
    assert "inputs." not in workflow
    assert "zizmor: ignore[dangerous-triggers]" not in workflow


def test_dco_workflow_uses_only_event_derived_commit_ranges() -> None:
    workflow = (REPOSITORY / ".github" / "workflows" / "dco.yml").read_text(encoding="utf-8")

    assert (
        "${{ github.event_name == 'pull_request' && github.event.pull_request.base.sha || github.event.merge_group.base_sha }}"
        in workflow
    )
    assert (
        "${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.event.merge_group.head_sha }}"
        in workflow
    )
    assert "github.sha" not in workflow
    assert (
        "${{ github.event_name == 'merge_group' && github.event.merge_group.head_sha || '' }}"
        in workflow
    )
    rejection_step = _step_block(workflow, "Reject bot-authored pull requests")
    assert "github.event_name == 'pull_request'" in rejection_step
    assert "pull_request_target" not in rejection_step


def test_pull_request_template_uses_repository_qualified_public_links() -> None:
    template = (REPOSITORY / ".github" / "pull_request_template.md").read_text(encoding="utf-8")

    contributing_url = "https://github.com/picogrid/ecn-sdk-python/blob/main/CONTRIBUTING.md"
    vulnerability_url = (
        "https://github.com/picogrid/ecn-sdk-python/blob/main/VULNERABILITY_REPORTING.md"
    )
    assert template.count(f"[CONTRIBUTING.md]({contributing_url})") == 2
    assert template.count(f"[VULNERABILITY_REPORTING.md]({vulnerability_url})") == 1


def test_public_pull_requests_are_proposals_routed_through_private_upstream(
    tmp_path: Path,
) -> None:
    public_repository = tmp_path / "public"
    public_repository.mkdir()
    for relative in ("CONTRIBUTING.md", ".github/pull_request_template.md"):
        source = REPOSITORY / relative
        destination = public_repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())

    assert not (public_repository / "DECISIONS.md").exists()
    contributing = (public_repository / "CONTRIBUTING.md").read_text(encoding="utf-8")
    template = (public_repository / ".github" / "pull_request_template.md").read_text(
        encoding="utf-8"
    )

    for document in (contributing, template):
        normalized = " ".join(document.split()).casefold()
        assert "inbound contribution proposal" in normalized
        assert "never merged directly" in normalized
        assert "private engineering upstream" in normalized
        assert "new public candidate" in normalized
        assert "qualified public export-candidate pull requests" in normalized
        assert "may be merged" in normalized
        assert "does not routinely require a contributor license agreement (cla)" in normalized
        assert "copyright assignment" in normalized
        assert "applicable contributor or rightsholder retains ownership" in normalized

    normalized_contributing = " ".join(contributing.split()).casefold()
    assert "normal public promotion path" in normalized_contributing


def test_accepted_proposal_evidence_is_bound_to_the_reviewed_head() -> None:
    for relative in ("CONTRIBUTING.md", ".github/pull_request_template.md"):
        document = (REPOSITORY / relative).read_text(encoding="utf-8")
        normalized = " ".join(document.split()).casefold()
        assert "accepted proposal head sha" in normalized
        assert "dco result" in normalized
        assert "before private reproduction" in normalized
        assert "later head change" in normalized
        assert "rerun" in normalized
        assert "updated export mapping" in normalized


def test_contributor_policy_names_counsel_and_prospective_ruleset_gates() -> None:
    contributing = (REPOSITORY / "CONTRIBUTING.md").read_text(encoding="utf-8")
    intake = contributing.split("### Intake status", 1)[1].split(
        "### Developer Certificate of Origin 1.1", 1
    )[0]
    normalized = " ".join(intake.split()).casefold()

    assert "counsel approval" in normalized
    assert ".github/workflows/dco.yml" in normalized
    assert "trusted public `main`" in normalized
    assert "pull-request and merge-group head shas" in normalized
    assert "hosted evidence" in normalized


def test_contributor_policy_requires_both_non_bypassable_intake_checks() -> None:
    contributing = (REPOSITORY / "CONTRIBUTING.md").read_text(encoding="utf-8")
    intake = contributing.split("### Intake status", 1)[1].split(
        "### Developer Certificate of Origin 1.1", 1
    )[0]
    normalized = " ".join(intake.split()).casefold()

    assert ".github/workflows/dco.yml" in normalized
    assert "trusted public `main`" in normalized
    assert "pull-request and merge-group head shas" in normalized
    assert "license-policy checker" in normalized
    assert "loaded and executed from trusted public `main` against the proposed tree" in normalized
    assert "pull-request heads" in normalized
    assert "merge-group heads if merge queue is enabled" in normalized
    assert "no general bypass" in normalized
    assert "ruleset configuration and hosted proof are pending" in normalized


def test_contributor_policy_calls_the_event_user_the_pull_request_author() -> None:
    contributing = (REPOSITORY / "CONTRIBUTING.md").read_text(encoding="utf-8")

    for document in (contributing,):
        normalized = " ".join(document.split()).casefold()
        assert "pull request author is a bot" in normalized
        assert "event actor is a bot" not in normalized


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


def test_heavy_runner_covers_private_builds_and_trusted_public_main_jobs() -> None:
    expected = "${{ (github.repository != 'picogrid/ecn-sdk-python' || github.ref == 'refs/heads/main') && vars.HEAVY_RUNNER || 'ubuntu-latest' }}"

    for name in ("documentation.yml", "release.yml", "verify.yml"):
        workflow = (REPOSITORY / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert expected in workflow
        assert "runs-on: ${{ vars.HEAVY_RUNNER || 'ubuntu-latest' }}" not in workflow

    release_workflow = (REPOSITORY / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    worker_count = "${{ (github.repository != 'picogrid/ecn-sdk-python' || github.ref == 'refs/heads/main') && vars.HEAVY_RUNNER && '4' || '1' }}"
    assert f"PLAYWRIGHT_WORKERS: {worker_count}" in release_workflow
    assert f"VERIFY_RELEASE_JOBS: {worker_count}" in release_workflow

    verify_workflow = (REPOSITORY / ".github" / "workflows" / "verify.yml").read_text(
        encoding="utf-8"
    )
    assert f"VERIFY_RELEASE_JOBS: {worker_count}" in verify_workflow

    # RELEASING.md is the internal release custody runbook and is excluded from
    # the public export, so only assert its contents when the file is present.
    releasing_md = REPOSITORY / "RELEASING.md"
    if releasing_md.exists():
        releasing = releasing_md.read_text(encoding="utf-8")
        assert "An existing tag with no matching release creates a new draft" in releasing
