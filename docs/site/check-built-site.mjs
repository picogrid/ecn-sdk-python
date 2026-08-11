// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import { createHash } from 'node:crypto';
import { readFile, readdir, stat } from 'node:fs/promises';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { resolveLegionDocumentation } from './legion-documentation.mjs';
import {
  maintainerOnlyDocumentationRoutes,
  publicGuideRoutes,
  pythonLeafRoutes,
} from './public-routes.mjs';
import {
  documentationBase,
  documentationCanonicalBase,
  documentationPagesHost,
  documentationWorkspaceRoot,
  repositoryRoot,
  siteContentRoot,
} from './site-config.mjs';
import { resolveVersionControl } from './version-control.mjs';

const root = siteContentRoot;
const base = documentationBase;
const canonicalBase = documentationCanonicalBase;
const repository = 'https://github.com/picogrid/ecn-sdk-python';
const retiredRepositoryPathPrefix = '/picogrid/ECN-SDK/';
const legion = resolveLegionDocumentation();
const packageMetadata = JSON.parse(
  await readFile(resolve(documentationWorkspaceRoot, 'package.json'), 'utf8'),
);
const version = packageMetadata.version;
const brandIconHref = `${base}brand/picogrid-app-icon-192.png`;
const printWordmarkHref = `${base}brand/picogrid-wordmark-light.png`;
const navTextureHref = `${base}brand/picogrid-nav-texture.png`;

// `text` is the explicit opt-out for deliberately unhighlighted content.
// `console` and `shellsession` are banned because Shiki does not tokenize
// prompt-less commands in those grammars.
const codeFenceLanguages = ['python', 'bash', 'json', 'text'];
const documentationRoot = documentationWorkspaceRoot;
// Discover documentation independently of the loader allowlist in
// docs/src/content.config.ts so the checker reports when they diverge.
const nonDocumentationTopLevelEntries = new Set([
  'site',
  'src',
  'cloudflare',
  'node_modules',
  '.astro',
  '.wrangler',
]);
// Keep this list aligned with the maintainer-only exclusions in docs/src/content.config.ts.
const unpublishedDocumentationSources = new Set([
  'README.md',
  join('reference', 'evidence-status.md'),
  join('reference', 'original-ecn-integration-parity.md'),
]);
const source = resolveVersionControl({ repository });

if (typeof version !== 'string' || !/^\d+\.\d+\.\d+$/.test(version)) {
  throw new Error('documentation package version is not a semantic version');
}

/**
 * The version the guide publishes is the one the SDK released under. They are
 * bumped together by release automation, but nothing in the site build would
 * notice if they drifted, so the release version is read from the distribution
 * metadata and compared here. `pyproject.toml` is the copy available to both the
 * repository and the immutable source snapshot the release verifier builds from;
 * the verifier separately ties it to the built wheel and the release policy.
 */
function projectVersion(toml) {
  const heading = '\n[project]\n';
  const start = `\n${toml}`.indexOf(heading);
  if (start < 0) throw new Error('pyproject.toml has no [project] table');
  const table = `\n${toml}`.slice(start + heading.length);
  const end = table.indexOf('\n[');
  return /^version = "([^"]+)"$/m.exec(end < 0 ? table : table.slice(0, end))?.[1];
}

const releaseVersion = projectVersion(
  await readFile(resolve(repositoryRoot, 'pyproject.toml'), 'utf8'),
);

if (!releaseVersion) {
  throw new Error('the released version is missing from the [project] table of pyproject.toml');
}
if (releaseVersion !== version) {
  throw new Error(
    `documentation version ${version} does not match released version ${releaseVersion}`,
  );
}

const guideRoutes = publicGuideRoutes;
const documentationRoutes = publicGuideRoutes;

const sectionTocExpectations = [
  ['how-to/check-clock', [
    '#check-a-saved-profile',
    '#use-the-typed-client',
    '#diagnostic-boundary',
  ]],
  ['getting-started/preflight', [
    '#run-preflight',
    '#probe-one-exact-subscription',
    '#authorization-scope',
  ]],
  ['concepts/mqtt-wire', [
    '#connection-and-subscription-lifecycle',
    '#confirmed-topic-families',
    '#bounded-topic-access',
  ]],
  ['concepts/locations', [
    '#location-and-motion-model',
    '#position-location-information-pli',
    '#observed-location-state',
    '#ecn-location-observation',
  ]],
  ['quickstarts/observe-data', [
    '#1-install-and-validate',
    '#2-watch-one-category',
    '#3-observe-location-state',
  ]],
  ['integrations/sensors', [
    '#model-the-sensor',
    '#publish-updates',
    '#validate-and-observe',
  ]],
  ['how-to/pli-entities', [
    '#validate-offline',
    '#publish-an-entity',
    '#publish-a-location',
    '#publish-a-location-as-pli',
    '#interpret-publication-receipts',
  ]],
  ['shipped-tooling', ['#runnable-python-examples', '#operator-application']],
  ['walkthroughs/tactical-live-map', ['#operator-walkthrough', '#production-use-checklist']],
  ['reference/wire-formats', ['#topic-families', '#qos-and-delivery', '#protobuf']],
];

async function filesBelow(directory, excludedEntries) {
  const members = [];
  for (const name of await readdir(directory)) {
    if (excludedEntries?.has(name)) continue;
    const path = join(directory, name);
    const metadata = await stat(path);
    if (metadata.isDirectory()) members.push(...await filesBelow(path));
    else members.push(path);
  }
  return members;
}

function sourceFenceFailures(path, source) {
  const problems = [];
  const lines = source.split(/\r?\n/);
  const label = relative(repositoryRoot, path);
  let fence = null;
  let inFrontmatter = lines[0]?.trim() === '---';

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (inFrontmatter) {
      if (index > 0 && /^(?:---|\.\.\.)\s*$/.test(line.trim())) inFrontmatter = false;
      continue;
    }

    if (fence) {
      const closing = line.match(/^\s*(`{3,}|~{3,})\s*$/);
      if (
        closing
        && closing[1][0] === fence.marker
        && closing[1].length >= fence.length
      ) {
        fence = null;
      }
      continue;
    }

    const opening = line.match(/^\s*(`{3,}|~{3,})(.*)$/);
    if (!opening) continue;
    fence = { marker: opening[1][0], length: opening[1].length };
    const info = opening[2].trim();
    if (!info) {
      problems.push(`${label}:${index + 1}: fenced code block is missing a language`);
      continue;
    }
    const language = info.split(/\s+/, 1)[0];
    if (!codeFenceLanguages.includes(language)) {
      problems.push(`${label}:${index + 1}: fenced code block uses unsupported language "${language}"`);
    }
  }

  return problems;
}

