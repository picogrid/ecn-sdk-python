// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  CACHE_BRAND,
  CACHE_IMMUTABLE,
  CACHE_REVALIDATE,
  CONTENT_SECURITY_POLICY,
  SECURITY_HEADERS,
  applyResponseHeaders,
  buildSecurityHeaders,
  cacheControlForPath,
  isBrandAssetPath,
  isImmutableAssetPath,
} from "./headers.ts";

import worker from './worker.ts';

describe('isImmutableAssetPath', () => {
  it('only treats emitted five- or eight-character filename tokens as fingerprints', () => {
    assert.equal(
      isImmutableAssetPath(
        '/ecn-sdk/_astro/MobileTableOfContents.astro_astro_type_script_index_0_lang.BcSo_yiZ.js',
      ),
      true,
    );
    assert.equal(
      isImmutableAssetPath('/ecn-sdk/_astro/index.DlwGQA8D.css'),
      true,
    );
    assert.equal(
      isImmutableAssetPath('/ecn-sdk/_astro/ec.0vx5m.js'),
      true,
    );
    assert.equal(
      isImmutableAssetPath('/ecn-sdk/assets/reference.generated.js'),
      false,
    );
    assert.equal(
      isImmutableAssetPath('/ecn-sdk/assets/logo.horizontal.png'),
      false,
    );
  });

  it("treats HTML and plain paths as revalidate-only", () => {
    assert.equal(isImmutableAssetPath("/ecn-sdk/"), false);
    assert.equal(isImmutableAssetPath("/ecn-sdk/index.html"), false);
    assert.equal(isImmutableAssetPath("/ecn-sdk/getting-started/"), false);
    assert.equal(isImmutableAssetPath("/ecn-sdk/favicon.svg"), false);
  });
});

describe("isBrandAssetPath", () => {
  it("matches brand artwork images", () => {
    assert.equal(
      isBrandAssetPath("/ecn-sdk/brand/picogrid-nav-texture.png"),
      true,
    );
    assert.equal(
      isBrandAssetPath("/ecn-sdk/brand/picogrid-wordmark-dark.png"),
      true,
    );
  });

  it("does not match pages or non-brand assets", () => {
    assert.equal(isBrandAssetPath("/ecn-sdk/brand/"), false);
    assert.equal(isBrandAssetPath("/ecn-sdk/brand/readme.html"), false);
    assert.equal(isBrandAssetPath("/ecn-sdk/favicon.svg"), false);
  });
});

describe("cacheControlForPath", () => {
  it("returns immutable policy for fingerprinted assets", () => {
    assert.equal(
      cacheControlForPath('/ecn-sdk/_astro/page.Dwipeu-R.js'),
      CACHE_IMMUTABLE,
    );
  });

  it("returns the brand policy for unhashed brand artwork", () => {
    assert.equal(
      cacheControlForPath("/ecn-sdk/brand/picogrid-nav-texture.png"),
      CACHE_BRAND,
    );
    // Brand policy allows serving from cache without a blocking revalidation,
    // so the band artwork cannot flash on route changes.
    assert.match(CACHE_BRAND, /max-age=(?!0\b)\d+/);
    assert.notEqual(CACHE_BRAND, CACHE_REVALIDATE);
    assert.notEqual(CACHE_BRAND, CACHE_IMMUTABLE);
  });

  it("returns revalidate policy for HTML and other paths", () => {
    assert.equal(cacheControlForPath("/ecn-sdk/"), CACHE_REVALIDATE);
    assert.equal(
      cacheControlForPath("/ecn-sdk/how-to/cleanup/"),
      CACHE_REVALIDATE,
    );
  });
});

