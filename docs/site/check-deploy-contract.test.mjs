// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';
import test from 'node:test';

const execFileAsync = promisify(execFile);
const checkerSource = await readFile(new URL('check-deploy-contract.mjs', import.meta.url), 'utf8');
const jsoncSource = await readFile(new URL('jsonc.mjs', import.meta.url), 'utf8');
const legionSource = await readFile(new URL('legion-documentation.mjs', import.meta.url), 'utf8');
const headersSource = await readFile(new URL('../cloudflare/headers.ts', import.meta.url), 'utf8');
const workerSource = await readFile(new URL('../cloudflare/worker.ts', import.meta.url), 'utf8');

async function runFixture({
  css = '',
  html = '<h1>Docs</h1>',
  wrangler = null,
  manifest = null,
  rootHtml = null,
  mutateHeaders = (source) => source,
  mutateWorker = (source) => source,
  legionDocsUrl = '',
} = {}) {
  const root = await mkdtemp(join(tmpdir(), 'deploy-contract-'));
  try {
    await Promise.all([
      mkdir(join(root, 'docs', 'site'), { recursive: true }),
      mkdir(join(root, 'docs', 'cloudflare'), { recursive: true }),
      mkdir(join(root, 'site-dist', 'ecn-sdk'), { recursive: true }),
    ]);
    await Promise.all([
      writeFile(join(root, 'docs', 'site', 'check-deploy-contract.mjs'), checkerSource),
      writeFile(join(root, 'docs', 'site', 'jsonc.mjs'), jsoncSource),
      writeFile(join(root, 'docs', 'site', 'legion-documentation.mjs'), legionSource),
      writeFile(join(root, 'docs', 'site', 'site-config.mjs'), [
        "import { join, resolve } from 'node:path';",
        "import { fileURLToPath } from 'node:url';",
        "export const assetsOutputDirectory = 'site-dist';",
        "export const documentationBase = '/ecn-sdk/';",
        "export const documentationBasePath = '/ecn-sdk';",
        "export const documentationHost = 'docs.picogrid.com';",
        "export const siteContentDirectory = 'site-dist/ecn-sdk';",
        "export const repositoryRoot = resolve(fileURLToPath(new URL('../../', import.meta.url)));",
        "export const documentationWorkspaceRoot = resolve(fileURLToPath(new URL('../', import.meta.url)));",
        "export const assetsOutputRoot = join(repositoryRoot, assetsOutputDirectory);",
        "export const siteContentRoot = join(repositoryRoot, siteContentDirectory);",
      ].join('\n')),
      writeFile(join(root, 'docs', 'site', 'public-url-manifest.mjs'),
        manifest ?? "export const publicUrlPathnames = ['/ecn-sdk/'];\n"),
      writeFile(join(root, 'docs', 'cloudflare', 'headers.ts'), mutateHeaders(headersSource)),
      writeFile(join(root, 'docs', 'cloudflare', 'worker.ts'), mutateWorker(workerSource)),
      writeFile(join(root, 'docs', 'wrangler.jsonc'), wrangler ?? JSON.stringify({
        name: 'ecn-sdk',
        main: 'cloudflare/worker.ts',
        workers_dev: false,
        preview_urls: true,
        routes: [
          { pattern: 'docs.picogrid.com/ecn-sdk', zone_id: 'test-zone-id' },
          { pattern: 'docs.picogrid.com/ecn-sdk/*', zone_id: 'test-zone-id' },
        ],
        assets: { directory: '../site-dist', binding: 'ASSETS' },
      })),
      writeFile(join(root, 'site-dist', 'index.html'), rootHtml
        ?? '<meta http-equiv="refresh" content="0;url=/ecn-sdk/">'
        + "<script>location.replace('/ecn-sdk/' + location.search + location.hash)</script>"
        + '<link rel="canonical" href="https://docs.picogrid.com/ecn-sdk/">'),
      writeFile(join(root, 'site-dist', 'ecn-sdk', 'index.html'), html),
      writeFile(join(root, 'site-dist', 'ecn-sdk', 'styles.css'), css),
    ]);

    try {
      const result = await execFileAsync(
        process.execPath,
        [join(root, 'docs', 'site', 'check-deploy-contract.mjs')],
        { cwd: join(root, 'docs'), env: { ...process.env, LEGION_DOCS_URL: legionDocsUrl } },
      );
      return { ok: true, output: result.stdout + result.stderr };
    } catch (error) {
      return { ok: false, output: error.stdout + error.stderr };
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

test('rejects a weakened deployment security header in the policy table', async () => {
  const result = await runFixture({
    mutateHeaders(source) {
      // Which quote character the policy table is written with is the
      // formatter's business, so the mutation reproduces whatever it finds
      // rather than pinning one style and silently weakening nothing.
      const mutated = source.replace(
        /(['"])X-Frame-Options\1(\s*:\s*)(['"])DENY\3/,
        '$1X-Frame-Options$1$2$3ALLOWALL$3',
      );
      assert.notEqual(mutated, source, 'headers.ts mutation fixture no longer matches source');
      return mutated;
    },
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /X-Frame-Options/);
  assert.match(result.output, /DENY/);
});

test('reports an invalid headers module through the aggregated failure summary', async () => {
  const result = await runFixture({
    mutateHeaders: () => 'this is not valid TypeScript;',
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /deploy contract check failed \(/);
  assert.match(result.output, /cloudflare\/headers\.ts could not be imported/);
});

test('rejects a Worker documentation mount that differs from site-config', async () => {
  const result = await runFixture({
    mutateWorker(source) {
      const mutated = source.replace(
        'const DOCUMENTATION_MOUNT = "/ecn-sdk";',
        'const DOCUMENTATION_MOUNT = "/wrong-mount";',
      );
      assert.notEqual(mutated, source, 'worker.ts mutation fixture no longer matches source');
      return mutated;
    },
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /Worker DOCUMENTATION_MOUNT/);
});

test('admits the reviewed Legion reference, and nothing else off the mount', async () => {
  const admitted = await runFixture({
    html: '<a href="https://docs.picogrid.com/reference/start">Legion API</a>',
  });
  assert.equal(admitted.ok, true, admitted.output);

  // Its neighbour on the same host is not the reviewed target.
  const neighbour = await runFixture({
    html: '<a href="https://docs.picogrid.com/reference/other">Legion API</a>',
  });
  assert.equal(neighbour.ok, false);
  assert.match(neighbour.output, /resolves outside \/ecn-sdk\/: .*\/reference\/other/);

  // A page of the guide that escapes the mount still fails.
  const escaped = await runFixture({ html: '<a href="/concepts/ecns/">Concepts</a>' });
  assert.equal(escaped.ok, false);
  assert.match(escaped.output, /resolves outside \/ecn-sdk\/: \/concepts\/ecns\//);
});

test('rejects variants of the reviewed Legion reference', async () => {
  const reviewed = 'https://docs.picogrid.com/reference/start';
  // The credential is applied through the URL API rather than written inline. A
  // credential-bearing URL literal on an approved host is a publication-input
  // violation in its own right, and the release scan refuses the file carrying
  // it, so spelling this variant out would trade one gate for another.
  const credentialed = new URL(reviewed);
  credentialed.username = 'reader';

  const variants = [
    ['different scheme', reviewed.replace('https:', 'http:')],
    ['explicit port', reviewed.replace('.com/', '.com:8443/')],
    ['query string', `${reviewed}?source=guide`],
    ['embedded credentials', credentialed.href],
  ];

  for (const [label, href] of variants) {
    const result = await runFixture({ html: `<a href="${href}">Legion API</a>` });
    assert.equal(result.ok, false, `${label} unexpectedly passed:\n${result.output}`);
    assert.match(result.output, /resolves outside \/ecn-sdk\//);
  }
});

test('rejects root-absolute CSS url() references outside the docs mount', async () => {
  const result = await runFixture({ css: '.hero { background: url( /asset.png ); }' });
  assert.equal(result.ok, false);
  assert.match(result.output, /reference resolves outside \/ecn-sdk\/: \/asset\.png/);
});

test('rejects protocol-relative CSS url() references outside the docs mount', async () => {
  const result = await runFixture({
    css: '.hero { background: url(//docs.picogrid.com/assets/app.css); }',
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /reference resolves outside \/ecn-sdk\//);
});

test('rejects protocol-relative CSS @import references outside the docs mount', async () => {
  const result = await runFixture({
    css: '@import "//docs.picogrid.com/assets/app.css";',
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /reference resolves outside \/ecn-sdk\//);
});

test('rejects development hosts in CSS @import references', async () => {
  const result = await runFixture({ css: "@import  'https://localhost:4321/x.css';" });
  assert.equal(result.ok, false);
  assert.match(result.output, /development host localhost/);
});

test('accepts a development-host word in an in-mount URL fragment', async () => {
  const result = await runFixture({
    html: '<a href="/ecn-sdk/how-to/troubleshooting/#connect-to-localhost">Help</a>',
  });
  assert.equal(result.ok, true, result.output);
});

test('rejects relative CSS url() references that resolve outside the mount', async () => {
  const result = await runFixture({ css: '.icon { background-image: url("../brand/icon.svg"); }' });
  assert.equal(result.ok, false);
  assert.match(result.output, /reference resolves outside \/ecn-sdk\//);
});

test('accepts relative CSS url() references that stay in the mount', async () => {
  const result = await runFixture({ css: '.icon { background-image: url("brand/icon.svg"); }' });
  assert.equal(result.ok, true, result.output);
});

test('rejects an out-of-mount srcset candidate after a valid one', async () => {
  const result = await runFixture({
    html: '<img srcset="/ecn-sdk/a.png 1x, /asset.png 2x" src="/ecn-sdk/a.png">',
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /reference resolves outside \/ecn-sdk\//);
});

test('accepts an srcset whose candidates all stay in the mount', async () => {
  const result = await runFixture({
    html: '<img srcset="/ecn-sdk/a.png 1x, /ecn-sdk/a2.png 2x" src="/ecn-sdk/a.png">',
  });
  assert.equal(result.ok, true, result.output);
});

test('rejects an unquoted HTML reference outside the docs mount', async () => {
  const result = await runFixture({ html: '<img src=/asset.png>' });
  assert.equal(result.ok, false);
  assert.match(result.output, /reference resolves outside \/ecn-sdk\//);
});

test('accepts an unquoted HTML reference inside the docs mount', async () => {
  const result = await runFixture({ html: '<img src=/ecn-sdk/a.png>' });
  assert.equal(result.ok, true, result.output);
});

test('rejects an HTML meta refresh target outside the docs mount', async () => {
  const result = await runFixture({
    html: '<meta http-equiv="refresh" content="0;url=/asset.png">',
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /reference resolves outside \/ecn-sdk\//);
});

test('accepts an HTML meta refresh target inside the docs mount', async () => {
  const result = await runFixture({
    html: '<meta http-equiv="REFRESH" content="0;url=/ecn-sdk/a.png">',
  });
  assert.equal(result.ok, true, result.output);
});

test('does not treat non-refresh HTML content attributes as URLs', async () => {
  const result = await runFixture({
    html: '<meta name="description" content="/asset.png">',
  });
  assert.equal(result.ok, true, result.output);
});

test('parses JSONC comments and trailing commas without breaking strings', async () => {
  const result = await runFixture({
    css: '.icon { background-image: url("brand/icon.svg"); }',
    wrangler: '{\n  // Worker entry\n  "name": "ecn-sdk",\n  "workers_dev": false,\n  "preview_urls": true,\n  "main": "cloudflare/worker.ts", // trailing comment\n'
      + '  "routes": [{ "pattern": "docs.picogrid.com/ecn-sdk", "zone_id": "test-zone-id" },\n'
      + '    { "pattern": "docs.picogrid.com/ecn-sdk/*", "zone_id": "test-zone-id" }],\n'
      + '  /* block */\n  "assets": { "directory": "../site-dist", "binding": "ASSETS", },\n}',
  });
  assert.equal(result.ok, true, result.output);
});

test('rejects an unterminated JSONC block comment', async () => {
  const result = await runFixture({ wrangler: '{} /*' });
  assert.equal(result.ok, false);
  assert.match(result.output, /unterminated block comment/);
});

test('rejects an unterminated JSONC string literal', async () => {
  const result = await runFixture({ wrangler: '{"main": "cloudflare\/worker.ts}' });
  assert.equal(result.ok, false);
  assert.match(result.output, /unterminated string literal/);
});

test('accepts in-mount CSS url() references', async () => {
  const result = await runFixture({ css: '.icon { mask: url( /ecn-sdk/brand/icon.svg ) }' });
  assert.equal(result.ok, true, result.output);
});

test('accepts in-mount protocol-relative CSS references', async () => {
  const result = await runFixture({
    css: '.icon { mask: url(//docs.picogrid.com/ecn-sdk/brand/icon.svg) }',
  });
  assert.equal(result.ok, true, result.output);
});

test('rejects dot-segment references that escape the mount after resolution', async () => {
  for (const css of [
    '.a { background: url("/ecn-sdk/../asset.png"); }',
    '.a { background: url("/ecn-sdk/%2e%2e/asset.png"); }',
    '.a { background: url("https://docs.picogrid.com/ecn-sdk/../asset.png"); }',
  ]) {
    const result = await runFixture({ css });
    assert.equal(result.ok, false, css);
    assert.match(result.output, /reference resolves outside \/ecn-sdk\//);
  }
});

test('a manifest path that resolves outside the mount finds no disk candidate', async () => {
  const result = await runFixture({
    manifest: "export const publicUrlPathnames = ['/ecn-sdk/', '/ecn-sdk/../../cloudflare/headers.ts'];\n",
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /public URL path missing on disk/);
});

test('rejects a wrangler assets directory that does not match the build', async () => {
  const result = await runFixture({
    wrangler: JSON.stringify({
      name: 'ecn-sdk',
      main: 'cloudflare/worker.ts',
      workers_dev: false,
      preview_urls: true,
      routes: [
        { pattern: 'docs.picogrid.com/ecn-sdk', zone_id: 'test-zone-id' },
        { pattern: 'docs.picogrid.com/ecn-sdk/*', zone_id: 'test-zone-id' },
      ],
      assets: { directory: './wrong-dist', binding: 'ASSETS' },
    }),
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /assets\.directory/);
});

test('rejects a wrangler assets binding rename', async () => {
  const result = await runFixture({
    wrangler: JSON.stringify({
      name: 'ecn-sdk',
      main: 'cloudflare/worker.ts',
      workers_dev: false,
      preview_urls: true,
      routes: [
        { pattern: 'docs.picogrid.com/ecn-sdk', zone_id: 'test-zone-id' },
        { pattern: 'docs.picogrid.com/ecn-sdk/*', zone_id: 'test-zone-id' },
      ],
      assets: { directory: '../site-dist', binding: 'STATIC' },
    }),
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /assets\.binding/);
});

test('rejects a wrangler main that is not the Worker entrypoint', async () => {
  const result = await runFixture({
    wrangler: JSON.stringify({
      name: 'ecn-sdk',
      main: 'cloudflare/other.ts',
      workers_dev: false,
      preview_urls: true,
      routes: [
        { pattern: 'docs.picogrid.com/ecn-sdk', zone_id: 'test-zone-id' },
        { pattern: 'docs.picogrid.com/ecn-sdk/*', zone_id: 'test-zone-id' },
      ],
      assets: { directory: '../site-dist', binding: 'ASSETS' },
    }),
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /main/);
});

test('rejects a root redirect that targets the wrong path', async () => {
  const result = await runFixture({
    rootHtml: '<meta http-equiv="refresh" content="0;url=/elsewhere/">'
      + "<script>location.replace('/elsewhere/')</script>"
      + '<link rel="canonical" href="https://docs.picogrid.com/ecn-sdk/">',
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /root redirect does not target/);
});

test('rejects a root redirect without a scripted navigation', async () => {
  const result = await runFixture({
    rootHtml: '<meta http-equiv="refresh" content="0;url=/ecn-sdk/">'
      + '<link rel="canonical" href="https://docs.picogrid.com/ecn-sdk/">',
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /missing client navigate to/);
});

test('rejects a root redirect missing the canonical host', async () => {
  const result = await runFixture({
    rootHtml: '<meta http-equiv="refresh" content="0;url=/ecn-sdk/">'
      + "<script>location.replace('/ecn-sdk/')</script>",
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /missing canonical host/);
});

test('rejects a renamed Worker, which the deploy smoke resolves by name', async () => {
  const result = await runFixture({
    wrangler: JSON.stringify({
      name: 'renamed',
      main: 'cloudflare/worker.ts',
      workers_dev: false,
      preview_urls: true,
      routes: [
        { pattern: 'docs.picogrid.com/ecn-sdk', zone_id: 'test-zone-id' },
        { pattern: 'docs.picogrid.com/ecn-sdk/*', zone_id: 'test-zone-id' },
      ],
      assets: { directory: '../site-dist', binding: 'ASSETS' },
    }),
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /wrangler name must be ecn-sdk/);
});

const routedWrangler = (routes, extra = {}) => JSON.stringify({
  name: 'ecn-sdk',
  main: 'cloudflare/worker.ts',
  workers_dev: false,
  preview_urls: true,
  routes,
  ...extra,
  assets: { directory: '../site-dist', binding: 'ASSETS' },
});

test('accepts the two exact mount routes', async () => {
  const result = await runFixture({
    wrangler: routedWrangler([
      { pattern: 'docs.picogrid.com/ecn-sdk', zone_id: 'test-zone-id' },
      { pattern: 'docs.picogrid.com/ecn-sdk/*', zone_id: 'test-zone-id' },
    ]),
  });
  assert.equal(result.ok, true, result.output);
});

test('rejects a route that over-captures sibling paths', async () => {
  const result = await runFixture({
    wrangler: routedWrangler([
      { pattern: 'docs.picogrid.com/ecn-sdk*', zone_id: 'test-zone-id' },
    ]),
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /not derived from the documentation mount/);
});

test('rejects a route that captures the whole canonical host', async () => {
  const result = await runFixture({
    wrangler: routedWrangler([
      { pattern: 'docs.picogrid.com/*', zone_id: 'test-zone-id' },
    ]),
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /not derived from the documentation mount/);
});

test('rejects routes missing the bare mount, which carries the redirect', async () => {
  const result = await runFixture({
    wrangler: routedWrangler([
      { pattern: 'docs.picogrid.com/ecn-sdk/*', zone_id: 'test-zone-id' },
    ]),
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /must include "docs\.picogrid\.com\/ecn-sdk"/);
});

test('rejects a route that names no zone', async () => {
  const result = await runFixture({
    wrangler: routedWrangler([
      { pattern: 'docs.picogrid.com/ecn-sdk' },
      { pattern: 'docs.picogrid.com/ecn-sdk/*' },
    ]),
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /must name a zone/);
});

test('rejects a configuration that declares no routes', async () => {
  const result = await runFixture({
    wrangler: JSON.stringify({
      name: 'ecn-sdk',
      main: 'cloudflare/worker.ts',
      workers_dev: false,
      preview_urls: true,
      assets: { directory: '../site-dist', binding: 'ASSETS' },
    }),
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /must declare the canonical host routes/);
});

test('rejects an empty routes array', async () => {
  const result = await runFixture({ wrangler: routedWrangler([]) });
  assert.equal(result.ok, false);
  assert.match(result.output, /must declare the canonical host routes/);
});

test('rejects a Worker whose preview URLs are disabled', async () => {
  const result = await runFixture({
    wrangler: JSON.stringify({
      name: 'ecn-sdk',
      main: 'cloudflare/worker.ts',
      preview_urls: false,
      routes: [
        { pattern: 'docs.picogrid.com/ecn-sdk', zone_id: 'test-zone-id' },
        { pattern: 'docs.picogrid.com/ecn-sdk/*', zone_id: 'test-zone-id' },
      ],
      assets: { directory: '../site-dist', binding: 'ASSETS' },
    }),
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /preview_urls must be true/);
});

test('rejects a Worker that omits preview URLs, which then follow workers_dev', async () => {
  const result = await runFixture({ wrangler: routedWrangler([
    { pattern: 'docs.picogrid.com/ecn-sdk', zone_id: 'test-zone-id' },
    { pattern: 'docs.picogrid.com/ecn-sdk/*', zone_id: 'test-zone-id' },
  ], { preview_urls: undefined }) });
  assert.equal(result.ok, false);
  assert.match(result.output, /preview_urls must be true/);
});

test('accepts the production route form, which selects the zone by name', async () => {
  // wrangler.jsonc selects the zone by name; the other fixtures use zone_id, so
  // without this one a regression in the shipped form would pass the suite.
  const result = await runFixture({
    wrangler: routedWrangler([
      { pattern: 'docs.picogrid.com/ecn-sdk', zone_name: 'example.invalid' },
      { pattern: 'docs.picogrid.com/ecn-sdk/*', zone_name: 'example.invalid' },
    ]),
  });
  assert.equal(result.ok, true, result.output);
});

test('accepts plain string routes, whose zone Wrangler infers from the pattern', async () => {
  const result = await runFixture({
    wrangler: routedWrangler(['docs.picogrid.com/ecn-sdk', 'docs.picogrid.com/ecn-sdk/*']),
  });
  assert.equal(result.ok, true, result.output);
});

test('rejects a string route that over-captures, as it does an object route', async () => {
  const result = await runFixture({ wrangler: routedWrangler(['docs.picogrid.com/ecn-sdk*']) });
  assert.equal(result.ok, false);
  assert.match(result.output, /not derived from the documentation mount/);
});

test('rejects re-enabling the bootstrap host after cutover', async () => {
  const result = await runFixture({
    wrangler: routedWrangler(
      [
        { pattern: 'docs.picogrid.com/ecn-sdk', zone_id: 'test-zone-id' },
        { pattern: 'docs.picogrid.com/ecn-sdk/*', zone_id: 'test-zone-id' },
      ],
      { workers_dev: true },
    ),
  });
  assert.equal(result.ok, false);
  assert.match(result.output, /workers_dev must be false/);
});