function codeBlocks(html) {
  return [...html.matchAll(/<pre\b([^>]*)>([\s\S]*?)<\/pre>/gi)].map((match) => {
    const languageMatch = match[1].match(/\bdata-language=(?:"([^"]*)"|'([^']*)')/i);
    return {
      html: match[2],
      language: languageMatch?.[1] ?? languageMatch?.[2] ?? null,
    };
  });
}

function themeTokenColors(block, themeIndex) {
  const colors = new Set();
  const variable = new RegExp(`(?:^|;)\\s*--${themeIndex}:\\s*(#[0-9a-f]{6})\\b`, 'i');
  for (const match of block.html.matchAll(/<span\b[^>]*\bstyle=(?:"([^"]*)"|'([^']*)')[^>]*>/gi)) {
    const color = (match[1] ?? match[2]).match(variable)?.[1];
    if (color) colors.add(color.toLowerCase());
  }
  return colors;
}

function mostColorfulBlock(blocks, language) {
  let selected = null;
  let selectedCounts = [-1, -1];
  for (const block of blocks.filter((candidate) => candidate.language === language)) {
    const counts = [0, 1].map((themeIndex) => themeTokenColors(block, themeIndex).size);
    const score = Math.min(...counts);
    const selectedScore = Math.min(...selectedCounts);
    if (
      !selected
      || score > selectedScore
      || (score === selectedScore && counts[0] + counts[1] > selectedCounts[0] + selectedCounts[1])
    ) {
      selected = block;
      selectedCounts = counts;
    }
  }
  return { block: selected, counts: selected ? selectedCounts : [0, 0] };
}

