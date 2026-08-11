// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * ReadMe → ECN cross-link crawler (A6).
 *
 * Crawl a ReadMe origin (production docs host by default), collect every
 * `/ecn-sdk/*` link the published docs currently point at, rewrite those
 * targets onto a candidate Worker base, and fail if any rewritten URL is
 * unreachable. Release gates can therefore reject a preview that would break
 * live ReadMe cross-links.
 *
 * Network live mode is opt-in (CLI + preview workflow). Unit tests inject a
 * local fetch fixture so CI never depends on production ReadMe.
 */
import {
  documentationBasePath,
  documentationOrigin,
} from './site-config.mjs';

const DEFAULT_MAX_PAGES = 80;
const DEFAULT_CONCURRENCY = 6;
const REQUEST_TIMEOUT_MS = 12_000;
const MAX_REDIRECTS = 10;
const USER_AGENT = 'Picogrid-ECN-SDK-readme-ecn-crawler/1.0';

const LINK_ATTRIBUTE_PATTERN = /\b(href|src)=(?:"([^"]*)"|'([^']*)')/gi;

function requireHttpBaseUrl(value, label) {
  if (!value) {
    throw new Error(`${label} is required`);
  }
  const url = new URL(value);
  const isLocalHttp = (
    url.protocol === 'http:'
    && (url.hostname === 'localhost' || url.hostname === '127.0.0.1')
  );
  if (url.protocol !== 'https:' && !isLocalHttp) {
    throw new Error(`${label} must be https (got ${url.protocol})`);
  }
  return url.origin;
}

export function normalizeCandidateBaseUrl(value) {
  return requireHttpBaseUrl(value, 'candidate base URL (--base-url=<url> or PREVIEW_BASE_URL)');
}

export function normalizeSourceBaseUrl(value = documentationOrigin) {
  return requireHttpBaseUrl(value, 'source base URL');
}

/** True when the pathname is the ECN guide mount or a path under it. */
export function isEcnSdkPath(pathname) {
  return (
    pathname === documentationBasePath
    || pathname === `${documentationBasePath}/`
    || pathname.startsWith(`${documentationBasePath}/`)
  );
}

/**
 * Decode a single layer of HTML `&amp;` in attribute hrefs so query strings
 * stay accurate. Manual scan avoids multi-pass string replace (CodeQL).
 */
export function decodeHref(href) {
  if (!href.includes('&amp;') && !href.includes('&AMP;')) return href;
  let out = '';
  for (let i = 0; i < href.length; i += 1) {
    if (href.startsWith('&amp;', i) || href.startsWith('&AMP;', i)) {
      out += '&';
      i += 4;
      continue;
    }
    out += href[i];
  }
  return out;
}

/**
 * Resolve a raw href against a page URL and return an absolute URL string, or
 * null for fragments, mail, javascript, and non-http(s) schemes.
 */
export function resolveDocumentLink(href, pageUrl) {
  if (!href || href.startsWith('#') || /^(?:mailto|javascript|data|tel):/i.test(href)) {
    return null;
  }
  try {
    const absolute = new URL(decodeHref(href), pageUrl);
    if (absolute.protocol !== 'http:' && absolute.protocol !== 'https:') return null;
    return absolute.href;
  } catch {
    return null;
  }
}

/**
 * If the link is an ECN path on the source origin, return the absolute path
 * (pathname + search). Hash is dropped: Worker responses do not depend on it.
 */
export function extractEcnSdkTarget(absoluteHref, sourceOrigin) {
  let url;
  try {
    url = new URL(absoluteHref);
  } catch {
    return null;
  }
  if (url.origin !== sourceOrigin) return null;
  if (!isEcnSdkPath(url.pathname)) return null;
  return `${url.pathname}${url.search}`;
}

