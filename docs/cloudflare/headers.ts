// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/** Header and cache policy for the docs Worker (pure; unit-tested). */

export const CACHE_IMMUTABLE = "public, max-age=31536000, immutable";

export const CACHE_REVALIDATE = "public, max-age=0, must-revalidate";

/**
 * Brand artwork ships unhashed at contract-fixed paths (NOTICE.md pins the
 * exact bytes), so it cannot ride the fingerprint tier — but revalidating it
 * per navigation makes the header band flash on every route change while the
 * browser waits out the conditional request. A short freshness window paints
 * it from cache instantly; stale-while-revalidate keeps updates flowing in the
 * background if the artwork ever does change.
 */
export const CACHE_BRAND =
  "public, max-age=86400, stale-while-revalidate=604800";

// Starlight needs 'unsafe-inline' for small theme/script islands.
// Pagefind search compiles WASM in-page and needs 'wasm-unsafe-eval' (not full
// 'unsafe-eval'). HSTS is zone-level, not set here.
export const CONTENT_SECURITY_POLICY = [
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
].join("; ");

export const PERMISSIONS_POLICY = [
  "accelerometer=()",
  "autoplay=()",
  "camera=()",
  "display-capture=()",
  "encrypted-media=()",
  "fullscreen=()",
  "geolocation=()",
  "gyroscope=()",
  "magnetometer=()",
  "microphone=()",
  "midi=()",
  "payment=()",
  "picture-in-picture=()",
  "publickey-credentials-get=()",
  "screen-wake-lock=()",
  "sync-xhr=()",
  "usb=()",
  "web-share=()",
  "xr-spatial-tracking=()",
  "interest-cohort=()",
  "browsing-topics=()",
].join(", ");

export type SecurityHeaderMap = Record<string, string>;

export const SECURITY_HEADERS: Readonly<SecurityHeaderMap> = Object.freeze({
  "Content-Security-Policy": CONTENT_SECURITY_POLICY,
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "Permissions-Policy": PERMISSIONS_POLICY,
  "X-Frame-Options": "DENY",
  "Cross-Origin-Opener-Policy": "same-origin",
  "Cross-Origin-Resource-Policy": "same-origin",
  "X-DNS-Prefetch-Control": "off",
});

export function buildSecurityHeaders(): SecurityHeaderMap {
  return { ...SECURITY_HEADERS };
}

export function isImmutableAssetPath(pathname: string): boolean {
  const path = pathname.split("?")[0] ?? pathname;

  return /\.(?:[A-Za-z0-9_-]{5}|[A-Za-z0-9_-]{8})\.(?:js|mjs|cjs|css|woff2?|ttf|otf|png|jpe?g|gif|svg|webp|avif|ico|map)$/i.test(
    path,
  );
}

export function isBrandAssetPath(pathname: string): boolean {
  const path = pathname.split("?")[0] ?? pathname;
  return /\/brand\/[^/]+\.(?:png|jpe?g|gif|svg|webp|avif|ico)$/i.test(path);
}

export function cacheControlForPath(pathname: string): string {
  if (isImmutableAssetPath(pathname)) return CACHE_IMMUTABLE;
  if (isBrandAssetPath(pathname)) return CACHE_BRAND;
  return CACHE_REVALIDATE;
}

/**
 * Workers Static Assets sometimes omit Content-Type for Pagefind binaries.
 * With X-Content-Type-Options: nosniff the browser then refuses them and client
 * search dies. Static file servers usually default to octet-stream.
 */
export function ensureAssetContentType(
  headers: Headers,
  pathname: string,
): void {
  if (headers.get("Content-Type")) return;
  const path = (pathname.split("?")[0] ?? pathname).toLowerCase();
  if (path.endsWith(".pagefind")) {
    headers.set("Content-Type", "application/wasm");
    return;
  }
  if (
    path.endsWith(".pf_meta") ||
    path.endsWith(".pf_fragment") ||
    path.endsWith(".pf_index")
  ) {
    headers.set("Content-Type", "application/octet-stream");
  }
}

function isPublishedWordmarkPath(pathname: string): boolean {
  const path = pathname.split("?")[0] ?? pathname;
  return /^\/ecn-sdk\/brand\/picogrid-wordmark-(?:light|dark)\.png$/.test(path);
}

export function applyResponseHeaders(
  headers: Headers,
  pathname: string,
  status: number,
): Headers {
  const next = new Headers(headers);
  ensureAssetContentType(next, pathname);
  for (const [name, value] of Object.entries(buildSecurityHeaders())) {
    next.set(name, value);
  }
  if (
    ((status >= 200 && status < 300) || status === 304) &&
    isPublishedWordmarkPath(pathname)
  ) {
    next.set("Cross-Origin-Resource-Policy", "cross-origin");
  }
  next.set(
    "Cache-Control",
    status >= 200 && status < 300
      ? cacheControlForPath(pathname)
      : CACHE_REVALIDATE,
  );
  return next;
}
