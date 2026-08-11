// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/** Offline deploy-contract checks after docs:build (layout, hosts, CSP, URL manifest). */
import { readFile, readdir, stat } from 'node:fs/promises';
import { join, relative, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

import { parseJsonc } from './jsonc.mjs';
import { resolveLegionDocumentation } from './legion-documentation.mjs';
import { publicUrlPathnames } from './public-url-manifest.mjs';
import {
  assetsOutputDirectory,
  assetsOutputRoot,
  documentationBase,
  documentationBasePath,
  documentationHost,
  documentationWorkspaceRoot,
  repositoryRoot,
  siteContentDirectory,
  siteContentRoot,
} from './site-config.mjs';

const repoRoot = repositoryRoot;
const siteRoot = siteContentRoot;
const assetsRoot = assetsOutputRoot;
const wranglerPath = resolve(documentationWorkspaceRoot, 'wrangler.jsonc');
const headersModuleUrl = pathToFileURL(
  resolve(documentationWorkspaceRoot, 'cloudflare/headers.ts'),
).href;
const workerPath = resolve(documentationWorkspaceRoot, 'cloudflare/worker.ts');
// One canonical origin, derived from the shared host constant, so the checker
// stays aligned if that constant ever changes.
const canonicalOrigin = `https://${documentationHost}`;

const failures = [];

function fail(message) {
  failures.push(message);
}


async function filesBelow(directory) {
  const members = [];
  for (const name of await readdir(directory)) {
    const path = join(directory, name);
    const metadata = await stat(path);
    if (metadata.isDirectory()) members.push(...await filesBelow(path));
    else members.push(path);
  }
  return members;
}

function diskCandidatesForPublicPath(pathname) {
  // Normalize before the mount check: `/ecn-sdk/../../cloudflare/headers.ts`
  // starts with the mount as raw text but resolves outside it, and resolving
  // afterwards would reach a real repository file outside the site root.
  let normalized;
  try {
    normalized = new URL(pathname, canonicalOrigin).pathname;
  } catch {
    return [];
  }
  if (
    normalized !== pathname
    || (normalized !== documentationBasePath
      && !normalized.startsWith(`${documentationBasePath}/`))
  ) {
    return [];
  }
  // URL pathnames keep percent-encoding, but the built tree holds real names:
  // `/ecn-sdk/getting%20started/` must find the `getting started` directory.
  const rest = normalized
    .slice(documentationBasePath.length)
    .replace(/^\/+/, '')
    .split('/')
    .map((segment) => {
      try {
        return decodeURIComponent(segment);
      } catch {
        return segment;
      }
    })
    .join('/');
  if (!rest || rest.endsWith('/')) {
    const dir = rest.replace(/\/+$/, '');
    const base = dir ? join(siteRoot, dir) : siteRoot;
    return [join(base, 'index.html'), `${base}.html`];
  }
  const candidate = join(siteRoot, rest);
  // Defence in depth: never hand back a path outside the built site root.
  return candidate === siteRoot || candidate.startsWith(`${siteRoot}${sep}`)
    ? [candidate]
    : [];
}

async function pathExistsAsFile(path) {
  try {
    return (await stat(path)).isFile();
  } catch {
    return false;
  }
}


const entrypoint = join(siteRoot, 'index.html');
if (!(await pathExistsAsFile(entrypoint))) {
  fail(`expected entrypoint missing: ${relative(repoRoot, entrypoint)}`);
}

const pagesRootRedirect = join(assetsRoot, 'index.html');
if (!(await pathExistsAsFile(pagesRootRedirect))) {
  fail(`pages root redirect missing: ${relative(repoRoot, pagesRootRedirect)} (run docs:build)`);
} else {
  const rootHtml = await readFile(pagesRootRedirect, 'utf8');
  if (
    !rootHtml.includes(`url=${documentationBase}`)
    && !rootHtml.includes(`url="${documentationBase}"`)
  ) {
    fail(`pages root redirect does not target ${documentationBase}`);
  }
  if (!rootHtml.includes(`location.replace('${documentationBase}'`)) {
    fail(`pages root redirect missing client navigate to ${documentationBase}`);
  }
  if (!rootHtml.includes(documentationHost)) {
    fail('pages root redirect missing canonical host');
  }
}

try {
  const assetsMeta = await stat(assetsRoot);
  if (!assetsMeta.isDirectory()) {
    fail(`assets output is not a directory: ${assetsOutputDirectory}`);
  }
} catch {
  fail(`assets output directory missing: ${assetsOutputDirectory} (run docs:build first)`);
}


let wrangler;
try {
  wrangler = parseJsonc(await readFile(wranglerPath, 'utf8'));
} catch (error) {
  fail(`wrangler.jsonc could not be parsed: ${error.message}`);
}

if (wrangler) {
  const directory = wrangler.assets?.directory;
  const expectedDirectory = `../${assetsOutputDirectory}`;
  if (directory !== expectedDirectory) {
    fail(
      `wrangler assets.directory is ${JSON.stringify(directory)}, `
      + `expected ${JSON.stringify(expectedDirectory)} (site-config assetsOutputDirectory)`,
    );
  }
  if (wrangler.assets?.binding !== 'ASSETS') {
    fail(`wrangler assets.binding must be ASSETS (got ${JSON.stringify(wrangler.assets?.binding)})`);
  }
  if (wrangler.main !== 'cloudflare/worker.ts') {
    fail(`wrangler main must be cloudflare/worker.ts (got ${JSON.stringify(wrangler.main)})`);
  }
  // The production deploy resolves its smoke target by the Worker name, so a
  // rename here would silently break that lookup.
  if (wrangler.name !== 'ecn-sdk') {
    fail(`wrangler name must be ecn-sdk (got ${JSON.stringify(wrangler.name)})`);
  }

  // Zone routes decide which paths the Worker takes over from the existing
  // documentation product on the same host, so they are derived from the mount
  // rather than hand-written. `${mount}*` is rejected on purpose: it would also
  // capture sibling paths such as `/ecn-sdk-migration`, which the Worker does
  // not serve. Both exact patterns are required together, because the bare
  // mount is what redirects to the trailing-slash form.
  // Re-enabling the bootstrap host would restore a second crawlable copy of the
  // guide and move the production smoke back off the canonical host, undoing
  // the cutover without touching anything else this gate inspects.
  if (wrangler.workers_dev !== false) {
    fail(`wrangler workers_dev must be false (got ${JSON.stringify(wrangler.workers_dev)})`);
  }
  // Preview URLs default to whatever `workers_dev` is, so once the bootstrap
  // host is off this must stay explicitly enabled or the preview and promote
  // workflows lose the URLs they upload, smoke, and promote.
  if (wrangler.preview_urls !== true) {
    fail(`wrangler preview_urls must be true (got ${JSON.stringify(wrangler.preview_urls)})`);
  }

  // Routes are the canonical delivery path, so their absence is a regression
  // rather than an earlier stage: dropping them silently returns the mount to
  // the other documentation product on that host.
  const mount = documentationBasePath.replace(/\/$/, '');
  const expected = [`${documentationHost}${mount}`, `${documentationHost}${mount}/*`];
  if (!Array.isArray(wrangler.routes) || wrangler.routes.length === 0) {
    fail(
      `wrangler must declare the canonical host routes ${expected.map((one) => JSON.stringify(one)).join(', ')}`,
    );
  } else {
    const routes = wrangler.routes;
    const patterns = routes.map((route) => (typeof route === 'string' ? route : route?.pattern));
    for (const [index, pattern] of patterns.entries()) {
      if (!expected.includes(pattern)) {
        fail(
          `wrangler routes[${index}] pattern ${JSON.stringify(pattern)} is not derived from the `
          + `documentation mount; expected one of ${expected.map((one) => JSON.stringify(one)).join(', ')}`,
        );
      }
      const route = routes[index];
      if (typeof route !== 'string' && !route?.zone_name && !route?.zone_id) {
        fail(`wrangler routes[${index}] must name a zone with zone_name or zone_id`);
      }
    }
    for (const one of expected) {
      if (!patterns.includes(one)) {
        fail(`wrangler routes must include ${JSON.stringify(one)} so the mount and its subtree both resolve`);
      }
    }
  }
}

if (assetsOutputDirectory !== 'site-dist') {
  fail(`site-config assetsOutputDirectory must remain site-dist (got ${assetsOutputDirectory})`);
}
if (documentationBasePath !== '/ecn-sdk' || documentationBase !== '/ecn-sdk/') {
  fail(
    `site-config base path must be /ecn-sdk/ `
    + `(got basePath=${documentationBasePath}, base=${documentationBase})`,
  );
}
if (siteContentDirectory !== 'site-dist/ecn-sdk') {
  fail(`site-config siteContentDirectory must be site-dist/ecn-sdk (got ${siteContentDirectory})`);
}
if (documentationHost !== 'docs.picogrid.com') {
  fail(`site-config documentationHost must be docs.picogrid.com (got ${documentationHost})`);
}

try {
  const workerSource = await readFile(workerPath, 'utf8');
  const mountMatch = workerSource.match(
    /\bconst\s+DOCUMENTATION_MOUNT(?:\s*:\s*string)?\s*=\s*(["'])([^"']+)\1\s*;/,
  );
  if (!mountMatch) {
    fail('Worker DOCUMENTATION_MOUNT must be declared as a string literal');
  } else if (mountMatch[2] !== documentationBasePath) {
    fail(
      'Worker DOCUMENTATION_MOUNT must match site-config documentationBasePath '
      + `(got ${mountMatch[2]}, expected ${documentationBasePath})`,
    );
  }
} catch (error) {
  fail(`docs/cloudflare/worker.ts could not be read: ${error.message}`);
}


const expectedContentSecurityPolicy = [
  "default-src 'self'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
  "object-src 'none'",
  // Pagefind compiles its search index WASM in-page; 'wasm-unsafe-eval' is the
  // narrow grant for that, never full 'unsafe-eval'.
  "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self' data:",
  "connect-src 'self'",
  "media-src 'self'",
  "worker-src 'self'",
  "manifest-src 'self'",
  'upgrade-insecure-requests',
].join('; ');
const expectedPermissionsPolicy = [
  'accelerometer=()',
  'autoplay=()',
  'camera=()',
  'display-capture=()',
  'encrypted-media=()',
  'fullscreen=()',
  'geolocation=()',
  'gyroscope=()',
  'magnetometer=()',
  'microphone=()',
  'midi=()',
  'payment=()',
  'picture-in-picture=()',
  'publickey-credentials-get=()',
  'screen-wake-lock=()',
  'sync-xhr=()',
  'usb=()',
  'web-share=()',
  'xr-spatial-tracking=()',
  'interest-cohort=()',
  'browsing-topics=()',
].join(', ');
const expectedSecurityHeaders = Object.freeze({
  'Content-Security-Policy': expectedContentSecurityPolicy,
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'strict-origin-when-cross-origin',
  'Permissions-Policy': expectedPermissionsPolicy,
  'X-Frame-Options': 'DENY',
  'Cross-Origin-Opener-Policy': 'same-origin',
  'Cross-Origin-Resource-Policy': 'same-origin',
  'X-DNS-Prefetch-Control': 'off',
});

let headersModule;
try {
  headersModule = await import(headersModuleUrl);
} catch (error) {
  fail(`docs/cloudflare/headers.ts could not be imported: ${error.message}`);
}

if (headersModule) {
  const {
    CACHE_IMMUTABLE,
    CACHE_REVALIDATE,
    CONTENT_SECURITY_POLICY,
    SECURITY_HEADERS,
    buildSecurityHeaders,
    isImmutableAssetPath,
  } = headersModule;

  if (typeof CONTENT_SECURITY_POLICY !== 'string' || !CONTENT_SECURITY_POLICY) {
    fail('CONTENT_SECURITY_POLICY is missing or empty');
  } else {
    const requiredDirectives = [
      "default-src 'self'",
      "frame-ancestors 'none'",
      "object-src 'none'",
      "base-uri 'self'",
    ];
    for (const directive of requiredDirectives) {
      if (!CONTENT_SECURITY_POLICY.includes(directive)) {
        fail(`CSP missing required directive: ${directive}`);
      }
    }
    if (!/script-src[^;]*'self'/.test(CONTENT_SECURITY_POLICY)) {
      fail("CSP script-src must include 'self'");
    }
    if (!/style-src[^;]*'self'/.test(CONTENT_SECURITY_POLICY)) {
      fail("CSP style-src must include 'self'");
    }
  }

  if (
    !SECURITY_HEADERS
    || typeof SECURITY_HEADERS !== 'object'
    || typeof buildSecurityHeaders !== 'function'
  ) {
    fail('docs/cloudflare/headers.ts must export SECURITY_HEADERS and buildSecurityHeaders()');
  } else {
    const securityHeaders = buildSecurityHeaders();
    const expectedHeaderNames = Object.keys(expectedSecurityHeaders).sort();
    const tableHeaderNames = Object.keys(SECURITY_HEADERS).sort();
    const actualHeaderNames = Object.keys(securityHeaders).sort();
    if (
      expectedHeaderNames.length !== tableHeaderNames.length
      || expectedHeaderNames.some((name, index) => name !== tableHeaderNames[index])
    ) {
      fail(
        'SECURITY_HEADERS names must exactly match checker literal expectations '
        + `(expected ${expectedHeaderNames.join(', ')}, got ${tableHeaderNames.join(', ')})`,
      );
    }
    if (
      tableHeaderNames.length !== actualHeaderNames.length
      || tableHeaderNames.some((name, index) => name !== actualHeaderNames[index])
    ) {
      fail(
        'buildSecurityHeaders() header names must exactly match SECURITY_HEADERS '
        + `(expected ${tableHeaderNames.join(', ')}, got ${actualHeaderNames.join(', ')})`,
      );
    }
    for (const [name, expected] of Object.entries(expectedSecurityHeaders)) {
      if (SECURITY_HEADERS[name] !== expected) {
        fail(
          `SECURITY_HEADERS must set ${name} to ${JSON.stringify(expected)} `
          + `(got ${JSON.stringify(SECURITY_HEADERS[name])})`,
        );
      }
      if (securityHeaders[name] !== expected) {
        fail(
          `buildSecurityHeaders() must set ${name} to ${JSON.stringify(expected)} `
          + `(got ${JSON.stringify(securityHeaders[name])})`,
        );
      }
    }
  }

  if (CACHE_IMMUTABLE === CACHE_REVALIDATE) {
    fail('CACHE_IMMUTABLE and CACHE_REVALIDATE must remain distinct cache policies');
  }
  if (typeof isImmutableAssetPath !== 'function') {
    fail('docs/cloudflare/headers.ts must export isImmutableAssetPath()');
  } else {
    // Only an emitted fingerprint earns the immutable policy: a bare `_astro/`
    // path is not sufficient, or a hand-authored multi-dot asset would be
    // cached for a year with no way to revalidate.
    if (!isImmutableAssetPath('/ecn-sdk/_astro/index.DlwGQA8D.css')) {
      fail("isImmutableAssetPath('/ecn-sdk/_astro/index.DlwGQA8D.css') must be true");
    }
    if (isImmutableAssetPath('/ecn-sdk/_astro/foo.js')) {
      fail("isImmutableAssetPath('/ecn-sdk/_astro/foo.js') must be false (no fingerprint)");
    }
    if (isImmutableAssetPath('/ecn-sdk/index.html')) {
      fail("isImmutableAssetPath('/ecn-sdk/index.html') must be false");
    }
  }
}


for (const pathname of publicUrlPathnames) {
  if (!pathname.startsWith(`${documentationBasePath}/`) && pathname !== documentationBasePath) {
    fail(`manifest path escapes public mount: ${pathname}`);
    continue;
  }
  const candidates = diskCandidatesForPublicPath(pathname);
  let found = false;
  for (const candidate of candidates) {
    if (await pathExistsAsFile(candidate)) {
      found = true;
      break;
    }
  }
  if (!found) {
    fail(
      `public URL path missing on disk: ${pathname} `
      + `(looked for ${candidates.map((path) => relative(repoRoot, path)).join(', ')})`,
    );
  }
}


// Non-production hosts are derived from the approved docs host so this file never
// embeds an unapproved hostname literal, which the release address scan rejects.
// Use plain substring checks (not RegExp) so static analysis does not flag
// host matching as an incomplete hostname regular expression.
const documentationApex = documentationHost.split('.').slice(1).join('.');
const nonProductionSubdomains = ['dev', 'staging'];
const forbiddenHostSubstrings = [
  'localhost',
  '127.0.0.1',
  '0.0.0.0',
  '[::1]',
  '::1',
  ...nonProductionSubdomains.map((subdomain) => `${subdomain}.${documentationApex}`),
];

let htmlFiles = [];
let referenceFiles = [];
try {
  const allFiles = await filesBelow(siteRoot);
  htmlFiles = allFiles.filter((path) => path.endsWith('.html'));
  referenceFiles = allFiles.filter((path) => path.endsWith('.html') || path.endsWith('.css'));
} catch (error) {
  fail(`could not enumerate built HTML/CSS under ${siteContentDirectory}: ${error.message}`);
}

const base = documentationBase;
// Legion's own documentation is published on the same host, outside this mount.
// The guide links one reviewed page of it from the header, so that exact URL is
// admitted by value: every other off-mount reference on this host is still a
// page of this guide that escaped, and still fails.
const offMountReference = resolveLegionDocumentation().href;

function srcsetCandidates(value) {
  // srcset is a comma-separated list of "<url> [descriptor]" candidates; the
  // whole attribute is not a URL, so each candidate must be checked on its own.
  return value
    .split(',')
    .map((candidate) => candidate.trim().split(/\s+/, 1)[0])
    .filter(Boolean);
}

function htmlAttributeValue(match) {
  return match[2] ?? match[3] ?? match[4] ?? '';
}

function metaRefreshTarget(content) {
  const match = content.match(
    /^\s*[^;]*;\s*url\s*=\s*(?:"([^"]*)"|'([^']*)'|([\s\S]*?))\s*$/i,
  );
  return match ? (match[1] ?? match[2] ?? match[3] ?? '') : null;
}

function referenceRefs(source, isCss) {
  const refs = [];
  if (!isCss) {
    // This deliberately handles the URL-bearing attribute shapes emitted by
    // the site: double-quoted, single-quoted, and unquoted values (ending at
    // whitespace or `>`). It is not a general HTML tokenizer: it does not
    // decode character references or interpret script/style raw-text content.
    const attributePattern =
      /\b(href|src|srcset|poster|data-href)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/gi;
    for (const match of source.matchAll(attributePattern)) {
      const value = htmlAttributeValue(match);
      if (match[1].toLowerCase() === 'srcset') refs.push(...srcsetCandidates(value));
      else refs.push(value);
    }

    // `content` is URL-bearing only for a meta refresh. Match a complete,
    // conventional <meta> start tag (including `>` inside quoted values), then
    // associate http-equiv and content regardless of attribute order. Comments,
    // malformed tags, and browser error-recovery behavior remain out of scope.
    const metaPattern = /<meta\b(?:[^>"']|"[^"]*"|'[^']*')*>/gi;
    const metaAttributePattern =
      /\b(http-equiv|content)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/gi;
    for (const tagMatch of source.matchAll(metaPattern)) {
      const attributes = new Map();
      for (const attributeMatch of tagMatch[0].matchAll(metaAttributePattern)) {
        attributes.set(attributeMatch[1].toLowerCase(), htmlAttributeValue(attributeMatch));
      }
      if (attributes.get('http-equiv')?.trim().toLowerCase() !== 'refresh') continue;
      const target = metaRefreshTarget(attributes.get('content') ?? '');
      if (target !== null) refs.push(target);
    }
  }
  for (const match of source.matchAll(/\burl\(\s*(?:"([^"]*)"|'([^']*)'|([^'"\s)]+))\s*\)/gi)) {
    refs.push(match[1] ?? match[2] ?? match[3] ?? '');
  }
  if (isCss) {
    for (const match of source.matchAll(
      /@import\s+(?!url\(\s*)(?:"([^"]*)"|'([^']*)'|([^'"\s;]+))/gi,
    )) {
      refs.push(match[1] ?? match[2] ?? match[3] ?? '');
    }
  }
  return refs;
}