/** True when crawling should follow this same-origin non-ECN document link. */
export function isCrawlableReadmePath(pathname) {
  if (isEcnSdkPath(pathname)) return false;
  if (pathname.includes('//')) return false;
  // Skip obvious static assets; they do not host ReadMe cross-links to ECN.
  if (/\.(?:css|js|mjs|map|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|eot|pdf|zip|xml|json|txt)$/i.test(pathname)) {
    return false;
  }
  return true;
}

export function rewriteEcnTargetToCandidate(ecnTargetPath, candidateOrigin) {
  return new URL(ecnTargetPath, candidateOrigin).href;
}

async function cancelResponseBody(response) {
  const body = response.body;
  if (!body || response.bodyUsed || body.locked) return;
  try {
    await body.cancel();
  } catch {
    // Cancellation is best-effort; the response status remains usable.
  }
}

function isRedirectStatus(status) {
  return status === 301
    || status === 302
    || status === 303
    || status === 307
    || status === 308;
}

async function fetchText(url, fetchImpl, method = 'GET', redirectOrigin) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let requestUrl = url;
  try {
    for (let redirectCount = 0; ; redirectCount += 1) {
      const response = await fetchImpl(requestUrl, {
        method,
        redirect: 'manual',
        signal: controller.signal,
        headers: { 'user-agent': USER_AGENT },
      });
      const responseUrl = response.url ? new URL(response.url).href : requestUrl;
      if (redirectOrigin && new URL(responseUrl).origin !== redirectOrigin) {
        await cancelResponseBody(response);
        throw new Error(`redirect leaves origin ${redirectOrigin} (${responseUrl})`);
      }

      const location = response.headers.get('location');
      if (isRedirectStatus(response.status) && location) {
        await cancelResponseBody(response);
        const redirectUrl = new URL(location, responseUrl);
        if (redirectUrl.protocol !== 'http:' && redirectUrl.protocol !== 'https:') {
          throw new Error(`redirect uses unsupported protocol (${redirectUrl.protocol})`);
        }
        if (redirectOrigin && redirectUrl.origin !== redirectOrigin) {
          throw new Error(
            `redirect leaves origin ${redirectOrigin} (${redirectUrl.href})`,
          );
        }
        if (redirectCount >= MAX_REDIRECTS) {
          throw new Error(`too many redirects (more than ${MAX_REDIRECTS})`);
        }
        requestUrl = redirectUrl.href;
        continue;
      }

      if (method === 'HEAD') {
        await cancelResponseBody(response);
        return { response, body: '' };
      }
      const body = await response.text();
      return { response, body };
    }
  } finally {
    clearTimeout(timer);
  }
}

function requireValidMaxPages(maxPages) {
  if (!Number.isFinite(maxPages) || !Number.isInteger(maxPages) || maxPages < 1) {
    const value = typeof maxPages === 'string' ? JSON.stringify(maxPages) : String(maxPages);
    throw new Error(
      `max-pages must be a finite integer greater than or equal to 1; received ${value}`,
    );
  }
}

/**
 * Collect unique ECN targets and the ReadMe pages that referenced them.
 * Returns { pagesVisited, ecnTargets: Map<path, Set<sourcePage>>, crawlFailures }.
 */
