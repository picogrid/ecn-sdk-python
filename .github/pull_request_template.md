# External pull-request intake is closed

Picogrid is not accepting external pull requests. Intake will remain closed unless
and until counsel approval of the contributor workflow is separately recorded.
This template documents future requirements; it does not invite or authorize a
submission. See [CONTRIBUTING.md](https://github.com/picogrid/ecn-sdk-python/blob/main/CONTRIBUTING.md).

If intake opens, an inbound contribution proposal submitted by a contributor through
a public pull request is never merged directly. Maintainers route accepted work into
the private engineering upstream, qualify it there, and export a new public candidate
through the one-way release process. Qualified public export-candidate pull requests
created by maintainers may be merged after the required gates pass.

Before private reproduction, maintainers record the accepted proposal head SHA and
the DCO result for that head. A later head change requires a DCO rerun and an updated
export mapping before the proposal can be accepted or reproduced.

## Summary

<!--
If intake opens, explain the change and why it is needed. Do not include private or
sensitive material.
-->

## Validation

<!-- List the focused checks run and any compatibility uncertainty. -->

## Required representations

<!--
Every box must be checked by the human contributor. Passing automation does not
replace these representations.
-->

- [ ] I understand that external intake is currently closed and that this template
  does not mean my submission is accepted for review.
- [ ] I submit my contribution under the unmodified Mozilla Public License 2.0
  (`MPL-2.0`), matching the project's outbound license.
- [ ] I have read and certify each commit under the authoritative
  [Developer Certificate of Origin 1.1](https://developercertificate.org/),
  understand that the contribution and sign-off record are public, and have added
  exactly one valid `Signed-off-by: Name <email>` trailer to every commit, including
  each merge commit I created.
- [ ] Each sign-off identity matches that commit's author or committer identity, and
  I have corrected any malformed, duplicate, conflicting, or non-matching trailers
  by amending or rebasing as described in
  [CONTRIBUTING.md](https://github.com/picogrid/ecn-sdk-python/blob/main/CONTRIBUTING.md).
- [ ] I created the contribution and may submit it under `MPL-2.0`; received it
  under compatible terms that permit this submission; or received it, unmodified,
  directly from someone who certified it under DCO 1.1. I do not knowingly violate
  another person's or organization's rights.
- [ ] I have authority to contribute this work, including any approval required by
  my employer, client, contracts, or other obligations.
- [ ] I have identified all third-party code, generated content, datasets, samples,
  and other incorporated material; documented its source and license; preserved
  required notices; and confirmed that its terms permit this distribution.
- [ ] For generated work and data, I have documented the generator, input source,
  applicable licenses, material transformations, and provenance rather than
  presenting the result as wholly original.
- [ ] I take full responsibility for any AI-assisted work; I have disclosed the
  assistance and relevant source provenance and substantively reviewed the result
  for correctness, security, licensing, provenance, and compatibility.
- [ ] I disclosed known patent claims, trademark material, additional license
  terms, export controls, confidentiality duties, and other restrictions that may
  affect use or redistribution.
- [ ] This pull request contains no credentials, private keys, personal or customer
  data, confidential information, non-public operational details, sensitive traffic
  or telemetry, private security evidence, or other material I am not authorized to
  publish.
- [ ] I minimized personal information throughout the pull request, commits, tests,
  fixtures, screenshots, and logs and used synthetic data wherever possible.
- [ ] I will comply with the
  [GitHub Community Guidelines](https://docs.github.com/en/site-policy/github-terms/github-community-guidelines)
  in project interactions.
- [ ] I routed suspected vulnerabilities and sensitive security evidence privately
  according to
  [VULNERABILITY_REPORTING.md](https://github.com/picogrid/ecn-sdk-python/blob/main/VULNERABILITY_REPORTING.md),
  rather than including them here or in a public issue.
- [ ] I substantively reviewed the complete change, including generated and
  automated output, and recorded the focused checks run and any remaining
  compatibility uncertainty.
- [ ] I understand that DCO automation does not establish ownership, license
  compatibility, safety, correctness, or acceptance; maintainers may request
  evidence, removal, clean human recreation, or legal or security escalation, and
  may reject the contribution.
- [ ] I understand that the project does not routinely require a contributor
  license agreement (CLA) or copyright assignment, and the applicable contributor
  or rightsholder retains ownership. Before intake opens, counsel must confirm this
  contributor workflow. Only if counsel determines that this contribution's
  exceptional circumstances require a separate contribution agreement or software
  grant will one apply; otherwise, the standard `MPL-2.0` and DCO terms apply.
- [ ] I understand there are no bot or actor exemptions. A Dependabot,
  release-please, or other bot branch or pull request cannot merge; a qualified human
  must review and recreate the change on a separate human-authored, signed-off
  branch and pull request, preserve and disclose the upstream bot, source, license,
  and automation provenance, and close the bot branch or pull request.
