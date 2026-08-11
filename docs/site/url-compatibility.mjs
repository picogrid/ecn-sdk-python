// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * URL compatibility gate (A7).
 *
 * Every path in the public URL manifest must answer successfully on a candidate
 * origin (preview Worker, local wrangler, or production). Intentional removals
 * are not silent 404s — list them under `publicRedirects` with the expected
 * Location path, and this gate asserts the redirect instead of 200-OK.
 *
 * Disk presence of the same paths remains A3 (`check-deploy-contract.mjs`).
 * This module is the network-facing half.
 */
import {
  knownJourneyPathnames,
  publicStaticPathnames,
  publicUrlPathnames,
} from './public-url-manifest.mjs';
import { documentationBasePath } from './site-config.mjs';

const REQUEST_TIMEOUT_MS = 12_000;
const USER_AGENT = 'Picogrid-ECN-SDK-url-compatibility/1.0';
const DEFAULT_CONCURRENCY = 8;
// The redirect statuses a declared move may use; the manifest contract and the
// live check accept exactly this set.
const PERMITTED_REDIRECT_STATUSES = Object.freeze([301, 302, 303, 307, 308]);

/**
 * Former public paths that must redirect rather than disappear.
 * Values are absolute pathnames under the docs host (usually still under
 * `/ecn-sdk/`).
 *
 * `404.html` is here because Cloudflare Assets serves HTML extensionless: the
 * default `html_handling` redirects `<page>.html` to `<page>`, so the Worker
 * answers this path with a 307 to `/ecn-sdk/404`, which serves the same
 * document. The URL still resolves, which is what this gate exists to protect.
 * Turning that off is not an option worth taking — `html_handling: "none"` also
 * disables index resolution, so every directory URL on the site would stop
 * working to keep one extension intact.
 *
 * @type {Readonly<Record<string, { location: string, status?: number }>>}
 */
export const publicRedirects = Object.freeze({
  [`${documentationBasePath}/404.html`]: {
    location: `${documentationBasePath}/404`,
    status: 307,
  },
});

export function normalizeCompatibilityBaseUrl(value) {
  if (!value) {
    throw new Error('compatibility base URL is required (--base-url=<url> or PREVIEW_BASE_URL)');
  }
  const url = new URL(value);
  const isLocalHttp = (
    url.protocol === 'http:'
    && (url.hostname === 'localhost' || url.hostname === '127.0.0.1')
  );
  if (url.protocol !== 'https:' && !isLocalHttp) {
    throw new Error(`compatibility base URL must be https (got ${url.protocol})`);
  }
  return url.origin;
}

/** Ordered unique list the live gate checks for success (not redirects). */
export function liveManifestPathnames({
  pathnames = publicUrlPathnames,
  redirects = publicRedirects,
} = {}) {
  return pathnames.filter((pathname) => !(pathname in redirects));
}

export function redirectManifestEntries(redirects = publicRedirects) {
  return Object.entries(redirects).sort(([a], [b]) => a.localeCompare(b));
}

/**
 * Defensive invariants on the published contract before any network I/O.
 * Pure — unit-tested — so a bad manifest fails before CI pays for a crawl.
 */
export function validateManifestContract({
  pathnames = publicUrlPathnames,
  redirects = publicRedirects,
  mount = documentationBasePath,
  // Full journey/static coverage only when checking the committed public set.
  journeys = pathnames === publicUrlPathnames ? knownJourneyPathnames : [],
  staticAssets = pathnames === publicUrlPathnames ? publicStaticPathnames : [],
} = {}) {
  const failures = [];
  const seen = new Set();

  for (const pathname of pathnames) {
    if (seen.has(pathname)) {
      failures.push(`duplicate public URL pathname: ${pathname}`);
    }
    seen.add(pathname);
    let normalized;
    try {
      normalized = new URL(pathname, 'https://example.invalid').pathname;
    } catch {
      failures.push(`public URL pathname is not a valid path: ${pathname}`);
      continue;
    }
    if (normalized !== pathname) {
      failures.push(
        `public URL pathname is not canonical after URL normalization: ${pathname} → ${normalized}`,
      );
      continue;
    }
    if (
      pathname !== mount
      && pathname !== `${mount}/`
      && !pathname.startsWith(`${mount}/`)
    ) {
      failures.push(`public URL pathname escapes ${mount}/: ${pathname}`);
    }
  }

  for (const journey of journeys) {
    if (!pathnames.includes(journey) && !(journey in redirects)) {
      failures.push(`known journey missing from public URL manifest: ${journey}`);
    }
  }

  for (const asset of staticAssets) {
    if (!pathnames.includes(asset) && !(asset in redirects)) {
      failures.push(`static public asset missing from public URL manifest: ${asset}`);
    }
  }

  for (const [from, rule] of Object.entries(redirects)) {
    let normalizedFrom;
    try {
      normalizedFrom = new URL(from, 'https://example.invalid').pathname;
    } catch {
      failures.push(`redirect source is not a valid path: ${from}`);
      continue;
    }
    if (normalizedFrom !== from) {
      failures.push(
        `redirect source is not canonical after URL normalization: ${from} → ${normalizedFrom}`,
      );
    }
    if (
      normalizedFrom !== mount
      && normalizedFrom !== `${mount}/`
      && !normalizedFrom.startsWith(`${mount}/`)
    ) {
      failures.push(`redirect source escapes ${mount}/: ${from}`);
    }
    if (!rule || typeof rule.location !== 'string' || !rule.location.startsWith('/')) {
      failures.push(`redirect for ${from} needs an absolute location path`);
      continue;
    }
    const normalizedLocation = new URL(rule.location, 'https://example.invalid').pathname;
    if (normalizedLocation !== rule.location) {
      failures.push(
        `redirect target is not canonical after URL normalization: ${rule.location} → ${normalizedLocation}`,
      );
    }
    if (
      normalizedLocation !== mount
      && normalizedLocation !== `${mount}/`
      && !normalizedLocation.startsWith(`${mount}/`)
    ) {
      failures.push(`redirect target escapes ${mount}/: ${rule.location}`);
    }
    if (rule.status !== undefined && !PERMITTED_REDIRECT_STATUSES.includes(rule.status)) {
      failures.push(`redirect for ${from} has unsupported status ${rule.status}`);
    }
  }

  return failures;
}