function localTarget(href, source) {
  const clean = href.split('#', 1)[0].split('?', 1)[0];
  if (href.startsWith('#')) return source;
  if (!clean || /^(?:[a-z]+:|#|\/\/)/i.test(clean)) return null;
  if (clean.startsWith(base)) return resolve(root, clean.slice(base.length));
  if (clean.startsWith('/')) return null;
  return resolve(dirname(source), clean);
}

function localFragment(href) {
  const boundary = href.indexOf('#');
  if (boundary === -1) return null;
  const encoded = href.slice(boundary + 1).split('?', 1)[0];
  if (!encoded) return null;
  try {
    return decodeURIComponent(encoded);
  } catch {
    return encoded;
  }
}

function isUnpublishedSourceLink(href) {
  const clean = href.split('#', 1)[0].split('?', 1)[0];
  return !/^(?:[a-z]+:|\/\/)/i.test(clean) && /\.(?:md|mdx|py)$/i.test(clean);
}

function isRootRelativeOutsideBase(href) {
  const clean = href.split('#', 1)[0].split('?', 1)[0];
  return clean.startsWith('/') && !clean.startsWith('//') && !clean.startsWith(base);
}

function isWithinRoot(target) {
  const path = relative(root, target);
  return path === '' || (path !== '..' && !path.startsWith(`..${sep}`) && !isAbsolute(path));
}

const directoryMembers = new Map();
async function hasExactPathCase(target) {
  if (!isWithinRoot(target)) return false;
  const path = relative(root, target);
  if (!path) return true;

  let directory = root;
  for (const part of path.split(sep)) {
    try {
      if (!directoryMembers.has(directory)) {
        directoryMembers.set(directory, await readdir(directory));
      }
      if (!directoryMembers.get(directory).includes(part)) return false;
    } catch {
      return false;
    }
    directory = join(directory, part);
  }
  return true;
}

async function siteFile(target) {
  const candidates = [target, `${target}.html`, join(target, 'index.html')];
  for (const candidate of candidates) {
    try {
      if (await hasExactPathCase(candidate) && (await stat(candidate)).isFile()) return candidate;
    } catch {
      // Continue through the finite candidate set.
    }
  }
  return null;
}

function routeFile(route) {
  return route ? join(root, route, 'index.html') : join(root, 'index.html');
}

function tableOfContents(html) {
  return html.match(/<starlight-toc\b[\s\S]*?<\/starlight-toc>/i)?.[0] ?? null;
}

function tableOfContentsLinks(toc) {
  return [...toc.matchAll(/\bhref=(?:"([^"]+)"|'([^']+)')/gi)]
    .map((match) => match[1] ?? match[2]);
}

function linkWithRelationship(html, relationship, href) {
  return [...html.matchAll(/<a\b[^>]*>/gi)].some((match) => (
    new RegExp(`\\brel=(?:"${relationship}"|'${relationship}')`, 'i').test(match[0])
    && new RegExp(`\\bhref=(?:"${href}"|'${href}')`, 'i').test(match[0])
  ));
}

/**
 * A link a reader can see and read, rather than one named only to a screen
 * reader. The text may be followed by an icon or a note as well as by the end of
 * the link, so it is required to end at a tag rather than at `</a>`.
 */
function linkWithVisibleText(html, href, text) {
  return [...html.matchAll(/<a\b[^>]*>[\s\S]*?<\/a>/gi)].some((match) => (
    match[0].includes(`href="${href}"`)
    && match[0].includes(`>${text}<`)
    && !match[0].includes('sr-only')
  ));
}

function linkWithLabel(html, href, label) {
  return [...html.matchAll(/<a\b[^>]*>[\s\S]*?<\/a>/gi)].some((match) => (
    match[0].includes(`href="${href}"`)
    && match[0].includes(`>${label}</span>`)
  ));
}

function cssBlock(css, selector) {
  // Attribute selectors are quoted either way depending on the formatter, and
  // which one the stylesheet happens to use is not what this gate measures.
  const pattern = new RegExp(
    selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/['"]/g, `['"]`),
  );
  const start = css.search(pattern);
  const opening = css.indexOf('{', start);
  const closing = css.indexOf('}', opening);
  if (start === -1 || opening === -1 || closing === -1) {
    throw new Error(`theme selector is missing: ${selector}`);
  }
  return css.slice(opening + 1, closing);
}

function oklchProperty(block, property) {
  const escaped = property.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = block.match(new RegExp(
    `${escaped}:\\s*oklch\\(\\s*([0-9.]+)\\s+([0-9.]+)\\s+([0-9.]+)\\s*\\)`,
  ));
  if (!match) throw new Error(`theme color is missing: ${property}`);
  return match.slice(1).map(Number);
}

function relativeLuminance([lightness, chroma, hue]) {
  const angle = hue * Math.PI / 180;
  const a = chroma * Math.cos(angle);
  const b = chroma * Math.sin(angle);
  const lPrime = lightness + 0.3963377774 * a + 0.2158037573 * b;
  const mPrime = lightness - 0.1055613458 * a - 0.0638541728 * b;
  const sPrime = lightness - 0.0894841775 * a - 1.291485548 * b;
  const l = lPrime ** 3;
  const m = mPrime ** 3;
  const s = sPrime ** 3;
  const channels = [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ].map((value) => Math.min(1, Math.max(0, value)));
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function contrast(first, second) {
  const firstLuminance = relativeLuminance(first);
  const secondLuminance = relativeLuminance(second);
  return (
    (Math.max(firstLuminance, secondLuminance) + 0.05)
    / (Math.min(firstLuminance, secondLuminance) + 0.05)
  );
}

const allFiles = await filesBelow(root);
const htmlFiles = allFiles.filter((path) => path.endsWith('.html'));
if (htmlFiles.length < 20) throw new Error('documentation build produced too few pages');

const failures = [];
const externalLinks = new Set();
const htmlCache = new Map();
const contentCache = new Map();
/**
 * Blank the body of every inline script and style, leaving their tags and
 * attributes in place.
 *
 * Everything below asks what the page says, and a bundler decides on its own
 * whether a script is a file or is inlined into the markup. A theme's own
 * script mentions the class names and selectors it operates on, so a check
 * written against the page can silently start matching a position inside
 * JavaScript when a bundling threshold moves. Blanking is length-preserving so
 * that checks comparing positions stay valid.
 */
function withoutInlineAssets(html) {
  return html.replace(
    /(<(script|style)\b[^>]*>)([\s\S]*?)(<\/\2>)/gi,
    (_match, open, _tag, body, close) => `${open}${' '.repeat(body.length)}${close}`,
  );
}

/** The page exactly as published, including whatever the bundler inlined. */
async function siteSource(file) {
  if (!htmlCache.has(file)) htmlCache.set(file, await readFile(file, 'utf8'));
  return htmlCache.get(file);
}

const documentationFiles = (await filesBelow(
  documentationRoot,
  nonDocumentationTopLevelEntries,
))
  .filter((path) => path.endsWith('.md'))
  .filter((path) => !unpublishedDocumentationSources.has(relative(documentationRoot, path)));
for (const file of documentationFiles) {
  failures.push(...sourceFenceFailures(file, await readFile(file, 'utf8')));
  const sourcePath = relative(documentationRoot, file);
  const routeParts = sourcePath.slice(0, -'.md'.length).split(sep);
  if (routeParts.at(-1) === 'index') routeParts.pop();
  if (!(await hasExactPathCase(routeFile(routeParts.join(sep))))) {
    failures.push(
      `${relative(repositoryRoot, file)}: loader in docs/src/content.config.ts does not cover this documentation source`,
    );
  }
}

let builtCodeBlockCount = 0;
let builtBashBlockCount = 0;
for (const file of htmlFiles) {
  const label = relative(root, file);
  for (const block of codeBlocks(await siteHtml(file))) {
    builtCodeBlockCount += 1;
    if (block.language === null) {
      failures.push(`${label}: rendered code block declares no language`);
    } else {
      if (block.language === 'bash') builtBashBlockCount += 1;
      if (!codeFenceLanguages.includes(block.language)) {
        failures.push(`${label}: rendered code block uses unsupported language "${block.language}"`);
      }
    }
  }
}
// The expected build has about 319 rendered blocks; 250 catches an empty or
// truncated render while leaving ample room for normal documentation edits.
if (builtCodeBlockCount < 250) {
  failures.push(`built site contains implausibly few rendered code blocks: ${builtCodeBlockCount}`);
}
if (builtBashBlockCount === 0) {
  failures.push('built site contains no language-tagged bash code blocks');
}

// Representative authored and generated pages must each render at least one
// genuinely multi-colored block in both the dark (`--0`) and light (`--1`)
// theme. Selecting the most colorful block rather than a positional one keeps
// the check stable when the reference generator emits one-line signatures.
for (const [route, language] of [
  ['getting-started/installation', 'bash'],
  ['how-to/check-clock', 'python'],
  ['reference/python/client/ecn-client', 'python'],
]) {
  const blocks = codeBlocks(await siteHtml(routeFile(route)));
  const { block, counts } = mostColorfulBlock(blocks, language);
  if (!block) {
    failures.push(`${route}: expected at least one rendered ${language} code block`);
    continue;
  }
  for (const themeIndex of [0, 1]) {
    if (counts[themeIndex] < 4) {
      failures.push(
        `${route}: ${language} code highlighting has ${counts[themeIndex]} distinct theme --${themeIndex} colors; expected at least 4`,
      );
    }
  }
}

/** The page as a reader receives it, which is what almost every check means. */
async function siteHtml(file) {
  if (!contentCache.has(file)) {
    contentCache.set(file, withoutInlineAssets(await siteSource(file)));
  }
  return contentCache.get(file);
}

if (
  !(await hasExactPathCase(routeFile('getting-started/installation')))
  || await hasExactPathCase(routeFile('Getting-Started/Installation'))
) {
  failures.push('built-site path validation does not enforce exact route casing');
}

for (const route of maintainerOnlyDocumentationRoutes) {
  if (await hasExactPathCase(routeFile(route))) {
    failures.push(`${route}: maintainer-only record leaked into the public site`);
  }
}

if (await hasExactPathCase(routeFile('concepts/pli'))) {
  failures.push('redundant standalone PLI concept route was published');
}

for (const file of htmlFiles) {
  const html = await siteHtml(file);
  const label = relative(root, file);
  if (html.includes(documentationPagesHost)) {
    failures.push(`${label}: canonical host must be docs.picogrid.com, not Pages`);
  }
  if (html.includes(retiredRepositoryPathPrefix)) {
    failures.push(`${label}: contains retired repository path ${retiredRepositoryPathPrefix}`);
  }
  for (const stale of [
    /Picogrid ECN Client/i,
    /\bpublic-safe\b/i,
    /\bsuccessor\b/i,
    /\bpinned guide\b/i,
  ]) {
    if (stale.test(html)) failures.push(`${label}: contains stale public product language ${stale}`);
  }
  for (const required of [
    /<html[^>]+lang="[^"]+"/i,
    /<title>[^<]+<\/title>/i,
    /<meta[^>]+name="description"/i,
    /<main\b/i,
  ]) {
    if (!required.test(html)) failures.push(`${label}: missing required accessible metadata`);
  }
  for (const match of html.matchAll(/<img\b([^>]*)>/gi)) {
    if (!/\balt=(?:"[^"]*"|'[^']*')/i.test(match[1])) {
      failures.push(`${label}: image is missing alt text`);
    }
  }
  for (const match of html.matchAll(/\bhref=(?:"([^"]+)"|'([^']+)')/gi)) {
    const href = match[1] ?? match[2];
    const target = localTarget(href, file);
    if (/^https?:\/\//i.test(href)) {
      try {
        const url = new URL(href);
        externalLinks.add(url.href);
        if (url.protocol !== 'https:' || url.username || url.password) {
          failures.push(`${label}: external link is not credential-free HTTPS ${href}`);
        }
      } catch {
        failures.push(`${label}: external link is invalid ${href}`);
      }
    } else if (isRootRelativeOutsideBase(href)) {
      failures.push(`${label}: root-relative link escapes configured base ${href}`);
    } else if (isUnpublishedSourceLink(href)) {
      failures.push(`${label}: source link was not rewritten ${href}`);
    } else if (target && !isWithinRoot(target)) {
      failures.push(`${label}: link escapes built site`);
    } else if (target) {
      const resolvedFile = await siteFile(target);
      if (!resolvedFile) {
        failures.push(`${label}: broken local link ${href}`);
      } else {
        const fragment = localFragment(href);
        if (fragment && resolvedFile.endsWith('.html')) {
          const targetHtml = await siteHtml(resolvedFile);
          if (!targetHtml.includes(`id="${fragment}"`) && !targetHtml.includes(`id='${fragment}'`)) {
            failures.push(`${label}: broken local fragment ${href}`);
          }
        }
      }
    }
  }
}

for (const route of guideRoutes) {
  const file = routeFile(route);
  const label = route || 'guide home';
  let html;
  try {
    html = await siteHtml(file);
  } catch {
    failures.push(`${label}: expected guide route is missing`);
    continue;
  }
  const canonical = `${canonicalBase}${route ? `${route}/` : ''}`;
  if (!html.includes(`<link rel="canonical" href="${canonical}"`)) {
    failures.push(`${label}: canonical metadata is missing or incorrect`);
  }
  if (!html.includes(`<meta property="og:url" content="${canonical}"`)) {
    failures.push(`${label}: Open Graph URL is missing or incorrect`);
  }
  if (!/<meta property="og:(?:title|description)" content="[^"]+"/i.test(html)) {
    failures.push(`${label}: Open Graph title or description is missing`);
  }
  if (!html.includes(`<meta property="og:image" content="${canonicalBase}brand/ecn-client-og.png"`)) {
    failures.push(`${label}: Open Graph image is missing or incorrect`);
  }
  if (!html.includes(`<meta name="version" content="${version}"`)) {
    failures.push(`${label}: version metadata does not match package.json`);
  }
  if (!html.includes(`<meta name="source-ref" content="${source.reference}"`)) {
    failures.push(`${label}: source reference metadata is missing or incorrect`);
  }
  if (
    source.commit
    && !html.includes(`<meta name="source-commit" content="${source.commit}"`)
  ) {
    failures.push(`${label}: source commit metadata is missing or incorrect`);
  }
  if (!source.commit && html.includes('<meta name="source-commit"')) {
    failures.push(`${label}: source commit metadata was published without a resolved commit`);
  }
  if (!linkWithVisibleText(html, source.href, source.referenceLabel)) {
    failures.push(`${label}: footer is missing the link to the published source`);
  }
  if (
    !new RegExp(
      String.raw`<link\b(?=[^>]*\brel="(?:shortcut )?icon")(?=[^>]*\bhref="${brandIconHref.replaceAll('/', String.raw`\/`)}")(?=[^>]*\btype="image\/png")[^>]*>`,
      'i',
    ).test(html)
  ) {
    failures.push(`${label}: approved Picogrid favicon is missing`);
  }
}

for (const route of documentationRoutes) {
  const html = await siteHtml(routeFile(route));
  const label = route || 'guide home';
  const h1Tags = [...html.matchAll(/<h1\b[^>]*>/gi)];
  if (
    h1Tags.length !== 1
    || !/\bid=(?:"_top"|'_top')/i.test(h1Tags[0][0])
  ) {
    failures.push(`${label}: expected exactly one top-level page title`);
  }
  const toc = tableOfContents(html);
  if (toc && !tableOfContentsLinks(toc).some((href) => href !== '#_top')) {
    failures.push(`${label}: table of contents exposes only synthetic Overview`);
  }
  // The outline below the two-column breakpoint is withdrawn in favour of the
  // trail, so no page may open with a disclosure between its title and its
  // first paragraph, and none may carry the script that drove one.
  if (/<mobile-starlight-toc\b/i.test(html) || html.includes('starlight__mobile-toc')) {
    failures.push(`${label}: publishes the withdrawn On this page panel`);
  }
  // The front page is where a trail would start, so it must publish none;
  // every other page must carry the wayfinding the panel no longer gives.
  const publishesTrail = /<nav[^>]*aria-label="Breadcrumb"/i.test(html);
  if (!route && publishesTrail) {
    failures.push(`${label}: publishes a trail even though it is the trail's starting point`);
  } else if (route && !publishesTrail) {
    failures.push(`${label}: does not publish the trail that replaced it`);
  }
}

for (const [route, expectedHeadings] of sectionTocExpectations) {
  const html = await siteHtml(routeFile(route));
  const toc = tableOfContents(html);
  if (!toc) {
    failures.push(`${route}: table of contents is missing`);
    continue;
  }
  for (const heading of expectedHeadings) {
    if (!toc.includes(`href="${heading}"`)) {
      failures.push(`${route}: table of contents is missing real section ${heading}`);
    }
  }
}

const navigationHtml = await siteHtml(routeFile('getting-started/configuration'));
// Scoped to the sidebar itself: the page body legitimately links to generated
// symbols, and those links are one of the reachability paths this structure
// relies on, so they must not read as sidebar entries.
const sidebarHtml = elementWithId(navigationHtml, 'starlight__sidebar');
if (!sidebarHtml) failures.push('navigation page does not publish the sidebar');
const leafRouteSet = new Set(pythonLeafRoutes);
for (const route of guideRoutes) {
  if (leafRouteSet.has(route)) continue;
  const href = `${base}${route ? `${route}/` : ''}`;
  if (!sidebarHtml.includes(`href="${href}"`)) {
    failures.push(`sidebar is missing guide route ${href}`);
  }
}
// Leaf symbol pages are reachable from their group index, search, and their
// stable routes; listing every one of them is the navigation noise this
// structure removes, so their absence is part of the contract.
for (const route of pythonLeafRoutes) {
  if (sidebarHtml.includes(`href="${base}${route}/"`)) {
    failures.push(`sidebar should not list leaf reference route ${route}`);
  }
}
for (const label of [
  'Install and authenticate',
  'Quickstarts',
  'Core concepts',
  'Sensor integration',
  'Effector integration',
  'Operator workflows',
  'Security and credentials',
  'API reference',
  'Deployment and support',
]) {
  if (!navigationHtml.includes(`>${label}</span>`)) {
    failures.push(`sidebar is missing product journey ${label}`);
  }
}
for (const [label, href] of [
  ['Observe ECN data', `${base}quickstarts/observe-data/`],
  ['Build a sensor publisher', `${base}quickstarts/sensor-publisher/`],
  ['Build an effector task handler', `${base}quickstarts/effector-handler/`],
  ['Run the operator view', `${base}quickstarts/operator-view/`],
  ['MQTT topics and delivery', `${base}concepts/mqtt-wire/`],
]) {
  if (!linkWithLabel(navigationHtml, href, label)) {
    failures.push(`sidebar is missing product route ${label}`);
  }
}
for (const [label, href] of [['Source repository', repository]]) {
  if (!navigationHtml.includes(`>${label}</span>`) || !navigationHtml.includes(`href="${href}"`)) {
    failures.push(`header is missing the ${label} link`);
  }
}

// Picogrid's two documentation sets are both named in the brand row as visible
// text, and the reader is told which one they are in.
const documentationSetsHtml = navigationHtml
  .match(/<nav[^>]*aria-label="Documentation"[\s\S]*?<\/nav>/i)?.[0] ?? '';
if (!documentationSetsHtml) {
  failures.push('header does not publish the documentation sets as a navigation landmark');
}
for (const [label, href] of [[legion.label, legion.href], ['ECN SDK', base]]) {
  if (!linkWithVisibleText(documentationSetsHtml, href, label)) {
    failures.push(`header is missing the visibly labelled ${label} documentation link`);
  }
}
if (!/aria-current="true"/.test(documentationSetsHtml)) {
  failures.push('header does not mark the documentation set being read');
}

// Below the flyout breakpoint the band gives its first row to the page being
// read, so a reader who has scrolled away from the title still has it, and the
// second row to search, which the theme otherwise collapses to an icon.
const currentPageHtml = navigationHtml
  .match(/<p[^>]*class="current-page[^"]*"[\s\S]*?<\/p>/i)?.[0] ?? '';
if (!currentPageHtml.includes('Configure a connection')) {
  failures.push('header does not name the page being read beside the menu button');
}
if (!/<button[^>]*data-open-modal/i.test(navigationHtml)) {
  failures.push('header is missing the search control');
}

// Below the flyout breakpoint the band keeps only the page and its controls, so
// the guide's own pages are published in the theme's menu pane, which every page
// has, around the version and both documentation sets.
const landingPageHtml = await siteHtml(routeFile(''));
function elementWithId(html, id) {
  const opening = new RegExp(`<([a-z][\\w:-]*)\\b[^>]*\\bid=(?:"${id}"|'${id}')[^>]*>`, 'i').exec(html);
  if (!opening) return '';
  const tagName = opening[1];
  const tags = new RegExp(`<\\/?${tagName}\\b[^>]*>`, 'gi');
  tags.lastIndex = opening.index;
  let depth = 0;
  for (let tag = tags.exec(html); tag; tag = tags.exec(html)) {
    if (tag[0].startsWith('</')) {
      depth -= 1;
      if (depth === 0) return html.slice(opening.index, tags.lastIndex);
    } else if (!tag[0].endsWith('/>')) {
      depth += 1;
    }
  }
  return '';
}

function elementWithClass(html, className) {
  for (const opening of html.matchAll(/<([a-z][\w:-]*)\b[^>]*>/gi)) {
    const classAttribute = /\bclass=(?:"([^"]*)"|'([^']*)')/i.exec(opening[0]);
    const classes = (classAttribute?.[1] ?? classAttribute?.[2] ?? '').split(/\s+/);
    if (!classes.includes(className)) continue;

    const tagName = opening[1];
    const tags = new RegExp(`<\\/?${tagName}\\b[^>]*>`, 'gi');
    tags.lastIndex = opening.index;
    let depth = 0;
    for (let tag = tags.exec(html); tag; tag = tags.exec(html)) {
      if (tag[0].startsWith('</')) {
        depth -= 1;
        if (depth === 0) return html.slice(opening.index, tags.lastIndex);
      } else if (!tag[0].endsWith('/>')) {
        depth += 1;
      }
    }
    return '';
  }
  return '';
}
const flyoutOf = (html) => elementWithClass(html, 'mobile-nav');
const flyouts = [
  ['overview', flyoutOf(landingPageHtml)],
  ['guide pages', flyoutOf(navigationHtml)],
];
for (const [where, flyoutHtml] of flyouts) {
  if (!flyoutHtml) {
    failures.push(`${where} do not publish the navigation flyout`);
    continue;
  }
  for (const [label, href] of [[legion.label, legion.href], ['ECN SDK', base]]) {
    if (!linkWithVisibleText(flyoutHtml, href, label)) {
      failures.push(`${where} flyout is missing the visibly labelled ${label} documentation link`);
    }
  }
  if (!linkWithVisibleText(flyoutHtml, source.href, source.versionLabel)) {
    failures.push(`${where} flyout is missing the released version`);
  }
  // The flyout mirrors the sidebar, so it carries the same contract: every
  // navigable route, and none of the generated leaves that the group indexes
  // inventory instead.
  for (const route of guideRoutes) {
    if (leafRouteSet.has(route)) continue;
    const href = `${base}${route ? `${route}/` : ''}`;
    if (!flyoutHtml.includes(`href="${href}"`)) {
      failures.push(`${where} flyout is missing guide route ${href}`);
    }
  }
  for (const route of pythonLeafRoutes) {
    if (flyoutHtml.includes(`href="${base}${route}/"`)) {
      failures.push(`${where} flyout should not list leaf reference route ${route}`);
    }
  }
  if (!flyoutHtml.includes('Install and authenticate')) {
    failures.push(`${where} flyout does not group the pages under their headings`);
  }
}

// The overview answers what a reader is trying to build before it explains the
// boundary they are building inside: four outcomes, each naming the quickstart
// that reaches it, between the page's one call to action and its prose.
const outcomesHtml = landingPageHtml.match(/<ul[^>]*class="outcomes[^"]*"[\s\S]*?<\/ul>/i)?.[0] ?? '';
if (!outcomesHtml) {
  failures.push('overview does not publish the outcomes a reader can start from');
}
for (const [outcome, label, route] of [
  ['Observe tracks, detections, or locations', 'Observe ECN data', 'quickstarts/observe-data'],
  ['Connect a sensor', 'Build a sensor publisher', 'quickstarts/sensor-publisher'],
  ['Connect an effector', 'Build an effector task handler', 'quickstarts/effector-handler'],
  ['Run a tactical display', 'Run the operator view', 'quickstarts/operator-view'],
]) {
  if (!outcomesHtml.includes(outcome)) {
    failures.push(`overview does not offer the ${outcome} outcome`);
  }
  if (!linkWithVisibleText(outcomesHtml, `${base}${route}/`, label)) {
    failures.push(`overview outcome is missing the visibly labelled ${label} quickstart`);
  }
}
// Matched as markup rather than as a bare class name, so the position is the
// container's and not some other mention of the same name.
const prosePosition = /<div[^>]*class="[^"]*\bsl-markdown-content\b/i.exec(landingPageHtml)?.index;
if (prosePosition === undefined) {
  failures.push('overview does not publish its prose in a content container');
} else if (landingPageHtml.indexOf('class="outcomes') > prosePosition) {
  failures.push('overview outcomes are published below the page prose');
}

// Every page says how it was reached, from the front page through the heading
// the sidebar files it under. The trail is published with the page, above its
// title, at every width, so a page carries exactly one.
const trails = navigationHtml.match(/<nav[^>]*aria-label="Breadcrumb"[\s\S]*?<\/nav>/gi) ?? [];
if (trails.length !== 1) {
  failures.push('a guide page does not carry exactly one trail above its title');
}
for (const trailHtml of trails) {
  if (!trailHtml.includes(`href="${base}"`)) {
    failures.push('trail does not lead back to the front page of the guide');
  }
  if (!trailHtml.includes('Install and authenticate')) {
    failures.push('trail does not name the heading the page is filed under');
  }
  if (!/aria-current="page"[^>]*>Configure a connection</.test(trailHtml)) {
    failures.push('trail does not end on the page being read');
  }
}

// The version menu names the release and links it to the source it was built
// from; the colour-scheme menu offers the three choices by name.
if (!/<div[^>]*role="menu"[^>]*aria-label="Version"/i.test(navigationHtml)) {
  failures.push('header is missing the version menu');
}
if (!linkWithVisibleText(navigationHtml, source.href, source.versionLabel)) {
  failures.push('header is missing the released version linked to its repository source');
}
// What changed between releases is asked of the version control, so the menu
// that names the release carries the changelog rather than a row of its own.
const versionMenuHtml = navigationHtml
  .match(/<picogrid-version-menu[\s\S]*?<\/picogrid-version-menu>/i)?.[0] ?? '';
if (!linkWithVisibleText(versionMenuHtml, `${base}changelog/`, 'Changelog')) {
  failures.push('version menu is missing the changelog');
}
const colourSchemeHtml = navigationHtml
  .match(/<picogrid-color-scheme[\s\S]*?<\/picogrid-color-scheme>/i)?.[0] ?? '';
if (!/<div[^>]*role="menu"[^>]*aria-label="Color Scheme"/i.test(colourSchemeHtml)) {
  failures.push('header is missing the colour scheme menu');
}
for (const [scheme, label] of [['auto', 'System'], ['light', 'Light'], ['dark', 'Dark']]) {
  if (
    !colourSchemeHtml.includes(`data-scheme-value="${scheme}"`)
    || !colourSchemeHtml.includes(`>${label}<`)
  ) {
    failures.push(`colour scheme menu is missing the ${label} choice`);
  }
}
if ([...colourSchemeHtml.matchAll(/role="menuitemradio"/g)].length !== 3) {
  failures.push('colour scheme menu does not publish exactly the three schemes');
}
// The footer states the version and links the source it was built from. Read
// the element rather than the page, so the check does not depend on where the
// anchor happens to sit in the surrounding whitespace.
const publishedSource = navigationHtml.match(
  /<p[^>]+class="[^"]*\bdocumentation-source\b[^"]*"[^>]*>(?<summary>[^<]*)<a[^>]+href="(?<href>[^"]+)"[^>]*>(?<label>[^<]*)<\/a>/,
)?.groups;
if (
  !publishedSource
  || !publishedSource.summary.includes(`Version ${version}`)
  || !publishedSource.summary.includes(source.sourceKind)
  || publishedSource.href !== source.href
  || publishedSource.label.trim() !== source.referenceLabel
) {
  failures.push('footer is missing the published version and source reference');
}
for (const required of [
  /<a[^>]+class="sl-skip-link[^>]+href="#_top"/i,
  /<nav[^>]+aria-label="Main"/i,
  /<button[^>]+aria-expanded="false"[^>]+aria-label="Menu"[^>]+aria-controls="starlight__sidebar"/i,
  /<site-search\b/i,
  /<button[^>]+data-open-modal[^>]+aria-label="Search"/i,
  /<dialog[^>]+aria-label="Search"/i,
]) {
  if (!required.test(navigationHtml)) {
    failures.push('responsive navigation or local-search controls are missing');
  }
}

