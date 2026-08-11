// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

const repository = 'https://example.invalid/picogrid/ecn-sdk-python';
let importSequence = 0;

async function resolveVersionControl(options) {
  // Each import gets its own answer cache so a worktree transition from clean
  // to dirty is observed rather than reusing the first immutable answer.
  const module = await import(`./version-control.mjs?test=${importSequence++}`);
  return module.resolveVersionControl({ repository, ...options });
}

function git(root, ...parameters) {
  return execFileSync('git', parameters, {
    cwd: root,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  }).trim();
}

function temporaryDirectory(t) {
  const root = mkdtempSync(join(tmpdir(), 'ecn-sdk-version-control-'));
  t.after(() => rmSync(root, { force: true, recursive: true }));
  return root;
}

function writePackage(root, version = '0.1.0') {
  writeFileSync(join(root, 'package.json'), `${JSON.stringify({ version }, null, 2)}\n`);
}

function createCheckout(t, version = '0.1.0') {
  const root = temporaryDirectory(t);
  git(root, 'init', '--quiet');
  git(root, 'config', 'user.email', 'docs-tests@example.invalid');
  git(root, 'config', 'user.name', 'Documentation tests');
  writePackage(root, version);
  writeFileSync(join(root, 'guide.md'), 'released guide\n');
  git(root, 'add', 'package.json', 'guide.md');
  git(root, 'commit', '--quiet', '-m', 'fixture release');
  return { commit: git(root, 'rev-parse', 'HEAD'), root, version };
}

test('injected identity', async (t) => {
  await t.test('wins over dirty local state with or without a release tag', async (subtest) => {
    const { root, version } = createCheckout(subtest);
    writeFileSync(join(root, 'guide.md'), 'uncommitted guide\n');
    const injectedCommit = 'a'.repeat(40);

    const withoutTag = await resolveVersionControl({
      root,
      environment: { DOCS_GIT_COMMIT: injectedCommit },
    });
    assert.equal(withoutTag.commit, injectedCommit);
    assert.equal(withoutTag.tag, '');
    assert.equal(withoutTag.sourceKind, 'commit');

    const withTag = await resolveVersionControl({
      root,
      environment: {
        DOCS_GIT_COMMIT: injectedCommit,
        DOCS_GIT_TAG: `v${version}`,
      },
    });
    assert.equal(withTag.commit, injectedCommit);
    assert.equal(withTag.tag, `v${version}`);
    assert.equal(withTag.referenceLabel, `v${version}`);
  });

  await t.test('rejects a tag without a commit', async (subtest) => {
    const root = temporaryDirectory(subtest);
    writePackage(root);
    await assert.rejects(
      resolveVersionControl({ root, environment: { DOCS_GIT_TAG: 'v0.1.0' } }),
      /DOCS_GIT_TAG requires DOCS_GIT_COMMIT/,
    );
  });

  await t.test('rejects a tag that does not name the package version', async (subtest) => {
    const root = temporaryDirectory(subtest);
    writePackage(root);
    await assert.rejects(
      resolveVersionControl({
        root,
        environment: {
          DOCS_GIT_COMMIT: 'b'.repeat(40),
          DOCS_GIT_TAG: 'v9.9.9',
        },
      }),
      /tag v9\.9\.9 does not name the released version v0\.1\.0/,
    );
  });

  await t.test('rejects a malformed commit', async (subtest) => {
    const root = temporaryDirectory(subtest);
    writePackage(root);
    await assert.rejects(
      resolveVersionControl({ root, environment: { DOCS_GIT_COMMIT: 'abc123' } }),
      /full 40-character hexadecimal hash/,
    );
  });
});

test('local Git identity', async (t) => {
  await t.test('ignores an unrelated tag pointing at a clean HEAD', async (subtest) => {
    const { commit, root } = createCheckout(subtest);
    git(root, 'tag', 'docs-preview');

    const identity = await resolveVersionControl({ root, environment: {} });
    assert.equal(identity.commit, commit);
    assert.equal(identity.tag, '');
    assert.equal(identity.referenceLabel, commit.slice(0, 7));
    assert.equal(identity.sourceKind, 'commit');
  });

  await t.test('selects the release tag when another tag also points at HEAD', async (subtest) => {
    const { root, version } = createCheckout(subtest);
    git(root, 'tag', `v${version}`);
    git(root, 'tag', 'docs-preview');

    const identity = await resolveVersionControl({ root, environment: {} });
    assert.equal(identity.tag, `v${version}`);
    assert.equal(identity.referenceLabel, `v${version}`);
  });

  await t.test('forfeits the commit claim for tracked and untracked changes', async (subtest) => {
    const tracked = createCheckout(subtest);
    const cleanIdentity = await resolveVersionControl({ root: tracked.root, environment: {} });
    assert.equal(cleanIdentity.commit, tracked.commit);
    assert.equal(cleanIdentity.sourceKind, 'commit');

    writeFileSync(join(tracked.root, 'guide.md'), 'changed after commit\n');
    const trackedDirty = await resolveVersionControl({ root: tracked.root, environment: {} });
    assert.equal(trackedDirty.commit, '');
    assert.equal(trackedDirty.tag, '');
    assert.equal(trackedDirty.href, `${repository}/tree/main`);
    assert.equal(trackedDirty.sourceKind, 'branch');

    const untracked = createCheckout(subtest);
    writeFileSync(join(untracked.root, 'new-page.md'), 'not in HEAD\n');
    const untrackedDirty = await resolveVersionControl({ root: untracked.root, environment: {} });
    assert.equal(untrackedDirty.commit, '');
    assert.equal(untrackedDirty.tag, '');
    assert.equal(untrackedDirty.href, `${repository}/tree/main`);
    assert.equal(untrackedDirty.sourceKind, 'branch');
  });

  await t.test('claims no identity below the checkout root', async (subtest) => {
    const { root } = createCheckout(subtest);
    const nested = join(root, 'copied-source');
    mkdirSync(nested);
    writePackage(nested);

    const identity = await resolveVersionControl({ root: nested, environment: {} });
    assert.equal(identity.commit, '');
    assert.equal(identity.tag, '');
    assert.equal(identity.sourceKind, 'branch');
  });
});
