// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import { readdir, readFile, realpath, stat } from 'node:fs/promises';
import { resolve, sep } from 'node:path';
import { resolveLegionDocumentation } from './legion-documentation.mjs';
import { resolveVersionControl } from './version-control.mjs';

import {
  assetsOutputDirectory,
  assetsOutputRoot,
  documentationBase,
  documentationBasePath,
  documentationCanonicalBase,
  documentationOrigin,
  documentationWorkspaceRoot,
  repositoryRoot,
} from './site-config.mjs';

const arguments_ = process.argv.slice(2);
const requirePublicReachability = arguments_.includes('--require-public');
const siteArgument = arguments_.find((value) => !value.startsWith('--'));
const siteRoot = siteArgument ? resolve(repositoryRoot, siteArgument) : assetsOutputRoot;
const requestTimeoutMs = 10_000;
const maximumAttempts = 3;
const maximumRedirects = 4;
const repositoryOrigin = 'https://github.com';
const repositoryPrefix = '/picogrid/ecn-sdk-python';
const releaseTag = `v${JSON.parse(
  await readFile(resolve(documentationWorkspaceRoot, 'package.json'), 'utf8'),
).version}`;
/**
 * The Legion reference is a build input, so the allowlist admits the one target
 * this build resolved rather than a hard-coded URL. The resolver already pins
 * the reviewed origin and rejects credentials, queries, and paths outside a
 * named documentation root, so an injected value cannot widen what the guide
 * may link to.
 */
const reviewedPublicLinks = new Set([
  'https://github.com/picogrid/legion-system-auth/blob/9f618b7ce1648789d816a49b8fd0ec0ab21ea24a/README.md',
  'https://github.com/picogrid/legion-system-auth/blob/9f618b7ce1648789d816a49b8fd0ec0ab21ea24a/README.md#1-initial-setup',
  resolveLegionDocumentation().href,
  'https://man.fas.org/dod-101/sys/ship/weaps/docs/plicon.htm',
]);
/**
 * The guide advertises the immutable source it was built from. Admit that one
 * reference and nothing else, so a build cannot publish an arbitrary tag or
 * commit link that no longer describes the bytes a reader is looking at.
 */
const publishedSourceLink = resolveVersionControl({
  repository: `${repositoryOrigin}${repositoryPrefix}`,
}).href;
const reviewedProjectUrls = new Map([
  ['Homepage', 'https://github.com/picogrid/ecn-sdk-python'],
  ['Source', 'https://github.com/picogrid/ecn-sdk-python'],
  ['Issues', 'https://github.com/picogrid/ecn-sdk-python/issues'],
  ['Security', 'https://github.com/picogrid/ecn-sdk-python/security/policy'],
  ['Support', 'https://github.com/picogrid/ecn-sdk-python/blob/main/SUPPORT.md'],
  ['Changelog', 'https://github.com/picogrid/ecn-sdk-python/blob/main/CHANGELOG.md'],
]);

class LinkPolicyError extends Error {}

async function filesBelow(directory) {
  const files = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) files.push(...await filesBelow(path));
    else if (entry.isFile() && entry.name.endsWith('.html')) files.push(path);
  }
  return files;
}

function retryableStatus(status) {
  return status === 408 || status === 425 || status === 429 || status >= 500;
}

async function request(url, method) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), requestTimeoutMs);
  try {
    return await fetch(url, {
      method,
      redirect: 'manual',
      signal: controller.signal,
      headers: {
        'user-agent': 'Picogrid-ECN-SDK-documentation-link-check/1.0',
        ...(method === 'GET' ? { range: 'bytes=0-0' } : {}),
      },
    });
  } finally {
    clearTimeout(timer);
  }
}

function validateRemoteDestination(url, expected) {
  if (
    url.protocol !== 'https:'
    || url.username
    || url.password
    || /\/(?:login|signin|sso)(?:\/|$)/i.test(url.pathname)
  ) {
    throw new LinkPolicyError(`${url.href} is not an approved anonymous HTTPS destination`);
  }
  if (expected.exactUrls && !expected.exactUrls.has(url.href)) {
    throw new LinkPolicyError(`${url.href} is not in the reviewed public-link allowlist`);
  }
  if (expected.origin && url.origin !== expected.origin) {
    throw new LinkPolicyError(`${url.href} is outside its approved public origin`);
  }
  if (
    expected.pathPrefix
    && expected.pathPrefix !== '/'
    && url.pathname !== expected.pathPrefix
    && !url.pathname.startsWith(`${expected.pathPrefix}/`)
  ) {
    throw new LinkPolicyError(`${url.href} is outside its approved public path`);
  }
}