// These describe behaviour the page carries rather than markup it publishes, so
// they are the one thing asked of the page including its inline scripts.
const navigationSource = await siteSource(routeFile('getting-started/configuration'));
for (const required of [/data-mobile-menu-expanded/, /matchMedia\([^)]*min-width:\s*50em/]) {
  if (!required.test(navigationSource)) {
    failures.push('responsive navigation behaviour is missing from the published page');
  }
}

const paginationHtml = await siteHtml(routeFile('getting-started/configuration'));
if (!linkWithRelationship(
  paginationHtml,
  'prev',
  `${base}getting-started/installation/`,
)) {
  failures.push('previous-page navigation is missing from configuration');
}
if (!linkWithRelationship(
  paginationHtml,
  'next',
  `${base}getting-started/authentication/`,
)) {
  failures.push('next-page navigation is missing from configuration');
}

const installationHtml = await siteHtml(routeFile('getting-started/installation'));
if (
  !/<pre\b[^>]+data-language=/i.test(installationHtml)
  || !/<div[^>]+aria-live="polite"/i.test(installationHtml)
) {
  failures.push('syntax highlighting or the accessible copy status region is missing');
}

// The decorative shell prompt lives in Expressive Code's gutter, never in the
// code text, so it must not reach the clipboard and must not be announced.
const promptGutters = [...installationHtml.matchAll(
  /<span\b((?=[^>]*\bclass=(?:"(?:[^"]*\s)?shell-prompt(?:\s[^"]*)?"|'(?:[^']*\s)?shell-prompt(?:\s[^']*)?'))[^>]*)>([^<]*)<\/span>/gi,
)];
if (!promptGutters.length) {
  failures.push('shell blocks do not render the decorative command prompt');
}
if (promptGutters.some(([, attributes]) => !attributes.includes('aria-hidden="true"'))) {
  failures.push('decorative shell prompt is exposed to assistive technology');
}
if (!promptGutters.some(([, , text]) => text === '$')) {
  failures.push('no shell command line renders a visible prompt');
}
if (!promptGutters.some(([, , text]) => text === '')) {
  failures.push('shell comment and blank lines do not render an empty aligned prompt');
}
// Presence of the copy control and the contents it copies are asserted by the
// same pass, so a parser that stops matching fails instead of passing silently.
let copyControlCount = 0;
let inspectedCopyControlCount = 0;
for (const [, attributes] of installationHtml.matchAll(
  /<button\b((?=[^>]*\btitle=(?:"Copy to clipboard"|'Copy to clipboard'))[^>]*)>/gi,
)) {
  copyControlCount += 1;
  const dataCodeMatch = attributes.match(/\bdata-code=(?:"([^"]*)"|'([^']*)')/i);
  if (!dataCodeMatch) continue;
  inspectedCopyControlCount += 1;
  const copiedText = dataCodeMatch[1] ?? dataCodeMatch[2];
  // Expressive Code encodes newlines in `data-code` as U+007F.
  if (/(?:^|\u007f)\s*\$\s/.test(copiedText)) {
    failures.push('copied shell text includes the decorative command prompt');
  }
}
if (inspectedCopyControlCount === 0) {
  failures.push('installation page has no inspectable copy controls');
} else if (inspectedCopyControlCount !== copyControlCount) {
  failures.push('a copy control on the installation page carries no copyable text');
}

