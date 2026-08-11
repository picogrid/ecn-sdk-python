// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * HTTP smoke checks for a candidate docs Worker preview.
 *
 * Deliberately plain fetch rather than Playwright: every assertion here is
 * about status, MIME, cache, and security headers, which a browser hides
 * behind its own caching and normalization. Rendered-DOM behaviour is already
 * covered by the Playwright suite against the built site.
 *
 * Expectations are imported from cloudflare/headers.ts so the Worker policy has
 * one definition rather than a copy that can drift.
 */

import { knownJourneyPathnames } from './public-url-manifest.mjs';
import { documentationBase, documentationCanonicalBase, documentationOrigin } from './site-config.mjs';

const headersModuleUrl = new URL('../cloudflare/headers.ts', import.meta.url).href;

/**
 * Unhashed brand asset: present in every build at a contract-fixed path, and
 * cached on the brand tier (a freshness window rather than per-request
 * revalidation, so the header band does not flash on route changes).
 */
const STATIC_ASSET_PATH = `${documentationBase}brand/picogrid-app-icon-192.png`;

/** A path the site must never define, so the Worker has to serve its 404 page. */
const MISSING_PATH = `${documentationBase}smoke-check-missing-page/`;

const HASHED_ASSET_PATTERN =
  /(?:href|src)="([^"]*\/_astro\/[^"/?#]+\.(?:[A-Za-z0-9_-]{5}|[A-Za-z0-9_-]{8})\.(?:css|js)(?:[?#][^"]*)?)"/;