export async function crawlReadmeForEcnLinks({
  sourceBaseUrl,
  seedPaths = ['/'],
  maxPages = DEFAULT_MAX_PAGES,
  fetchImpl = fetch,
}) {
  requireValidMaxPages(maxPages);
  const sourceOrigin = normalizeSourceBaseUrl(sourceBaseUrl);
  const queue = [];
  const queuedPages = new Set();
  const seenPages = new Set();
  const pagesVisited = [];
  const ecnTargets = new Map();
  const crawlFailures = [];
  let fetchAttempts = 0;

  for (const seed of seedPaths) {
    const path = seed.startsWith('/') ? seed : `/${seed}`;
    const seedUrl = new URL(path, sourceOrigin).href;
    if (!queuedPages.has(seedUrl)) {
      queue.push(seedUrl);
      queuedPages.add(seedUrl);
    }
  }

  // Bound total network fetches, not only successful HTML pages, so a flaky
  // host cannot burn unlimited quota on non-HTML / error responses.
  while (queue.length > 0 && fetchAttempts < maxPages) {
    const pageUrl = queue.shift();
    queuedPages.delete(pageUrl);
    if (seenPages.has(pageUrl)) continue;
    seenPages.add(pageUrl);
    fetchAttempts += 1;

    let response;
    let body;
    try {
      ({ response, body } = await fetchText(pageUrl, fetchImpl, 'GET', sourceOrigin));
    } catch (error) {
      crawlFailures.push(
        `${pageUrl}: crawl fetch failed (${error instanceof Error ? error.message : 'request failed'})`,
      );
      continue;
    }

    const finalUrl = response.url ? new URL(response.url).href : pageUrl;
    if (finalUrl !== pageUrl && seenPages.has(finalUrl)) continue;
    seenPages.add(finalUrl);

    const contentType = response.headers.get('content-type') ?? '';
    if (!response.ok) {
      crawlFailures.push(`${finalUrl}: crawl status ${response.status}`);
      continue;
    }
    if (!contentType.includes('text/html') && !contentType.includes('application/xhtml')) {
      crawlFailures.push(
        `${finalUrl}: skipped non-HTML content type ${contentType || '(absent)'}`,
      );
      continue;
    }

    pagesVisited.push(finalUrl);

    for (const match of body.matchAll(LINK_ATTRIBUTE_PATTERN)) {
      const attribute = match[1].toLowerCase();
      const href = match[2] ?? match[3];
      const absolute = resolveDocumentLink(href, finalUrl);
      if (!absolute) continue;

      const ecnPath = extractEcnSdkTarget(absolute, sourceOrigin);
      if (ecnPath) {
        if (!ecnTargets.has(ecnPath)) ecnTargets.set(ecnPath, new Set());
        ecnTargets.get(ecnPath).add(finalUrl);
        continue;
      }
      if (attribute !== 'href') continue;

      let linkUrl;
      try {
        linkUrl = new URL(absolute);
      } catch {
        continue;
      }
      if (linkUrl.origin !== sourceOrigin) continue;
      if (!isCrawlableReadmePath(linkUrl.pathname)) continue;
      // Drop hash/query for page identity to limit crawl fan-out.
      const next = `${linkUrl.origin}${linkUrl.pathname}`;
      if (!seenPages.has(next) && !queuedPages.has(next)) {
        queue.push(next);
        queuedPages.add(next);
      }
    }
  }

  return { sourceOrigin, pagesVisited, ecnTargets, crawlFailures };
}

function requireValidConcurrency(concurrency) {
  if (!Number.isFinite(concurrency) || !Number.isInteger(concurrency) || concurrency < 1) {
    throw new Error('concurrency must be a finite integer greater than or equal to 1');
  }
}


function formatReadmeSources(sources) {
  const sourcePages = [...sources];
  const displayed = sourcePages.slice(0, 3).join(', ');
  const remainder = sourcePages.length - 3;
  return remainder > 0 ? `${displayed}, … and ${remainder} more` : displayed;
}

async function mapPool(items, concurrency, worker) {
  requireValidConcurrency(concurrency);
  const results = new Array(items.length);
  let next = 0;
  async function run() {
    while (next < items.length) {
      const index = next;
      next += 1;
      results[index] = await worker(items[index], index);
    }
  }
  const runners = Array.from({ length: Math.min(concurrency, items.length) }, () => run());
  await Promise.all(runners);
  return results;
}

/**
 * Validate every collected ECN path against the candidate Worker base.
 */