const landingHtml = await siteHtml(routeFile(''));
if (!landingHtml.includes('starlight-aside--caution')) {
  failures.push('guide home is missing the ECN authorization caution');
}
if (
  !landingHtml.includes('primary MQTT v5 transport')
  || !landingHtml.includes('Broker ACLs determine')
) {
  failures.push('guide home does not define the SDK transport boundary');
}
if (!landingHtml.includes('Position Location Information (PLI)')) {
  failures.push('guide home does not define PLI');
}
const credentialGuidance = [
  'Use the credentials you were provided.',
  'contact your Picogrid Deployments or Engineering contact.',
];
const credentialPages = [
  ['guide home', landingHtml],
  ['authentication guide', await siteHtml(routeFile('getting-started/authentication'))],
  ['observation quickstart', await siteHtml(routeFile('quickstarts/observe-data'))],
  ['operator setup', await siteHtml(routeFile('operator/application'))],
];
for (const [label, html] of credentialPages) {
  const normalizedHtml = html.replace(/\s+/g, ' ');
  if (!credentialGuidance.every((text) => normalizedHtml.includes(text))) {
    failures.push(`${label} is missing the credential contact guidance`);
  }
}
const locationsConceptHtml = await siteHtml(routeFile('concepts/locations'));
if (
  !locationsConceptHtml.includes('Position Location Information (PLI)')
  || !locationsConceptHtml.includes('https://man.fas.org/dod-101/sys/ship/weaps/docs/plicon.htm')
  || !locationsConceptHtml.includes('not a separate Python model')
  || !locationsConceptHtml.includes('client.locations.publish')
) {
  failures.push('locations concept does not define PLI and its shared-wire boundary');
}
for (const route of [
  'quickstarts/effector-handler',
  'quickstarts/observe-data',
  'quickstarts/operator-view',
  'quickstarts/sensor-publisher',
]) {
  const html = await siteHtml(routeFile(route));
  if (!html.includes('Run profile:')) {
    failures.push(`${route}: quickstart does not identify its run profile`);
  }
}