async function request(
  url,
  fetchImpl,
  { method = 'GET', redirect = 'manual', timeoutMs = REQUEST_TIMEOUT_MS } = {},
) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(url, {
      method,
      redirect,
      signal: controller.signal,
      headers: { 'user-agent': USER_AGENT },
    });
    let finished = false;
    return {
      response,
      // Cancelling an aborted or errored stream can reject. Releasing the body
      // is best-effort cleanup, so it must never displace the caller's own
      // diagnosis — every call site reports something more precise than a
      // cancellation failure.
      async cancel() {
        if (finished) return;
        try {
          await response.body?.cancel();
        } catch {
          // Ignored: the deadline is cleared below either way.
        } finally {
          finished = true;
          clearTimeout(timer);
        }
      },
      complete() {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
      },
    };
  } catch (error) {
    clearTimeout(timer);
    throw error;
  }
}

async function minimumBodyBytes(exchange, minimum = 16) {
  const { response } = exchange;
  if (response.body && typeof response.body.getReader === 'function') {
    const reader = response.body.getReader();
    let bytes = 0;
    try {
      while (bytes < minimum) {
        const { done, value } = await reader.read();
        if (done) break;
        bytes += typeof value === 'string'
          ? new TextEncoder().encode(value).byteLength
          : value?.byteLength ?? 0;
      }
      if (bytes >= minimum) {
        // Releasing the rest of the body is cleanup, not evidence: the bytes
        // are already counted, so a rejection here must not turn a proven-good
        // path into an "unreachable" failure.
        await reader.cancel().catch(() => {});
      }
      exchange.complete();
      return bytes;
    } catch (error) {
      // Cancelling an aborted or errored stream can itself reject; the original
      // read failure is the diagnosis worth surfacing.
      await exchange.cancel().catch(() => {});
      throw error;
    }
  }

  try {
    let bytes = 0;
    if (typeof response.arrayBuffer === 'function') {
      bytes = (await response.arrayBuffer()).byteLength;
    } else if (typeof response.text === 'function') {
      bytes = new TextEncoder().encode(await response.text()).byteLength;
    }
    exchange.complete();
    return bytes;
  } catch (error) {
    await exchange.cancel().catch(() => {});
    throw error;
  }
}

async function mapPool(items, concurrency, worker) {
  const results = new Array(items.length);
  let next = 0;
  async function run() {
    while (next < items.length) {
      const index = next;
      next += 1;
      results[index] = await worker(items[index], index);
    }
  }
  await Promise.all(
    Array.from({ length: Math.min(concurrency, Math.max(items.length, 1)) }, () => run()),
  );
  return results;
}

function resolveLocation(response, requestUrl) {
  const location = response.headers.get('location');
  if (!location) return null;
  try {
    return new URL(location, requestUrl);
  } catch {
    return null;
  }
}

/**
 * @returns {{ ok: boolean, checked: string[], failures: string[] }}
 */