export async function validateEcnTargetsOnCandidate({
  ecnTargets,
  candidateBaseUrl,
  fetchImpl = fetch,
  concurrency = DEFAULT_CONCURRENCY,
}) {
  const candidateOrigin = normalizeCandidateBaseUrl(candidateBaseUrl);
  // Plain code-unit ordering: localeCompare depends on the runtime locale and
  // ICU build, so it can order the same paths differently across machines.
  const entries = [...ecnTargets.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  const failures = [];
  const checked = await mapPool(entries, concurrency, async ([ecnPath, sources]) => {
    const candidateUrl = rewriteEcnTargetToCandidate(ecnPath, candidateOrigin);
    try {
      let resultMethod = 'HEAD';
      let result = await fetchText(candidateUrl, fetchImpl, resultMethod, candidateOrigin);
      // The first probe is always HEAD; retry with GET when the host disallows
      // or filters HEAD probes.
      if (result.response.status === 405 || result.response.status === 403) {
        result = await fetchText(candidateUrl, fetchImpl, 'GET', candidateOrigin);
        resultMethod = 'GET';
      }
      // Retry a HEAD response with GET when the host invents a body-less soft 404.
      if (resultMethod === 'HEAD' && result.response.status === 404) {
        result = await fetchText(candidateUrl, fetchImpl, 'GET', candidateOrigin);
        resultMethod = 'GET';
      }
      if (!result.response.ok) {
        const from = formatReadmeSources(sources);
        failures.push(
          `${candidateUrl}: status ${result.response.status} (from ReadMe: ${from})`,
        );
      }
    } catch (error) {
      const from = formatReadmeSources(sources);
      failures.push(
        `${candidateUrl}: unreachable (${error instanceof Error ? error.message : 'request failed'}) (from ReadMe: ${from})`,
      );
    }
    return candidateUrl;
  });

  failures.sort();
  return { candidateOrigin, checked, failures };
}

/**
 * Full A6 gate: crawl ReadMe for ECN links, validate them on the candidate.
 *
 * @returns {{ ok: boolean, pagesVisited: string[], ecnLinkCount: number, checked: string[], failures: string[], warnings: string[] }}
 */
export async function runReadmeEcnCrawler({
  sourceBaseUrl = documentationOrigin,
  candidateBaseUrl,
  seedPaths = ['/'],
  maxPages = DEFAULT_MAX_PAGES,
  concurrency = DEFAULT_CONCURRENCY,
  fetchImpl = fetch,
  /** When true, finding zero /ecn-sdk links is a failure (strict promotion gate). */
  requireEcnLinks = false,
}) {
  if (!candidateBaseUrl) {
    throw new Error('candidate base URL is required');
  }
  requireValidConcurrency(concurrency);

  const {
    pagesVisited,
    ecnTargets,
    crawlFailures,
  } = await crawlReadmeForEcnLinks({
    sourceBaseUrl,
    seedPaths,
    maxPages,
    fetchImpl,
  });

  // Crawl fetch/status noise on unrelated ReadMe pages must not block an
  // otherwise-correct ECN candidate. Surface them only when the crawl yields
  // no HTML (so operators still see why the source side is empty).
  const failures = [];
  const warnings = [...crawlFailures];

  if (pagesVisited.length === 0) {
    failures.push(...crawlFailures);
    failures.push('ReadMe crawl visited zero HTML pages; refuse to validate empty crawl');
    return {
      ok: false,
      pagesVisited,
      ecnLinkCount: 0,
      checked: [],
      failures: failures.sort(),
      // Already reported as hard failures on this path; repeating them as
      // warnings would print every diagnostic twice.
      warnings: [],
    };
  }

  if (ecnTargets.size === 0) {
    if (requireEcnLinks) {
      failures.push(
        `ReadMe crawl of ${pagesVisited.length} page(s) found no /ecn-sdk links`,
      );
    }
    return {
      ok: failures.length === 0,
      pagesVisited,
      ecnLinkCount: 0,
      checked: [],
      failures: failures.sort(),
      warnings,
    };
  }

  const validation = await validateEcnTargetsOnCandidate({
    ecnTargets,
    candidateBaseUrl,
    fetchImpl,
    concurrency,
  });
  failures.push(...validation.failures);

  return {
    ok: failures.length === 0,
    pagesVisited,
    ecnLinkCount: ecnTargets.size,
    checked: validation.checked,
    failures: failures.sort(),
    warnings,
  };
}
