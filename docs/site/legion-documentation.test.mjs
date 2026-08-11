// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import assert from 'node:assert/strict';
import test from 'node:test';

import { resolveLegionDocumentation } from './legion-documentation.mjs';

function resolve(environment) {
  return resolveLegionDocumentation({ environment });
}

const defaultTarget = {
  href: 'https://docs.picogrid.com/reference/start',
  label: 'Legion API',
  origin: 'https://docs.picogrid.com',
  title: 'Legion API documentation',
  version: '',
};

test('uses the default target when LEGION_DOCS_URL is absent', () => {
  assert.deepEqual(resolve({}), defaultTarget);
});

test('uses the default target when LEGION_DOCS_URL is empty or whitespace', () => {
  assert.deepEqual(resolve({ LEGION_DOCS_URL: '' }), defaultTarget);
  assert.deepEqual(resolve({ LEGION_DOCS_URL: '  \t  ' }), defaultTarget);
});

test('accepts explicit targets under each documentation root', () => {
  for (const root of ['docs', 'guides', 'reference']) {
    const href = `https://docs.picogrid.com/${root}/start`;
    assert.equal(resolve({ LEGION_DOCS_URL: href }).href, href);
  }
});

test('rejects a backslash before URL can normalize it into a slash', () => {
  const target = String.raw`https://docs.picogrid.com/reference\start`;
  assert.equal(new URL(target).pathname, '/reference/start');
  assert.throws(
    () => resolve({ LEGION_DOCS_URL: target }),
    /the Legion documentation target must use forward-slash path separators/,
  );
});

test('rejects a backslash outside a dot segment rather than publishing its normalized path', () => {
  const target = String.raw`https://docs.picogrid.com/guides\operator\start`;
  assert.equal(new URL(target).pathname, '/guides/operator/start');
  assert.throws(
    () => resolve({ LEGION_DOCS_URL: target }),
    /the Legion documentation target must use forward-slash path separators/,
  );
});

test('rejects a percent-encoded backslash, which survives parsing as a separator', () => {
  // `URL` keeps `%5c` in the path rather than normalizing it, so this one reaches
  // `isDocumentationPath` intact and would be published as an escaped separator.
  const target = 'https://docs.picogrid.com/reference%5cstart';
  assert.equal(new URL(target).pathname, '/reference%5cstart');
  assert.throws(
    () => resolve({ LEGION_DOCS_URL: target }),
    /the Legion documentation target must use forward-slash path separators/,
  );
});

test('rejects plain and percent-encoded traversal segments', () => {
  assert.throws(
    () => resolve({ LEGION_DOCS_URL: 'https://docs.picogrid.com/reference/../guides/start' }),
    /the Legion documentation target must carry no traversal segment/,
  );
  assert.throws(
    () => resolve({ LEGION_DOCS_URL: 'https://docs.picogrid.com/reference/%2e%2e/guides/start' }),
    /the Legion documentation target must carry no traversal segment/,
  );
});

test('rejects a target on a non-approved origin', () => {
  assert.throws(
    () => resolve({ LEGION_DOCS_URL: 'https://example.com/reference/start' }),
    /the Legion documentation target must be published on https:\/\/docs\.picogrid\.com/,
  );
});

test('rejects a target carrying credentials', () => {
  // Applied through the URL API rather than written inline: a credential-bearing
  // URL literal on an approved host is itself a publication-input violation, and
  // the release scan refuses the file carrying it.
  const credentialed = new URL('https://docs.picogrid.com/reference/start');
  credentialed.username = 'reader';
  credentialed.password = 'secret';
  assert.throws(
    () => resolve({ LEGION_DOCS_URL: credentialed.href }),
    /the Legion documentation target must carry no credential, query, or fragment/,
  );
});

test('rejects a target carrying a query', () => {
  assert.throws(
    () => resolve({ LEGION_DOCS_URL: 'https://docs.picogrid.com/reference/start?version=1' }),
    /the Legion documentation target must carry no credential, query, or fragment/,
  );
});

test('rejects a target carrying a fragment', () => {
  assert.throws(
    () => resolve({ LEGION_DOCS_URL: 'https://docs.picogrid.com/reference/start#examples' }),
    /the Legion documentation target must carry no credential, query, or fragment/,
  );
});

test('rejects bare query and fragment delimiters', () => {
  assert.throws(
    () => resolve({ LEGION_DOCS_URL: 'https://docs.picogrid.com/reference/start?' }),
    /the Legion documentation target must carry no credential, query, or fragment/,
  );
  assert.throws(
    () => resolve({ LEGION_DOCS_URL: 'https://docs.picogrid.com/reference/start#' }),
    /the Legion documentation target must carry no credential, query, or fragment/,
  );
});

test('rejects a path outside the documentation roots', () => {
  assert.throws(
    () => resolve({ LEGION_DOCS_URL: 'https://docs.picogrid.com/api/start' }),
    /the Legion documentation target must address a published documentation path \(docs, guides, reference\)/,
  );
});

test('rejects an empty path segment', () => {
  assert.throws(
    () => resolve({ LEGION_DOCS_URL: 'https://docs.picogrid.com/reference//start' }),
    /the Legion documentation target must address a published documentation path \(docs, guides, reference\)/,
  );
});

test('rejects a relative target', () => {
  assert.throws(
    () => resolve({ LEGION_DOCS_URL: '/reference/start' }),
    /the Legion documentation target must be an absolute URL/,
  );
});

test('reflects an accepted Legion version in the title', () => {
  const href = 'https://docs.picogrid.com/reference/v2/start';
  assert.deepEqual(resolve({ LEGION_DOCS_URL: href, LEGION_DOCS_VERSION: 'v2.4 LTS' }), {
    ...defaultTarget,
    href,
    title: 'Legion API documentation for v2.4 LTS',
    version: 'v2.4 LTS',
  });
});

test('rejects a version without the target it claims to document', () => {
  // The default target is not asserted to document any particular Legion release,
  // so a version on its own would publish a claim about a page nobody chose for
  // it. Which target documents which release is a fact about Legion's own site.
  assert.throws(
    () => resolve({ LEGION_DOCS_VERSION: 'v2.4 LTS' }),
    /LEGION_DOCS_VERSION requires LEGION_DOCS_URL/,
  );
});

test('rejects a malformed Legion version label', () => {
  assert.throws(
    () => resolve({ LEGION_DOCS_VERSION: 'v2/preview' }),
    /the Legion documentation version must be an ordinary version label/,
  );
});