export async function runUrlCompatibility({
  baseUrl,
  pathnames = publicUrlPathnames,
  redirects = publicRedirects,
  fetchImpl = fetch,
  concurrency = DEFAULT_CONCURRENCY,
  requestTimeoutMs = REQUEST_TIMEOUT_MS,
}) {
  if (!Number.isSafeInteger(concurrency) || concurrency <= 0) {
    throw new Error('concurrency must be a positive safe integer');
  }
  const origin = normalizeCompatibilityBaseUrl(baseUrl);
  const contractFailures = validateManifestContract({ pathnames, redirects });
  if (contractFailures.length > 0) {
    return { ok: false, checked: [], failures: contractFailures };
  }

  const failures = [];
  const checked = [];
  const live = liveManifestPathnames({ pathnames, redirects });
  const redirectEntries = redirectManifestEntries(redirects);

  await mapPool(live, concurrency, async (pathname) => {
    const url = `${origin}${pathname}`;
    const bodyRequired = (
      pathname.endsWith('/')
      || pathname.endsWith('.html')
      || pathname.endsWith('.png')
    );
    checked.push(pathname);
    try {
      let exchange = await request(url, fetchImpl, {
        method: bodyRequired ? 'GET' : 'HEAD',
        redirect: 'manual',
        timeoutMs: requestTimeoutMs,
      });
      let { response } = exchange;
      if (
        !bodyRequired
        && (
          response.status === 405
          || response.status === 403
          || response.status === 404
          || (response.status >= 300 && response.status < 400)
        )
      ) {
        await exchange.cancel();
        exchange = await request(url, fetchImpl, {
          method: 'GET',
          redirect: 'manual',
          timeoutMs: requestTimeoutMs,
        });
        ({ response } = exchange);
      }

      if (response.status >= 300 && response.status < 400) {
        const location = resolveLocation(response, url);
        await exchange.cancel();
        failures.push(
          `${pathname}: unexpected redirect ${response.status} `
          + `→ ${location?.pathname ?? '(no location)'} `
          + '(add publicRedirects for intentional moves)',
        );
        return;
      }

      if (!response.ok) {
        await exchange.cancel();
        failures.push(`${pathname}: status ${response.status}, expected 200`);
        return;
      }

      if (bodyRequired) {
        const bytes = await minimumBodyBytes(exchange);
        if (bytes < 16) {
          failures.push(`${pathname}: response body is empty or too small`);
        }
      } else {
        await exchange.cancel();
      }
    } catch (error) {
      failures.push(
        `${pathname}: unreachable (${error instanceof Error ? error.message : 'request failed'})`,
      );
    }
  });

  await mapPool(redirectEntries, concurrency, async ([pathname, rule]) => {
    const url = `${origin}${pathname}`;
    checked.push(pathname);
    try {
      const exchange = await request(url, fetchImpl, {
        method: 'GET',
        redirect: 'manual',
        timeoutMs: requestTimeoutMs,
      });
      const { response } = exchange;
      const expectedStatus = rule.status ?? null;
      if (expectedStatus !== null && response.status !== expectedStatus) {
        await exchange.cancel();
        failures.push(
          `${pathname}: redirect status ${response.status}, expected ${expectedStatus}`,
        );
        return;
      }
      if (!PERMITTED_REDIRECT_STATUSES.includes(response.status)) {
        await exchange.cancel();
        failures.push(
          `${pathname}: expected redirect, got status ${response.status}`,
        );
        return;
      }
      const actual = resolveLocation(response, url);
      await exchange.cancel();
      if (actual && actual.origin !== origin) {
        failures.push(
          `${pathname}: cross-origin redirect location ${actual.href}, expected ${rule.location}`,
        );
        return;
      }
      // Compare the whole target, not just its path: the Location is fetched
      // below as `actual.href`, so a query string or fragment the manifest never
      // declared would send the gate off to validate an undeclared URL.
      const declared = new URL(rule.location, 'https://example.invalid');
      const expectedLocation = declared.pathname + declared.search + declared.hash;
      const actualLocation = actual
        ? actual.pathname + actual.search + actual.hash
        : null;
      if (actualLocation !== expectedLocation) {
        failures.push(
          `${pathname}: redirect location ${actualLocation ?? '(absent)'}, expected ${expectedLocation}`,
        );
        return;
      }

      // Declaring a move takes the source out of the live path list, so nothing
      // else asserts the destination. A redirect into a 404, an empty body, or
      // another redirect is a broken URL wearing a valid-looking Location.
      const target = await request(actual.href, fetchImpl, {
        method: 'GET',
        redirect: 'manual',
        timeoutMs: requestTimeoutMs,
      });
      if (target.response.status >= 300 && target.response.status < 400) {
        const next = resolveLocation(target.response, actual.href);
        await target.cancel();
        failures.push(
          `${expectedLocation}: redirect target redirected again to `
          + `${next?.pathname ?? '(no location)'}, expected a served document`,
        );
        return;
      }
      // Exactly 200, not any 2xx: a declared move has to land on the document
      // itself, and a 201 or 204 from a documentation origin is not that.
      if (target.response.status !== 200) {
        await target.cancel();
        failures.push(
          `${expectedLocation}: redirect target status ${target.response.status}, expected 200`,
        );
        return;
      }
      const targetBytes = await minimumBodyBytes(target);
      if (targetBytes < 16) {
        failures.push(`${expectedLocation}: redirect target body is empty or too small`);
      }
    } catch (error) {
      failures.push(
        `${pathname}: unreachable (${error instanceof Error ? error.message : 'request failed'})`,
      );
    }
  });

  return { ok: failures.length === 0, checked, failures };
}
