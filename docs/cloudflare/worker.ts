// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import { applyResponseHeaders } from "./headers.ts";

/**
 * Structural binding type: avoids depending on generated Workers globals so the
 * repository type-checks with one Astro-owned tsconfig.
 */
export interface Env {
  ASSETS: { fetch(request: Request): Promise<Response> };
}

/** Mount without trailing slash (matches site-config documentationBasePath). */
const DOCUMENTATION_MOUNT = "/ecn-sdk";

function withStandardHeaders(response: Response, pathname: string): Response {
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: applyResponseHeaders(response.headers, pathname, response.status),
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    // Canonicalize the mount: static assets live under /ecn-sdk/, and a bare
    // /ecn-sdk path otherwise 404s (or never reaches the Worker without an
    // extra zone route for the unsuffixed path).
    if (url.pathname === DOCUMENTATION_MOUNT) {
      url.pathname = `${DOCUMENTATION_MOUNT}/`;
      return withStandardHeaders(Response.redirect(url, 308), url.pathname);
    }

    let assetResponse: Response;
    try {
      assetResponse = await env.ASSETS.fetch(request);
    } catch {
      assetResponse = new Response('Documentation assets unavailable.\n', {
        status: 502,
        headers: { 'Content-Type': 'text/plain; charset=utf-8' },
      });
    }
    return withStandardHeaders(assetResponse, url.pathname);
  },
};