describe("buildSecurityHeaders", () => {
  it("includes required hardening headers without HSTS", () => {
    const headers = buildSecurityHeaders();
    assert.deepEqual(
      Object.keys(headers).sort(),
      Object.keys(SECURITY_HEADERS).sort(),
    );
    for (const [name, expectedValue] of Object.entries(SECURITY_HEADERS)) {
      assert.equal(headers[name], expectedValue, `${name} must match policy`);
    }
    assert.equal(headers["X-Content-Type-Options"], "nosniff");
    assert.equal(headers["Referrer-Policy"], "strict-origin-when-cross-origin");
    assert.equal(headers["X-Frame-Options"], "DENY");
    assert.match(headers["Permissions-Policy"] ?? "", /camera=\(\)/);
    assert.equal(headers["Content-Security-Policy"], CONTENT_SECURITY_POLICY);
    assert.match(CONTENT_SECURITY_POLICY, /frame-ancestors 'none'/);
    assert.equal(headers["Strict-Transport-Security"], undefined);
    assert.ok(!Object.hasOwn(headers, "Strict-Transport-Security"));
  });

  it("keeps CSP from being open to arbitrary third-party script hosts", () => {
    assert.doesNotMatch(CONTENT_SECURITY_POLICY, /script-src[^;]*\*/);
    assert.doesNotMatch(CONTENT_SECURITY_POLICY, /default-src[^;]*\*/);
    assert.deepEqual(CONTENT_SECURITY_POLICY.split("; "), [
      "default-src 'self'",
      "base-uri 'self'",
      "form-action 'self'",
      "frame-ancestors 'none'",
      "object-src 'none'",
      "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'",
      "style-src 'self' 'unsafe-inline'",
      "img-src 'self' data:",
      "font-src 'self' data:",
      "connect-src 'self'",
      "media-src 'self'",
      "worker-src 'self'",
      "manifest-src 'self'",
      "upgrade-insecure-requests",
    ]);
  });
});

describe("applyResponseHeaders", () => {
  it("overwrites cache and security headers while preserving other asset headers", () => {
    const incoming = new Headers({
      ...Object.fromEntries(
        Object.keys(SECURITY_HEADERS).map((name) => [name, "hostile-value"]),
      ),
      "Content-Security-Policy": "default-src *",
      "X-Frame-Options": "ALLOWALL",
      "Referrer-Policy": "unsafe-url",
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "max-age=999",
      ETag: '"abc"',
    });
    const out = applyResponseHeaders(incoming, "/ecn-sdk/index.html", 200);
    assert.equal(out.get("Content-Type"), "text/html; charset=utf-8");
    assert.equal(out.get("ETag"), '"abc"');
    assert.equal(out.get("Cache-Control"), CACHE_REVALIDATE);
    for (const [name, expectedValue] of Object.entries(SECURITY_HEADERS)) {
      assert.equal(
        out.get(name),
        expectedValue,
        `${name} must overwrite the incoming value`,
      );
    }
    assert.equal(out.get("X-Content-Type-Options"), "nosniff");
  });

  it("fills missing Pagefind wasm Content-Type so nosniff cannot strip search", () => {
    const out = applyResponseHeaders(
      new Headers(),
      "/ecn-sdk/pagefind/wasm.en.pagefind",
      200,
    );
    assert.equal(out.get("Content-Type"), "application/wasm");
    assert.equal(out.get("X-Content-Type-Options"), "nosniff");
  });

  it("fills missing Pagefind index/meta Content-Type", () => {
    const index = applyResponseHeaders(
      new Headers(),
      "/ecn-sdk/pagefind/index/en_abc.pf_index",
      200,
    );
    assert.equal(index.get("Content-Type"), "application/octet-stream");
    const meta = applyResponseHeaders(
      new Headers(),
      "/ecn-sdk/pagefind/pagefind.en_abc.pf_meta",
      200,
    );
    assert.equal(meta.get("Content-Type"), "application/octet-stream");
    const fragment = applyResponseHeaders(
      new Headers(),
      "/ecn-sdk/pagefind/fragment/en_abc.pf_fragment",
      200,
    );
    assert.equal(fragment.get("Content-Type"), "application/octet-stream");
  });

  it("does not override an existing Content-Type", () => {
    // Distinct from the Pagefind fallback, so overwriting with it would fail.
    const out = applyResponseHeaders(
      new Headers({ "Content-Type": "text/plain; charset=utf-8" }),
      "/ecn-sdk/pagefind/wasm.en.pagefind",
      200,
    );
    assert.equal(out.get("Content-Type"), "text/plain; charset=utf-8");
  });

  it('only gives successful immutable-looking responses the immutable policy', () => {
    const path = '/ecn-sdk/_astro/chunk.AbCdEf12.js';
    const notFound = applyResponseHeaders(new Headers(), path, 404);
    const success = applyResponseHeaders(new Headers(), path, 200);

    assert.equal(notFound.get('Cache-Control'), CACHE_REVALIDATE);
    assert.equal(success.get('Cache-Control'), CACHE_IMMUTABLE);
  });

  it('allows only the published README wordmarks to load cross-origin', () => {
    for (const theme of ['light', 'dark']) {
      const wordmark = applyResponseHeaders(
        new Headers(),
        `/ecn-sdk/brand/picogrid-wordmark-${theme}.png`,
        200,
      );
      assert.equal(
        wordmark.get('Cross-Origin-Resource-Policy'),
        'cross-origin',
      );
    }

    const versionedWordmark = applyResponseHeaders(
      new Headers(),
      '/ecn-sdk/brand/picogrid-wordmark-light.png?version=0.1.0',
      200,
    );
    assert.equal(
      versionedWordmark.get('Cross-Origin-Resource-Policy'),
      'cross-origin',
    );

    const revalidatedWordmark = applyResponseHeaders(
      new Headers(),
      '/ecn-sdk/brand/picogrid-wordmark-light.png',
      304,
    );
    assert.equal(
      revalidatedWordmark.get('Cross-Origin-Resource-Policy'),
      'cross-origin',
    );

    const nonCanonicalWordmark = applyResponseHeaders(
      new Headers(),
      '/other/brand/picogrid-wordmark-light.png',
      200,
    );
    assert.equal(
      nonCanonicalWordmark.get('Cross-Origin-Resource-Policy'),
      'same-origin',
    );

    const mixedCaseWordmark = applyResponseHeaders(
      new Headers(),
      '/ECN-SDK/brand/PICOGRID-WORDMARK-LIGHT.PNG',
      200,
    );
    assert.equal(
      mixedCaseWordmark.get('Cross-Origin-Resource-Policy'),
      'same-origin',
    );

    const appIcon = applyResponseHeaders(
      new Headers(),
      '/ecn-sdk/brand/picogrid-app-icon-192.png',
      200,
    );
    assert.equal(
      appIcon.get('Cross-Origin-Resource-Policy'),
      'same-origin',
    );

    const missingWordmark = applyResponseHeaders(
      new Headers(),
      '/ecn-sdk/brand/picogrid-wordmark-light.png',
      404,
    );
    assert.equal(
      missingWordmark.get('Cross-Origin-Resource-Policy'),
      'same-origin',
    );
  });
});