if (allFiles.some((path) => path === join(root, 'favicon.svg'))) {
  failures.push('obsolete independently traced favicon is still published');
}
for (const [name, width, height] of [
  ['operator-mock.png', 1440, 920],
  ['operator-mock-light.png', 1440, 920],
  ['operator-mock-mobile-dark.png', 390, 844],
  ['operator-mock-mobile-light.png', 390, 844],
]) {
  const screenshotPath = join(root, name);
  if (!allFiles.includes(screenshotPath)) {
    failures.push(`operator publication screenshot is missing: ${name}`);
    continue;
  }
  const screenshot = await readFile(screenshotPath);
  if (
    screenshot.length < 1_000
    || screenshot.subarray(0, 8).toString('hex') !== '89504e470d0a1a0a'
    || screenshot.readUInt32BE(16) !== width
    || screenshot.readUInt32BE(20) !== height
  ) {
    failures.push(`operator publication screenshot has an invalid PNG shape: ${name}`);
  }
}
// `publicDir` is copied into the artifact verbatim, and that copy is what carries
// the third-party notices: the deployed guide renders Lucide path data and serves
// a Chivo Mono subset, both licenses require their notice to travel with the copy,
// and the artifact contains no `NOTICE.md`. So every file in the source tree must
// reach the built site byte for byte — an empty or edited copy satisfies a path
// check while leaving the deployed guide without the notice it owes.
//
// The comparison is against the source tree rather than a path recorded in a
// policy file, which means this gate takes no path from data: no traversal or
// separator spelling can aim it at a file outside the served tree, and the site
// build needs no policy file in its source snapshot. Whether each source notice
// is the correct upstream text is the other half of the question, settled against
// its pinned digest by `scripts/license_policy.py`.
const publicSource = resolve(documentationWorkspaceRoot, 'site/public');
for (const source of await filesBelow(publicSource)) {
  const servedRelative = relative(publicSource, source);
  const served = join(root, servedRelative);
  if (!allFiles.includes(served)) {
    failures.push(`public asset is missing from the built site: ${servedRelative}`);
    continue;
  }
  const [expected, actual] = await Promise.all([readFile(source), readFile(served)]);
  if (!expected.equals(actual)) {
    failures.push(`public asset does not carry its source bytes: ${servedRelative}`);
  }
}
const notFoundHtml = await siteHtml(join(root, '404.html'));
if (
  !/<h1\b[^>]*>Page not found<\/h1>/i.test(notFoundHtml)
  || !notFoundHtml.includes('Picogrid ECN SDK')
  || !notFoundHtml.includes(`href="${brandIconHref}"`)
  || !notFoundHtml.includes(`href="${base}quickstarts/observe-data/"`)
) {
  failures.push('branded 404 page is missing required recovery content');
}
const guideIdentity = (name) =>
  new RegExp(`<meta name="${name}" content="([^"]*)"`).exec(landingPageHtml)?.[1];
