// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

/** Shared docs mount: /ecn-sdk/ under docs.picogrid.com (canonical) and Pages. */

export const documentationHost = 'docs.picogrid.com';
export const documentationOrigin = `https://${documentationHost}`;
export const documentationPagesHost = 'stunning-barnacle-ee45l85.pages.github.io';
export const documentationPagesOrigin = `https://${documentationPagesHost}`;
export const documentationBasePath = '/ecn-sdk';
export const documentationBase = `${documentationBasePath}/`;
export const documentationSite = documentationOrigin;
export const documentationCanonicalBase = `${documentationOrigin}${documentationBase}`;
export const documentationPagesCanonicalBase = `${documentationPagesOrigin}${documentationBase}`;
export const assetsOutputDirectory = 'site-dist';
export const siteContentDirectory = `${assetsOutputDirectory}${documentationBasePath}`;

// Absolute locations, so every consumer resolves them the same way from any
// working directory. `resolve` drops the trailing separator `fileURLToPath`
// leaves on a directory URL, keeping these ordinary directory paths that a
// consumer can compare and prefix-test.
/** Absolute path of the repository root (this module lives at <root>/docs/site/). */
export const repositoryRoot = resolve(fileURLToPath(new URL('../../', import.meta.url)));
/** Absolute path of the repo-root build output directory, `<root>/site-dist`. */
export const assetsOutputRoot = join(repositoryRoot, assetsOutputDirectory);
/** Absolute path of the built documentation tree, `<root>/site-dist/ecn-sdk`. */
export const siteContentRoot = join(repositoryRoot, siteContentDirectory);
/** Absolute path of the docs workspace root, `<root>/docs`. */
export const documentationWorkspaceRoot = resolve(fileURLToPath(new URL('../', import.meta.url)));
export const publicMountPath = documentationBasePath;