describe('worker', () => {
  it('applies security headers to the canonical mount redirect', async () => {
    const response = await worker.fetch(
      new Request('https://ecn.example/ecn-sdk'),
      {
        ASSETS: {
          async fetch() {
            throw new Error('redirect must not fetch assets');
          },
        },
      },
    );

    assert.equal(response.status, 308);
    assert.equal(response.headers.get('Location'), 'https://ecn.example/ecn-sdk/');
    assert.equal(
      response.headers.get('Content-Security-Policy'),
      CONTENT_SECURITY_POLICY,
    );
    assert.equal(response.headers.get('X-Frame-Options'), 'DENY');
    assert.equal(
      response.headers.get('Referrer-Policy'),
      'strict-origin-when-cross-origin',
    );
  });

  it('returns a secured plain 502 when the assets binding throws', async () => {
    const response = await worker.fetch(
      new Request('https://ecn.example/ecn-sdk/'),
      {
        ASSETS: {
          async fetch() {
            throw new Error('binding unavailable');
          },
        },
      },
    );

    assert.equal(response.status, 502);
    assert.equal(await response.text(), 'Documentation assets unavailable.\n');
    assert.match(response.headers.get('Content-Type') ?? '', /^text\/plain/);
    assert.equal(
      response.headers.get('Content-Security-Policy'),
      CONTENT_SECURITY_POLICY,
    );
    assert.equal(response.headers.get('X-Frame-Options'), 'DENY');
    assert.equal(
      response.headers.get('Referrer-Policy'),
      'strict-origin-when-cross-origin',
    );
  });
});