for (const name of ['version', 'source-ref']) {
  const value = guideIdentity(name);
  if (!value || !notFoundHtml.includes(`<meta name="${name}" content="${value}"`)) {
    failures.push(`branded 404 page ${name} metadata does not match the guide`);
  }
}
const guideCommit = guideIdentity('source-commit');
if (
  guideCommit !== undefined
  && !notFoundHtml.includes(`<meta name="source-commit" content="${guideCommit}"`)
) {
  failures.push('branded 404 page source-commit metadata does not match the guide');
}
for (const asset of [
  join(root, 'pagefind', 'pagefind.js'),
  join(root, 'pagefind', 'pagefind-ui.js'),
  join(root, 'pagefind', 'pagefind-worker.js'),
]) {
  if (!allFiles.includes(asset)) failures.push(`local Pagefind asset is missing: ${relative(root, asset)}`);
}
if (allFiles.filter((path) => path.endsWith('.pf_fragment')).length < 20) {
  failures.push('local Pagefind index contains too few documents');
}

const sitemapIndex = join(root, 'sitemap-index.xml');
const sitemap = join(root, 'sitemap-0.xml');
if (!allFiles.includes(sitemapIndex) || !allFiles.includes(sitemap)) {
  failures.push('sitemap is missing');
} else {
  const sitemapIndexXml = await readFile(sitemapIndex, 'utf8');
  const sitemapXml = await readFile(sitemap, 'utf8');
  if (!sitemapIndexXml.includes(`${canonicalBase}sitemap-0.xml`)) {
    failures.push('sitemap index does not use the published base URL');
  }
  for (const route of guideRoutes) {
    const canonical = `${canonicalBase}${route ? `${route}/` : ''}`;
    if (!sitemapXml.includes(`<loc>${canonical}</loc>`)) {
      failures.push(`sitemap is missing guide route ${canonical}`);
    }
  }
  for (const route of maintainerOnlyDocumentationRoutes) {
    const canonical = `${canonicalBase}${route}/`;
    if (sitemapXml.includes(`<loc>${canonical}</loc>`)) {
      failures.push(`sitemap contains maintainer-only route ${canonical}`);
    }
  }
}