for (const file of referenceFiles) {
  const label = relative(siteRoot, file);
  // The URL this file is served from, so relative references resolve exactly
  // as a browser resolves them rather than being assumed in-mount.
  const deployedUrl = new URL(
    `${base}${label.split(sep).join('/')}`,
    canonicalOrigin,
  );
  let source;
  try {
    source = await readFile(file, 'utf8');
  } catch (error) {
    fail(`${label}: could not read built asset (${error.message})`);
    continue;
  }

  const refs = referenceRefs(source, file.endsWith('.css'));
  for (const ref of refs) {
    if (!ref || ref.startsWith('data:') || ref.startsWith('blob:') || ref.startsWith('mailto:')) {
      continue;
    }
    if (ref.startsWith('#')) continue;
    // Fragments identify a location within the one reviewed page; every other
    // URL component remains part of the by-value comparison.

    // Resolve every reference the way a browser would, from the URL this file
    // is served at, then enforce the mount on the resolved path. Resolution is
    // what closes the dot-segment bypasses (`/ecn-sdk/../asset.png`, its
    // percent-encoded spelling, and the canonical-host absolute form), which a
    // raw prefix test accepts.
    let resolved;
    try {
      resolved = new URL(ref, deployedUrl);
    } catch {
      fail(`${label}: reference is not a valid URL: ${ref}`);
      continue;
    }
    resolved.hash = '';
    if (
      resolved.hostname === deployedUrl.hostname
      && !resolved.pathname.startsWith(base)
      && resolved.href !== offMountReference
    ) {
      fail(
        `${label}: reference resolves outside ${base}: ${ref} → ${resolved.pathname}`,
      );
    }
    // Host-policy gate on live references only. Full-page substrings false-positive
    // on intentional mock bind-address examples (127.0.0.1 in code blocks).
    // Compare the RESOLVED host: a substring scan also inspects the path, so a
    // legitimate in-mount link such as `/ecn-sdk/how-to/troubleshooting/localhost/`
    // would fail the gate.
    const referencedHost = resolved.hostname.toLowerCase();
    for (const host of forbiddenHostSubstrings) {
      if (referencedHost === host.toLowerCase()) {
        fail(`${label}: built asset must not reference development host ${host} in ${ref}`);
      }
    }
  }
}

if (htmlFiles.length === 0 && failures.every((item) => !item.includes('enumerate'))) {
  fail('no built HTML pages found under site content directory');
}


if (failures.length) {
  throw new Error(
    `deploy contract check failed (${failures.length}):\n${failures.map((item) => `- ${item}`).join('\n')}`,
  );
}

console.log(
  `deploy contract check passed: entrypoint ${relative(repoRoot, entrypoint)}, `
  + `${publicUrlPathnames.length} public URL paths, ${htmlFiles.length} HTML pages, `
  + `${referenceFiles.length - htmlFiles.length} CSS files, `
  + 'CSP/header policy imported from docs/cloudflare/headers.ts',
);