const CANONICAL_LINK_PATTERN =
  /<link\b(?=[^>]*\srel\s*=\s*(["'])canonical\1)[^>]*\shref\s*=\s*(["'])([^"']+)\2[^>]*>/i;
const META_REFRESH_TAG_PATTERN =
  /<meta\b(?=[^>]*\bhttp-equiv\s*=\s*(["'])refresh\1)[^>]*>/i;
const CONTENT_ATTRIBUTE_PATTERN = /\bcontent\s*=\s*(["'])(.*?)\1/i;
const META_REFRESH_TARGET_PATTERN = /^\s*\d+(?:\.\d+)?\s*;\s*url\s*=\s*(.*?)\s*$/i;
const SCRIPT_REDIRECT_PATTERN =
  /\blocation\.replace\(\s*(["'])([^"']*)\1\s*\+\s*location\.search\s*\+\s*location\.hash\s*\)/g;
const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]']);
const REQUEST_TIMEOUT_MS = 15_000;
const JAVASCRIPT_MEDIA_TYPES = new Set([
  'application/ecmascript',
  'application/javascript',
  'text/ecmascript',
  'text/javascript',
]);

function rootRedirectTargets(body) {
  const targets = [];
  const refreshTag = META_REFRESH_TAG_PATTERN.exec(body)?.[0];
  const refreshContent = refreshTag
    ? CONTENT_ATTRIBUTE_PATTERN.exec(refreshTag)?.[2]
    : undefined;
  const refreshTarget = refreshContent
    ? META_REFRESH_TARGET_PATTERN.exec(refreshContent)?.[1]
    : undefined;
  if (refreshTarget) {
    targets.push(refreshTarget);
  }
  for (const match of body.matchAll(SCRIPT_REDIRECT_PATTERN)) {
    targets.push(match[2]);
  }
  return targets;
}

function contentType(response) {
  return response.headers.get('content-type') ?? '';
}

function mediaType(response) {
  return contentType(response).split(';', 1)[0].trim().toLowerCase();
}

function isHtml(response) {
  return mediaType(response) === 'text/html';
}

export function normalizeBaseUrl(value) {
  if (!value) {
    throw new Error('preview base URL is required (--base-url=<url> or PREVIEW_BASE_URL)');
  }
  const url = new URL(value);
  if (url.protocol !== 'https:' && url.protocol !== 'http:') {
    throw new Error(
      `preview base URL uses unsupported scheme ${url.protocol}; expected https: or loopback http:`,
    );
  }
  if (url.protocol === 'http:' && !LOOPBACK_HOSTS.has(url.hostname)) {
    throw new Error('preview base URL must be https for non-loopback hosts');
  }
  return url.origin;
}

/**
 * Every Worker response carries the full security header set; a missing header
 * on one route is a policy hole even if the page renders.
 */
function checkSecurityHeaders(response, label, expected, report) {
  for (const [name, value] of Object.entries(expected)) {
    const actual = response.headers.get(name);
    if (actual !== value) {
      report.fail(`${label}: ${name} is ${actual ?? '(absent)'}, expected ${value}`);
    }
  }
}

function checkCacheControl(response, label, expected, report) {
  const actual = response.headers.get('cache-control');
  if (actual !== expected) {
    report.fail(`${label}: Cache-Control is ${actual ?? '(absent)'}, expected ${expected}`);
  }
}

/**
 * The canonical host is shared with the existing documentation product, so the
 * Worker owns only the mount there. A bootstrap origin serves this Worker
 * alone. Derived rather than configured, so a cutover cannot be half-declared.
 */
export function sharedHostFor(baseUrl) {
  return normalizeBaseUrl(baseUrl) === documentationOrigin;
}

export async function runPreviewSmoke({
  baseUrl,
  fetchImpl = fetch,
  requestTimeoutMs = REQUEST_TIMEOUT_MS,
  // On the canonical host the Worker owns only the mount: everything outside it
  // belongs to the existing documentation product, so paths outside the mount
  // are not ours to assert. On a bootstrap origin the Worker owns the whole
  // host, including the root redirect document.
  sharedHost = sharedHostFor(baseUrl),
}) {
  const origin = normalizeBaseUrl(baseUrl);
  const failures = [];
  const checked = [];
  const report = { fail: (message) => failures.push(message) };

  const { CACHE_BRAND, CACHE_IMMUTABLE, CACHE_REVALIDATE, buildSecurityHeaders } =
    await import(headersModuleUrl);
  const securityHeaders = buildSecurityHeaders();

  const get = async (pathname) => {
    const url = `${origin}${pathname}`;
    checked.push(pathname);
    const controller = new AbortController();
    let timeout;
    const deadline = new Promise((_, reject) => {
      timeout = setTimeout(() => {
        const error = new Error(`${pathname}: request timed out after ${requestTimeoutMs}ms`);
        reject(error);
        controller.abort(error);
      }, requestTimeoutMs);
    });
    try {
      return await Promise.race([
        (async () => {
          const response = await fetchImpl(url, {
            redirect: 'manual',
            signal: controller.signal,
          });
          return { response, body: await response.text() };
        })(),
        deadline,
      ]);
    } finally {
      clearTimeout(timeout);
    }
  };

  // Entrypoint: the mount every external link and bookmark targets.
  const home = await get(documentationBase);
  if (home.response.status !== 200) {
    report.fail(`${documentationBase}: status ${home.response.status}, expected 200`);
  }
  if (!isHtml(home.response)) {
    report.fail(`${documentationBase}: content-type ${contentType(home.response)}, expected HTML`);
  }
  const canonicalHref = CANONICAL_LINK_PATTERN.exec(home.body)?.[3];
  if (canonicalHref !== documentationCanonicalBase) {
    report.fail(
      `${documentationBase}: canonical link ${canonicalHref ?? '(absent)'} does not equal configured canonical origin ${documentationCanonicalBase}`,
    );
  }
  checkCacheControl(home.response, documentationBase, CACHE_REVALIDATE, report);
  checkSecurityHeaders(home.response, documentationBase, securityHeaders, report);

  // Known product journeys: a preview that only serves its index is not usable.
  for (const pathname of knownJourneyPathnames.filter((path) => path !== documentationBase)) {
    const page = await get(pathname);
    if (page.response.status !== 200) {
      report.fail(`${pathname}: status ${page.response.status}, expected 200`);
    }
    if (!isHtml(page.response)) {
      report.fail(`${pathname}: content-type ${contentType(page.response)}, expected HTML`);
    }
    if (page.body === home.body) {
      report.fail(`${pathname}: served the index page instead of the journey page`);
    }
    checkCacheControl(page.response, pathname, CACHE_REVALIDATE, report);
    checkSecurityHeaders(page.response, pathname, securityHeaders, report);
  }

  // Fingerprinted asset discovered from the page that references it, so the
  // check follows the build instead of a hardcoded hash.
  const hashedAsset = HASHED_ASSET_PATTERN.exec(home.body)?.[1];
  if (!hashedAsset) {
    report.fail(`${documentationBase}: no fingerprinted /_astro/ asset referenced in HTML`);
  } else {
    let assetUrl;
    try {
      assetUrl = new URL(hashedAsset, `${origin}${documentationBase}`);
    } catch {
      report.fail(`${hashedAsset}: not a valid asset URL`);
      assetUrl = null;
    }
    if (assetUrl) {
      if (assetUrl.origin !== origin) {
        report.fail(
          `${hashedAsset}: asset origin ${assetUrl.origin} differs from preview origin ${origin}`,
        );
      } else {
        const assetPath = `${assetUrl.pathname}${assetUrl.search}`;
        const asset = await get(assetPath);
        if (asset.response.status !== 200) {
          report.fail(`${assetPath}: status ${asset.response.status}, expected 200`);
        }
        const assetType = mediaType(asset.response);
        const isStylesheet = assetUrl.pathname.endsWith('.css');
        const expectedType = isStylesheet ? 'text/css' : 'JavaScript';
        const validType = isStylesheet
          ? assetType === 'text/css'
          : JAVASCRIPT_MEDIA_TYPES.has(assetType);
        if (!validType) {
          report.fail(`${assetPath}: content-type ${contentType(asset.response)}, expected ${expectedType}`);
        }
        checkCacheControl(asset.response, assetPath, CACHE_IMMUTABLE, report);
        checkSecurityHeaders(asset.response, assetPath, securityHeaders, report);
      }
    }
  }

  // Unhashed brand asset: correct MIME, and cached on the bounded brand tier
  // rather than forever or per-request.
  const icon = await get(STATIC_ASSET_PATH);
  if (icon.response.status !== 200) {
    report.fail(`${STATIC_ASSET_PATH}: status ${icon.response.status}, expected 200`);
  }
  if (mediaType(icon.response) !== 'image/png') {
    report.fail(`${STATIC_ASSET_PATH}: content-type ${contentType(icon.response)}, expected PNG`);
  }
  checkCacheControl(icon.response, STATIC_ASSET_PATH, CACHE_BRAND, report);
  checkSecurityHeaders(icon.response, STATIC_ASSET_PATH, securityHeaders, report);

  // 404 must be the site's own page, never a rewrite to the index.
  const missing = await get(MISSING_PATH);
  if (missing.response.status !== 404) {
    report.fail(`${MISSING_PATH}: status ${missing.response.status}, expected 404`);
  }
  if (!isHtml(missing.response)) {
    report.fail(`${MISSING_PATH}: content-type ${contentType(missing.response)}, expected HTML`);
  }
  if (!missing.body.includes('Page not found')) {
    report.fail(`${MISSING_PATH}: body is not the site 404 page`);
  }
  if (missing.body === home.body) {
    report.fail(`${MISSING_PATH}: served the index page instead of the 404 page`);
  }
  checkSecurityHeaders(missing.response, MISSING_PATH, securityHeaders, report);
  checkCacheControl(missing.response, MISSING_PATH, CACHE_REVALIDATE, report);

  if (sharedHost) {
    // The bare mount is a separate zone route, and it is the form users type and
    // link. Without it the canonical host answers from the other documentation
    // product instead of redirecting into the guide.
    const bare = documentationBase.replace(/\/$/, '');
    const bareMount = await get(bare);
    if (bareMount.response.status !== 308) {
      report.fail(`${bare}: status ${bareMount.response.status}, expected 308 to ${documentationBase}`);
    }
    const location = bareMount.response.headers.get('location');
    if (location !== documentationBase && location !== `${origin}${documentationBase}`) {
      report.fail(`${bare}: redirects to ${location ?? '(absent)'}, expected ${documentationBase}`);
    }
    // This redirect is Worker-served too, so it carries the same policy as any
    // other response: a redirect that skips the headers is still a regression.
    checkCacheControl(bareMount.response, bare, CACHE_REVALIDATE, report);
    checkSecurityHeaders(bareMount.response, bare, securityHeaders, report);
  } else {
    // Root outside the mount: A1's redirect document, not a Worker error. Only
    // this Worker answers the bootstrap origin, so the root is ours to assert.
    const root = await get('/');
    if (root.response.status !== 200) {
      report.fail(`/: status ${root.response.status}, expected 200`);
    }
    if (!isHtml(root.response)) {
      report.fail(`/: content-type ${contentType(root.response)}, expected HTML`);
    }
    const redirectTargets = rootRedirectTargets(root.body);
    if (redirectTargets.length === 0) {
      report.fail(`/: redirect document has no redirect target, expected ${documentationBase}`);
    }
    for (const target of redirectTargets) {
      let resolvedTarget;
      try {
        resolvedTarget = new URL(target, `${origin}/`);
      } catch {
        report.fail(`/: redirect target ${target} is not a valid URL`);
        continue;
      }
      const expectedTarget = new URL(documentationBase, `${origin}/`);
      if (resolvedTarget.href !== expectedTarget.href) {
        report.fail(
          `/: redirect target ${target} resolves to ${resolvedTarget.href}, expected ${documentationBase}`,
        );
      }
    }
    checkCacheControl(root.response, '/', CACHE_REVALIDATE, report);
    checkSecurityHeaders(root.response, '/', securityHeaders, report);
  }

  return { failures, checked };
}