async function requestWithReviewedRedirects(url, method, expected) {
  let current = new URL(url);
  let redirectsFollowed = 0;
  while (true) {
    validateRemoteDestination(current, expected);
    const response = await request(current, method);
    if (![301, 302, 303, 307, 308].includes(response.status)) return response;
    const location = response.headers.get('location');
    await response.body?.cancel();
    if (!location) {
      throw new LinkPolicyError(`${current.href} returned a redirect without a location`);
    }
    if (redirectsFollowed >= maximumRedirects) {
      throw new LinkPolicyError(`${url} exceeded ${maximumRedirects} reviewed redirects`);
    }
    current = new URL(location, current);
    redirectsFollowed += 1;
  }
}

async function reachable(url, expected = {}) {
  let lastFailure = 'no response';
  let attemptsMade = 0;
  for (let attempt = 1; attempt <= maximumAttempts; attempt += 1) {
    attemptsMade = attempt;
    try {
      let response = await requestWithReviewedRedirects(url, 'HEAD', expected);
      if (response.status === 403 || response.status === 405) {
        await response.body?.cancel();
        response = await requestWithReviewedRedirects(url, 'GET', expected);
      }
      if (response.ok) {
        const finalUrl = new URL(response.url);
        await response.body?.cancel();
        validateRemoteDestination(finalUrl, expected);
        return;
      }
      await response.body?.cancel();
      lastFailure = `HTTP ${response.status}`;
      if (!retryableStatus(response.status)) break;
    } catch (error) {
      lastFailure = error instanceof Error ? error.message : 'request failed';
      if (error instanceof LinkPolicyError) break;
    }
    if (attempt < maximumAttempts) {
      await new Promise((done) => setTimeout(done, 250 * attempt));
    }
  }
  throw new Error(`${url} is unreachable after ${attemptsMade} bounded attempt(s) (${lastFailure})`);
}

async function validateRepositoryLink(link) {
  const url = new URL(link);
  if (url.pathname === repositoryPrefix) return;
  if (link === publishedSourceLink) return;
  const escapedPrefix = repositoryPrefix.replaceAll('/', '\\/');
  const escapedTag = releaseTag.replaceAll('.', '\\.');
  const match = url.pathname.match(
    new RegExp(`^${escapedPrefix}\/(?:blob|edit)\/(?:main|${escapedTag})\/(.+)$`),
  );
  if (!match) throw new Error(`${link} is not a supported repository source link`);
  const target = resolve(repositoryRoot, decodeURIComponent(match[1]));
  if (!target.startsWith(`${repositoryRoot}${sep}`)) {
    throw new Error(`${link} does not resolve to a released repository file`);
  }
  try {
    const resolvedTarget = await realpath(target);
    if (
      !resolvedTarget.startsWith(`${repositoryRoot}${sep}`)
      || !(await stat(resolvedTarget)).isFile()
    ) {
      throw new Error('unsafe repository target');
    }
  } catch {
    throw new Error(`${link} does not resolve to a released repository file`);
  }
}

/**
 * Map same-site absolute URLs into the built tree.
 * Default siteRoot is the Worker assets directory (site-dist/), so
 * https://docs.picogrid.com/ecn-sdk/... → site-dist/ecn-sdk/...
 * If the caller passes the content root (site-dist/ecn-sdk), strip the mount.
 */
function localPathForSameSiteUrl(url) {
  const pathname = decodeURIComponent(url.pathname);
  const assetsMarker = `${sep}${assetsOutputDirectory}`;
  const contentMarker = `${assetsMarker}${documentationBasePath.replaceAll('/', sep)}`;
  const normalizedRoot = siteRoot.endsWith(sep) ? siteRoot.slice(0, -1) : siteRoot;
  const contentRoot = normalizedRoot.endsWith(contentMarker)
    || normalizedRoot.endsWith(`${contentMarker.slice(1)}`);
  let relativePath = pathname;
  if (contentRoot) {
    if (
      relativePath !== documentationBasePath
      && !relativePath.startsWith(`${documentationBasePath}/`)
      && relativePath !== documentationBase.slice(0, -1)
    ) {
      throw new Error(`${url.href} is outside the documentation mount`);
    }
    relativePath = relativePath.slice(documentationBasePath.length).replace(/^\/+/, '');
  } else {
    relativePath = relativePath.replace(/^\/+/, '');
  }
  return resolve(siteRoot, relativePath);
}

