// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * Resolve the Legion API documentation target that the guide header points at.
 *
 * Legion's REST surface is documented on its own site, and that site is
 * versioned independently of this client. A guide built for one Legion version
 * must be able to send readers to that version's reference, so the destination
 * is a build input rather than a constant compiled into the header component.
 *
 * The value is injected, not discovered, for the same reason the version-control
 * identity is: the release verifier builds the site twice from an immutable
 * source snapshot and requires the two trees to be identical byte for byte, so
 * anything the build reads must come from the environment that both builds
 * share. `LEGION_DOCS_URL` selects the destination and `LEGION_DOCS_VERSION`
 * names the Legion version it documents.
 *
 * Resolution fails closed. Only the reviewed anonymous documentation origin is
 * admissible, and the path must be an ordinary documentation path carrying no
 * credential, query, or fragment, so a build cannot send readers off the
 * reviewed host or attach a token to a link the guide publishes. The same
 * resolution runs in the build and in the built-site and external-link checkers
 * so the published header can be asserted against exactly one expected target.
 */

const approvedOrigin = 'https://docs.picogrid.com';
const defaultPath = '/reference/start';
const label = 'Legion API';
const segmentPattern = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
// Reference material lives under one of these roots. An allowlist rather than a
// list of authentication routes to refuse: a blacklist admits every endpoint
// nobody thought of, and the decision requires this to fail closed.
const documentationRoots = new Set(['docs', 'guides', 'reference']);
const versionPattern = /^[A-Za-z0-9][A-Za-z0-9. _-]{0,31}$/;
// A dot segment, single or double, literal or percent-encoded. `URL` normalizes
// these away, so the raw target has to be refused before it is parsed or the
// published path is not the path a maintainer wrote.
const traversalPattern = /(?:^|[\\/])(?:\.|%2e)(?:\.|%2e)?(?=[\\/?#]|$)/i;
// A backslash path separator is normalized into `/` by `URL`, while its
// percent-encoded spelling is ambiguous in the same way. Refuse both raw forms
// so the published path is exactly the path a maintainer reviewed.
const backslashPattern = /\\|%5c/i;

/** An absolute path under a documentation root, with no empty segment. */
function isDocumentationPath(pathname) {
  const segments = pathname.split('/');
  if (segments.shift() !== '') return false;
  if (segments.at(-1) === '') segments.pop();
  if (segments.length === 0 || !documentationRoots.has(segments[0])) return false;
  return segments.every((segment) => segmentPattern.test(segment));
}

function approvedTarget(value) {
  if (traversalPattern.test(value)) {
    throw new Error('the Legion documentation target must carry no traversal segment');
  }
  if (backslashPattern.test(value)) {
    throw new Error('the Legion documentation target must use forward-slash path separators');
  }
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error('the Legion documentation target must be an absolute URL');
  }
  if (url.origin !== approvedOrigin) {
    throw new Error(`the Legion documentation target must be published on ${approvedOrigin}`);
  }
  // `url.search` and `url.hash` are empty for a target ending in a bare `?` or
  // `#`, so read the raw value: the delimiter is what is refused, not only what
  // follows it, and `url.href` would otherwise publish a non-canonical link.
  if (url.username || url.password || value.includes('?') || value.includes('#')) {
    throw new Error('the Legion documentation target must carry no credential, query, or fragment');
  }
  if (!isDocumentationPath(url.pathname)) {
    throw new Error(
      `the Legion documentation target must address a published documentation path (${[...documentationRoots].join(', ')})`,
    );
  }
  return url.href;
}

/**
 * @param {{ environment?: Record<string, string | undefined> }} [options]
 */
export function resolveLegionDocumentation({ environment = process.env } = {}) {
  const injectedTarget = (environment.LEGION_DOCS_URL ?? '').trim();
  const version = (environment.LEGION_DOCS_VERSION ?? '').trim();
  if (version && !versionPattern.test(version)) {
    throw new Error('the Legion documentation version must be an ordinary version label');
  }
  // The version is what the header tells a reader the linked page documents. The
  // default target is not asserted to document any particular Legion release, so
  // a version without an explicit target would publish a claim about a page
  // nobody chose for it. Which target documents which Legion release is a fact
  // about Legion's own site, so the build states both or neither.
  if (version && !injectedTarget) {
    throw new Error(
      'LEGION_DOCS_VERSION requires LEGION_DOCS_URL so the version names the target it documents',
    );
  }

  return {
    href: approvedTarget(injectedTarget || `${approvedOrigin}${defaultPath}`),
    label,
    origin: approvedOrigin,
    // What a reader sees on hover, so a versioned build says which Legion
    // release the reference describes rather than only naming the product.
    title: version ? `Legion API documentation for ${version}` : 'Legion API documentation',
    version,
  };
}
