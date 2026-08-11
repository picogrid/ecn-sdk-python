// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * Resolve the version-control identity that the published guide advertises.
 *
 * The release verifier builds the site twice from an immutable source snapshot
 * that deliberately excludes `.git`, and requires the two trees to be identical
 * byte for byte. Git therefore cannot be consulted during a verified build: the
 * verifier reads the commit once from the real repository and injects it into
 * both builds through `DOCS_GIT_COMMIT` and `DOCS_GIT_TAG`. Reading Git locally
 * is only a convenience for working copies and pull-request builds, where no
 * value is injected.
 *
 * The same resolution runs in the build and in the built-site checkers so that
 * published markup can be asserted against exactly one expected identity.
 */

import { execFileSync } from 'node:child_process';
import { readFileSync, realpathSync } from 'node:fs';
import { resolve } from 'node:path';

import { documentationWorkspaceRoot, repositoryRoot } from './site-config.mjs';

const commitPattern = /^[0-9a-f]{40}$/;
const tagPattern = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

function git(parameters, root, failure = '') {
  try {
    return execFileSync('git', parameters, {
      cwd: root,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
      timeout: 5_000,
    }).trim();
  } catch {
    return failure;
  }
}

/**
 * Only trust a checkout whose own root is the documentation project. Without
 * this the build would silently inherit the identity of an unrelated enclosing
 * repository when the source tree is copied somewhere else.
 */
function checkoutRoot(root) {
  const toplevel = git(['rev-parse', '--show-toplevel'], root);
  if (!toplevel) return '';
  try {
    return realpathSync(toplevel) === realpathSync(root) ? toplevel : '';
  } catch {
    return '';
  }
}


function readIdentity(root, environment, releaseTag) {
  const injectedCommit = (environment.DOCS_GIT_COMMIT ?? '').trim();
  const injectedTag = (environment.DOCS_GIT_TAG ?? '').trim();
  if (injectedTag && !injectedCommit) {
    throw new Error('DOCS_GIT_TAG requires DOCS_GIT_COMMIT so the tag can be bound to a commit');
  }
  if (injectedCommit) return { commit: injectedCommit, tag: injectedTag };
  if (!checkoutRoot(root)) return { commit: '', tag: '' };
  // A local preview remains usable with edits, but HEAD cannot identify
  // rendered bytes containing uncommitted changes. A failed status probe is
  // likewise not proof that the checkout is clean.
  if (git(['status', '--porcelain'], root, null) !== '') return { commit: '', tag: '' };
  // Only the package's release tag is meaningful here; unrelated labels on
  // the same commit neither become the reader-facing identity nor hide it.
  const tags = git(['tag', '--points-at', 'HEAD'], root).split('\n').filter(Boolean);
  return {
    commit: git(['rev-parse', 'HEAD'], root),
    tag: tags.includes(releaseTag) ? releaseTag : '',
  };
}

function sourceLink(repository, commit) {
  if (commit) return { href: `${repository}/commit/${commit}`, sourceKind: 'commit' };
  return { href: `${repository}/tree/main`, sourceKind: 'branch' };
}

function readVersionControl(repository, root, packageRoot, environment) {
  const version = JSON.parse(readFileSync(resolve(packageRoot, 'package.json'), 'utf8')).version;

  const { commit, tag } = readIdentity(root, environment, `v${version}`);
  if (commit && !commitPattern.test(commit)) {
    throw new Error('the documentation commit must be a full 40-character hexadecimal hash');
  }
  if (tag && !tagPattern.test(tag)) {
    throw new Error('the documentation tag must be an ordinary Git tag name');
  }
  // A tag names this release or it names nothing. Without this the guide would
  // publish whatever label a caller injected, and the built-site and
  // external-link checkers, which resolve the same environment, would agree
  // with it.
  if (tag && tag !== `v${version}`) {
    throw new Error(
      `the documentation tag ${tag} does not name the released version v${version}`,
    );
  }

  const shortCommit = commit ? commit.slice(0, 7) : '';
  const reference = commit || 'main';
  const { href, sourceKind } = sourceLink(repository, commit);

  return Object.freeze({
    commit,
    href,
    reference,
    // What the reader sees and can look up in the repository.
    referenceLabel: tag || shortCommit || 'main',
    shortCommit,
    sourceKind,
    tag,
    version,
    versionLabel: `v${version}`,
  });
}

/**
 * Every page renders the header, the version menu, and the footer, so a build
 * asks this question hundreds of times. Neither the checkout nor `package.json`
 * changes while a build runs, so each distinct question is answered once and the
 * frozen answer is shared. This also keeps the two builds the release verifier
 * compares from differing over a mid-build change to the working tree.
 */
const answers = new Map();

/**
 * Where to look for the checkout, and for the package that names the version,
 * when the caller does not say.
 *
 * Astro bundles this module into its build output, so `import.meta.url` — and
 * therefore the roots site-config derives from it — names the bundle rather than
 * this file's place in the checkout. Every documentation command, including the
 * build, runs with the documentation workspace as its working directory, so that
 * is what locates the checkout once the module's own location no longer does.
 * A wrong answer stays fail-closed: the checkout guard above rejects it and the
 * build publishes no source identity rather than a borrowed one.
 */
let defaultRootsAnswer;
function defaultRoots() {
  if (!defaultRootsAnswer) {
    if (checkoutRoot(repositoryRoot)) {
      defaultRootsAnswer = { root: repositoryRoot, packageRoot: documentationWorkspaceRoot };
    } else {
      const packageRoot = resolve(process.cwd());
      defaultRootsAnswer = { root: resolve(packageRoot, '..'), packageRoot };
    }
  }
  return defaultRootsAnswer;
}

/**
 * @param {{
 *   repository: string,
 *   root?: string,
 *   packageRoot?: string,
 *   environment?: Record<string, string | undefined>,
 * }} options
 */
export function resolveVersionControl({
  repository,
  root: requestedRoot,
  packageRoot: requestedPackageRoot,
  environment = process.env,
}) {
  if (!repository) throw new Error('a repository URL is required to resolve version control');
  const defaults = defaultRoots();
  const root = requestedRoot ?? defaults.root;
  // A caller naming its own checkout names a self-contained one, whose package
  // sits at its root.
  const packageRoot = requestedPackageRoot
    ?? (requestedRoot === undefined ? defaults.packageRoot : requestedRoot);
  // Only the injected identity is read from the environment, so the rest of it
  // cannot change the answer and does not belong in the key.
  const key = JSON.stringify([
    repository,
    resolve(root),
    resolve(packageRoot),
    environment.DOCS_GIT_COMMIT ?? '',
    environment.DOCS_GIT_TAG ?? '',
  ]);
  const answer = answers.get(key)
    ?? readVersionControl(repository, root, packageRoot, environment);
  answers.set(key, answer);
  return answer;
}
