// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * ReadMe over-capture probe (A8 post-promote gate).
 *
 * The Worker route must be scoped to `/ecn-sdk/*` only. If the Worker ever
 * intercepts other `docs.picogrid.com` paths, production ReadMe content is
 * replaced by Workers Static Assets (root redirect, guide CSP, branded 404).
 * This probe fetches a non-mount path and fails when the response looks like
 * the ECN Worker policy rather than the long-standing ReadMe origin.
 */

import {
  documentationBase,
  documentationOrigin,
} from './site-config.mjs';

const headersModuleUrl = new URL('../cloudflare/headers.ts', import.meta.url).href;

const REQUEST_TIMEOUT_MS = 12_000;
const USER_AGENT = 'Picogrid-ECN-SDK-readme-overcapture-probe/1.0';

/** Default probe: host root, which must never be owned by the ECN Worker. */
export const DEFAULT_README_PROBE_PATH = '/';

/**
 * Loopback origins exist for tests only: the promotion workflow always derives
 * the probe origin from `documentationOrigin`, so production callers must not
 * be able to aim this gate at a loopback host through configuration.
 */
export function normalizeProbeOrigin(
  value = documentationOrigin,
  { allowLoopback = false } = {},
) {
  const url = new URL(value);
  const isLoopback = url.hostname === 'localhost' || url.hostname === '127.0.0.1';
  if (isLoopback && !allowLoopback) {
    throw new Error('probe origin must not be loopback');
  }
  if (
    url.protocol !== 'https:'
    && !(allowLoopback && isLoopback && url.protocol === 'http:')
  ) {
    throw new Error(`probe origin must be https (got ${url.protocol})`);
  }
  return url.origin;
}

/**
 * Paths under the ECN mount are not valid over-capture probes — those *should*
 * be Worker-served after cutover.
 */
export function assertNonEcnProbePath(pathname) {
  if (!pathname.startsWith('/')) {
    throw new Error(`probe path must be absolute (got ${pathname})`);
  }
  const probePathname = new URL(pathname, documentationOrigin).pathname;
  const mountPathname = new URL(documentationBase, documentationOrigin).pathname.replace(/\/$/, '');
  if (
    probePathname === mountPathname
    || probePathname.startsWith(`${mountPathname}/`)
  ) {
    throw new Error(
      `probe path ${pathname} is under ${documentationBase}; choose a ReadMe path outside the Worker route`,
    );
  }
  return pathname;
}

/** Media type without parameters, case-folded, for exact comparison. */
function mediaType(value) {
  return typeof value === 'string'
    ? value.split(';', 1)[0].trim().toLowerCase()
    : '';
}

/**
 * Signals that the response was produced by this Worker (or a clone of its
 * assets + header policy) rather than the ReadMe origin.
 */
export function isWorkerOvercaptureResponse({
  pathname,
  status,
  contentType,
  csp,
  body,
  workerCsp,
}) {
  if (csp && workerCsp && csp === workerCsp) {
    return `Content-Security-Policy matches the ECN Worker policy on ${pathname}`;
  }

  // Root redirect assets live only in Worker/Pages site-dist root — ReadMe does
  // not emit this client navigate into the guide mount.
  if (typeof body === 'string' && body.includes(`location.replace('${documentationBase}'`)) {
    return `body on ${pathname} contains the ECN root redirect into ${documentationBase}`;
  }
  if (typeof body === 'string' && body.includes(`url=${documentationBase}`)) {
    return `body on ${pathname} contains the ECN meta refresh into ${documentationBase}`;
  }

  // Hard 404 of our branded guide page on a non-mount path means the Worker
  // handled the miss (Workers Static Assets 404-page), not ReadMe's 404.
  if (
    status === 404
    && mediaType(contentType) === 'text/html'
    && typeof body === 'string'
    && body.includes('Page not found')
    && body.includes('ecn-sdk')
  ) {
    return `branded ECN 404 served for non-mount path ${pathname}`;
  }

  return null;
}

export async function runReadmeOvercaptureProbe({
  origin = documentationOrigin,
  probePath = DEFAULT_README_PROBE_PATH,
  fetchImpl = fetch,
  allowLoopback = false,
}) {
  const base = normalizeProbeOrigin(origin, { allowLoopback });
  const path = assertNonEcnProbePath(probePath);
  const { CONTENT_SECURITY_POLICY } = await import(headersModuleUrl);

  const url = `${base}${path}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response;
  let body = '';
  try {
    response = await fetchImpl(url, {
      method: 'GET',
      redirect: 'follow',
      signal: controller.signal,
      headers: { 'user-agent': USER_AGENT },
    });
    body = await response.text();
  } catch (error) {
    clearTimeout(timer);
    return {
      ok: false,
      url,
      failures: [
        `${url}: probe fetch failed (${error instanceof Error ? error.message : 'request failed'})`,
      ],
    };
  }
  clearTimeout(timer);

  const reason = isWorkerOvercaptureResponse({
    pathname: path,
    status: response.status,
    contentType: response.headers.get('content-type') ?? '',
    csp: response.headers.get('content-security-policy'),
    body,
    workerCsp: CONTENT_SECURITY_POLICY,
  });

  if (reason) {
    return { ok: false, url, failures: [`over-capture: ${reason}`] };
  }

  return { ok: true, url, failures: [] };
}