try {
  const themeCss = await readFile(
    resolve(documentationWorkspaceRoot, 'src/styles/picogrid.css'),
    'utf8',
  );
  const builtCss = (await Promise.all(
    allFiles.filter((path) => path.endsWith('.css')).map((path) => readFile(path, 'utf8')),
  )).join('\n');
  if (!builtCss.includes('--pg-background') || !builtCss.includes('--sl-color-accent-low')) {
    failures.push('Picogrid light/dark design tokens are absent from the built CSS');
  }
  const light = cssBlock(themeCss, ':root {');
  const dark = cssBlock(themeCss, ":root[data-theme='dark']");
  const pairs = [
    ['light text', oklchProperty(light, '--pg-foreground'), oklchProperty(light, '--pg-background')],
    ['light muted text', oklchProperty(light, '--pg-muted-foreground'), oklchProperty(light, '--pg-background')],
    ['light link', oklchProperty(light, '--sl-color-accent'), oklchProperty(light, '--pg-background')],
    ['dark text', oklchProperty(dark, '--pg-foreground'), oklchProperty(dark, '--pg-background')],
    ['dark muted text', oklchProperty(dark, '--pg-muted-foreground'), oklchProperty(dark, '--pg-background')],
    ['dark link', oklchProperty(dark, '--sl-color-accent'), oklchProperty(dark, '--pg-background')],
  ];
  for (const [label, foreground, background] of pairs) {
    const ratio = contrast(foreground, background);
    if (ratio < 4.5) failures.push(`${label} contrast is ${ratio.toFixed(2)}:1, below 4.5:1`);
  }
  const markupHasPrintWordmark = new RegExp(
    String.raw`<img\b(?=[^>]*\bclass="[^"]*site-title-print-wordmark)(?=[^>]*\bsrc="${printWordmarkHref.replaceAll('/', String.raw`\/`)}")[^>]*>`,
  ).test(landingPageHtml);
  const printTreatment = /@media\s+print/.test(themeCss)
    ? themeCss.slice(themeCss.search(/@media\s+print/))
    : '';
  // A background image is not printed unless the reader opts in, so the printed
  // mark has to be foreground content the print block reveals.
  if (
    !printTreatment.includes('.site-title::before')
    || !/\.site-title::before\s*\{[^}]*display:\s*none/.test(printTreatment)
    || !/\.site-title-print-wordmark\s*\{[^}]*display:\s*block/.test(printTreatment)
    || !markupHasPrintWordmark
    || !printTreatment.includes('overflow: visible;')
  ) {
    failures.push('readable light print treatment is missing from the public theme');
  }
  // The brand band is one shared surface with the Legion API documentation, so
  // it must be published as artwork on a dark field rather than a page tint.
  if (
    !/header\.header\s*\{[^}]*var\(--pg-nav-texture\)/.test(themeCss)
    || !/header\.header\s*\{[^}]*--pg-nav-surface/.test(themeCss)
    // The mount-derived property is injected as an inline style, which
    // `siteHtml` blanks, so read the page exactly as published.
    || !(await siteSource(routeFile(''))).includes(
      `--pg-nav-texture:url('${navTextureHref}')`,
    )
  ) {
    failures.push('shared navigation band artwork is missing from the published theme');
  }
} catch (error) {
  failures.push(`theme verification failed: ${error.message}`);
}
if (failures.length) throw new Error(failures.join('\n'));

console.log(
  `documentation site check passed: ${htmlFiles.length} HTML pages, ${externalLinks.size} external links, ${builtCodeBlockCount} code blocks`,
);