async function validateSameSiteLink(link) {
  const url = new URL(link);
  const target = localPathForSameSiteUrl(url);
  if (target !== siteRoot && !target.startsWith(`${siteRoot}${sep}`)) {
    throw new Error(`${link} escapes the built documentation site`);
  }
  const candidates = url.pathname.endsWith('/')
    ? [resolve(target, 'index.html')]
    : [target, resolve(target, 'index.html')];
  for (const candidate of candidates) {
    try {
      if ((await stat(candidate)).isFile()) return;
    } catch {
      // Try the next deterministic local representation.
    }
  }
  throw new Error(`${link} does not resolve inside the built documentation site`);
}

async function projectMetadataUrls() {
  const configuration = await readFile(resolve(repositoryRoot, 'pyproject.toml'), 'utf8');
  const marker = '[project.urls]';
  const sectionStart = configuration.indexOf(marker);
  if (sectionStart === -1) throw new Error('pyproject.toml is missing [project.urls]');
  const remainder = configuration.slice(sectionStart + marker.length);
  const nextSection = remainder.search(/^\[/m);
  const section = nextSection === -1 ? remainder : remainder.slice(0, nextSection);
  const actual = new Map();
  for (const match of section.matchAll(/^([A-Za-z][A-Za-z0-9_-]*)\s*=\s*"([^"]+)"\s*$/gm)) {
    actual.set(match[1], new URL(match[2]).href.replace(/\/$/, ''));
  }
  if (
    actual.size !== reviewedProjectUrls.size
    || [...reviewedProjectUrls].some(([name, url]) => actual.get(name) !== url)
  ) {
    throw new Error('project URLs differ from the reviewed publication destinations');
  }
  return new Set(actual.values());
}

const htmlFiles = await filesBelow(siteRoot);
const absoluteLinks = new Set();
const canonicalOrigins = new Set();
const projectUrls = await projectMetadataUrls();

for (const file of htmlFiles) {
  const html = await readFile(file, 'utf8');
  for (const match of html.matchAll(/<link\b[^>]*\brel=(?:"canonical"|'canonical')[^>]*\bhref=(?:"([^"]+)"|'([^']+)')/gi)) {
    canonicalOrigins.add(new URL(match[1] ?? match[2]).origin);
  }
  for (const match of html.matchAll(/\bhref=(?:"([^"]+)"|'([^']+)')/gi)) {
    const href = match[1] ?? match[2];
    if (/^https:\/\//i.test(href)) absoluteLinks.add(new URL(href).href);
  }
}

if (canonicalOrigins.size !== 1 || !canonicalOrigins.has(documentationOrigin)) {
  throw new Error('documentation canonical origin does not match the reviewed public origin');
}

const sameSiteLinks = [];
const repositoryLinks = [];
const publicExternalLinks = [];
for (const link of [...absoluteLinks].sort()) {
  const url = new URL(link);
  // The guide shares its host with Legion's own documentation, so being on the
  // canonical origin is not enough to be a page of this site: only what is
  // under the mount resolves inside the built tree. Anything else on the host
  // is another published site, checked as a public external link.
  if (canonicalOrigins.has(url.origin) && url.pathname.startsWith(documentationBase)) {
    sameSiteLinks.push(link);
  } else if (url.origin === repositoryOrigin && (
    url.pathname === repositoryPrefix || url.pathname.startsWith(`${repositoryPrefix}/`)
  )) {
    repositoryLinks.push(link);
  } else {
    publicExternalLinks.push(link);
  }
}

if (publicExternalLinks.length === 0) {
  throw new Error('documentation has no independent public external link to verify');
}
for (const link of repositoryLinks) await validateRepositoryLink(link);
for (const link of sameSiteLinks) await validateSameSiteLink(link);
for (const link of publicExternalLinks) {
  await reachable(link, { exactUrls: reviewedPublicLinks });
}

if (requirePublicReachability) {
  // Repository-link structure and file existence were already checked against
  // this exact checkout above. One anonymous request for the release tag proves
  // both that the repository is public and that its immutable source identity
  // is available without turning hundreds of rendered source links into a
  // GitHub availability/load test.
  await reachable(`${repositoryOrigin}${repositoryPrefix}/tree/${releaseTag}`, {
    origin: repositoryOrigin,
    pathPrefix: repositoryPrefix,
  });
  await reachable(documentationCanonicalBase, {
    origin: documentationOrigin,
    pathPrefix: documentationBasePath,
  });
}

console.log(
  `external reachability check passed: ${publicExternalLinks.length} public external, `
  + `${repositoryLinks.length} repository references, ${sameSiteLinks.length} same-site local links`
  + (requirePublicReachability ? '; anonymous publication origins verified' : ''),
);
