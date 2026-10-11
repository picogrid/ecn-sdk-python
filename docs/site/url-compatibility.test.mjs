// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * Fixture HTTP origin for the public URL compatibility gate (A7).
 */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { after, test } from 'node:test';

import {
  knownJourneyPathnames,
  publicStaticPathnames,
  publicUrlPathnames,
} from './public-url-manifest.mjs';
import {
  liveManifestPathnames,
  normalizeCompatibilityBaseUrl,
  publicRedirects,
  runUrlCompatibility,
  validateManifestContract,
} from './url-compatibility.mjs';
import { documentationBasePath } from './site-config.mjs';

const fixtures = [];
const nonpublicRouteSegment = ['pri', 'vate'].join('');
after(async () => {
  for (const server of fixtures) {
    await new Promise((closed) => server.close(closed));
  }
});

async function startServer(handler) {
  const server = createServer(handler);
  await new Promise((listening) => server.listen(0, '127.0.0.1', listening));
  fixtures.push(server);
  const { port } = server.address();
  return `http://127.0.0.1:${port}`;
}

const SAMPLE_PATHS = [
  `${documentationBasePath}/`,
  `${documentationBasePath}/getting-started/installation/`,
  `${documentationBasePath}/brand/picogrid-app-icon-192.png`,
];

test('normalizeCompatibilityBaseUrl rejects remote http', () => {
  assert.throws(() => normalizeCompatibilityBaseUrl('http://example.invalid'), /must be https/);
  assert.throws(() => normalizeCompatibilityBaseUrl('ftp://localhost'), /must be https/);
  assert.equal(
    normalizeCompatibilityBaseUrl('https://example.invalid/x'),
    'https://example.invalid',
  );
  assert.equal(normalizeCompatibilityBaseUrl('http://localhost:4173/'), 'http://localhost:4173');
});

test('runUrlCompatibility rejects invalid concurrency', async () => {
  for (const concurrency of [0, -1, Number.NaN, 1.5]) {
    await assert.rejects(
      runUrlCompatibility({
        baseUrl: 'http://localhost:4173',
        pathnames: [],
        redirects: {},
        concurrency,
      }),
      /concurrency must be a positive safe integer/,
    );
  }
});

test('manifest contract holds for the committed public URL list', () => {
  assert.deepEqual(validateManifestContract(), []);
  assert.ok(publicUrlPathnames.length >= knownJourneyPathnames.length);
  assert.ok(publicStaticPathnames.every((path) => publicUrlPathnames.includes(path)));
  // Pinned rather than merely non-empty: a declared redirect is a change to the
  // public URL contract, so adding one has to be a deliberate edit here too.
  assert.deepEqual(publicRedirects, {
    [`${documentationBasePath}/404.html`]: {
      location: `${documentationBasePath}/404`,
      status: 307,
    },
  });
  assert.equal(
    liveManifestPathnames().length,
    publicUrlPathnames.length - Object.keys(publicRedirects).length,
  );
});

test('manifest contract rejects escapes and missing journeys', () => {
  const failures = validateManifestContract({
    pathnames: ['/outside/', `${documentationBasePath}/`],
    journeys: [`${documentationBasePath}/missing-journey/`],
    staticAssets: [],
    redirects: {
      '/not-mounted': { location: `${documentationBasePath}/` },
    },
  });
  assert.ok(failures.some((item) => item.includes('escapes')));
  assert.ok(failures.some((item) => item.includes('missing-journey')));
  assert.ok(failures.some((item) => item.includes('redirect source')));
});

test('manifest contract rejects pathnames that URL-normalize outside the mount', () => {
  const sneaky = `${documentationBasePath}/../${nonpublicRouteSegment}/`;
  const failures = validateManifestContract({
    pathnames: [sneaky],
    journeys: [],
    staticAssets: [],
    redirects: {},
  });
  assert.ok(failures.some((item) => item.includes('not canonical')));
});

test('manifest contract rejects redirect sources that URL-normalize', () => {
  const sneaky = `${documentationBasePath}/../${nonpublicRouteSegment}/`;
  const failures = validateManifestContract({
    pathnames: [],
    journeys: [],
    staticAssets: [],
    redirects: {
      [sneaky]: { location: `${documentationBasePath}/` },
    },
  });
  assert.ok(failures.some((item) => item.includes('not canonical')));
  assert.ok(failures.some((item) => item.includes('redirect source escapes')));
});

test('manifest contract rejects redirect targets that URL-normalize', () => {
  const sneaky = `${documentationBasePath}/../${nonpublicRouteSegment}/`;
  const failures = validateManifestContract({
    pathnames: [],
    journeys: [],
    staticAssets: [],
    redirects: {
      [`${documentationBasePath}/old-page/`]: { location: sneaky },
    },
  });
  assert.ok(failures.some((item) => item.includes('not canonical')));
  assert.ok(failures.some((item) => item.includes('redirect target escapes')));
});

test('compliant origin passes the sample manifest', async () => {
  const baseUrl = await startServer((request, response) => {
    const { pathname } = new URL(request.url, 'http://localhost');
    if (SAMPLE_PATHS.includes(pathname)) {
      const body = pathname.endsWith('.png') ? 'PNG-FAKE-BYTES!!!!!!!!!!' : '<!doctype html><html><body>ok</body></html>';
      response.writeHead(200, {
        'content-type': pathname.endsWith('.png') ? 'image/png' : 'text/html',
      });
      response.end(body);
      return;
    }
    response.writeHead(404);
    response.end('no');
  });

  const result = await runUrlCompatibility({
    baseUrl,
    pathnames: SAMPLE_PATHS,
    redirects: {},
  });
  assert.equal(result.ok, true, result.failures.join('; '));
  assert.equal(result.checked.length, SAMPLE_PATHS.length);
});

test('missing public path fails', async () => {
  const baseUrl = await startServer((_request, response) => {
    response.writeHead(404);
    response.end('gone');
  });
  const result = await runUrlCompatibility({
    baseUrl,
    pathnames: [`${documentationBasePath}/gone/`],
    redirects: {},
  });
  assert.equal(result.ok, false);
  assert.ok(result.failures.some((item) => item.includes('status 404')));
});

test('non-body paths use HEAD with a GET fallback for rejected methods', async () => {
  const headOnly = `${documentationBasePath}/robots.txt`;
  const getFallback = `${documentationBasePath}/manifest.json`;
  const requests = [];
  const baseUrl = await startServer((request, response) => {
    const { pathname } = new URL(request.url, 'http://localhost');
    requests.push(`${pathname}:${request.method}`);
    if (pathname === getFallback && request.method === 'HEAD') {
      response.writeHead(405);
      response.end();
      return;
    }
    response.writeHead(200);
    response.end('ok');
  });

  const result = await runUrlCompatibility({
    baseUrl,
    pathnames: [headOnly, getFallback],
    redirects: {},
    concurrency: 1,
  });
  assert.equal(result.ok, true, result.failures.join('; '));
  assert.deepEqual(requests, [
    `${headOnly}:HEAD`,
    `${getFallback}:HEAD`,
    `${getFallback}:GET`,
  ]);
});

test('body-required path validates GET even when HEAD succeeds', async () => {
  for (const getResponse of [
    { status: 200, body: 'short' },
    { status: 404, body: 'not found' },
  ]) {
    const methods = [];
    const baseUrl = await startServer((request, response) => {
      methods.push(request.method);
      if (request.method === 'HEAD') {
        response.writeHead(200);
        response.end();
        return;
      }
      response.writeHead(getResponse.status);
      response.end(getResponse.body);
    });

    const result = await runUrlCompatibility({
      baseUrl,
      pathnames: [`${documentationBasePath}/body-required/`],
      redirects: {},
    });
    assert.equal(result.ok, false);
    assert.deepEqual(methods, ['GET']);
  }
});

test('body read shares the request deadline', async () => {
  const baseUrl = await startServer((_request, response) => {
    response.writeHead(200);
    response.write('too short');
    setTimeout(() => response.destroy(), 250);
  });
  const started = Date.now();
  const result = await runUrlCompatibility({
    baseUrl,
    pathnames: [`${documentationBasePath}/never-closes/`],
    redirects: {},
    requestTimeoutMs: 100,
  });
  assert.equal(result.ok, false);
  assert.ok(Date.now() - started < 225, `deadline took ${Date.now() - started}ms`);
  assert.ok(result.failures.some((item) => item.includes('unreachable')));
});

test('body validation cancels after reading the minimum prefix', async () => {
  let bytesServed = 0;
  let cancelled = false;
  const fetchImpl = async () => new Response(new ReadableStream({
    pull(controller) {
      if (bytesServed >= 128) {
        controller.close();
        return;
      }
      controller.enqueue(new Uint8Array(8));
      bytesServed += 8;
    },
    cancel() {
      cancelled = true;
    },
  }, { highWaterMark: 0 }), { status: 200 });

  const result = await runUrlCompatibility({
    baseUrl: 'http://localhost:4173',
    pathnames: [`${documentationBasePath}/large/`],
    redirects: {},
    fetchImpl,
  });
  assert.equal(result.ok, true, result.failures.join('; '));
  assert.equal(bytesServed, 16);
  assert.equal(cancelled, true);
});

test('unexpected redirect fails unless listed in publicRedirects', async () => {
  const moved = `${documentationBasePath}/old-page/`;
  const target = `${documentationBasePath}/new-page/`;
  const baseUrl = await startServer((request, response) => {
    const { pathname } = new URL(request.url, 'http://localhost');
    if (pathname === moved) {
      response.writeHead(301, { location: target });
      response.end();
      return;
    }
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end('<!doctype html><html><body>ok content here</body></html>');
  });

  const unexpected = await runUrlCompatibility({
    baseUrl,
    pathnames: [moved],
    redirects: {},
  });
  assert.equal(unexpected.ok, false);
  assert.ok(unexpected.failures.some((item) => item.includes('unexpected redirect')));

  const intended = await runUrlCompatibility({
    baseUrl,
    pathnames: [moved, target],
    redirects: { [moved]: { location: target, status: 301 } },
  });
  assert.equal(intended.ok, true, intended.failures.join('; '));
});

test('listed redirect with wrong location fails', async () => {
  const from = `${documentationBasePath}/legacy/`;
  const baseUrl = await startServer((request, response) => {
    if (new URL(request.url, 'http://localhost').pathname === from) {
      response.writeHead(308, { location: `${documentationBasePath}/elsewhere/` });
      response.end();
      return;
    }
    response.writeHead(404);
    response.end('no');
  });
  const result = await runUrlCompatibility({
    baseUrl,
    pathnames: [from],
    redirects: { [from]: { location: `${documentationBasePath}/expected/`, status: 308 } },
  });
  assert.equal(result.ok, false);
  assert.ok(result.failures.some((item) => item.includes('redirect location')));
});


test('listed redirect rejects a cross-origin Location with the expected path', async () => {
  const from = `${documentationBasePath}/legacy-cross-origin/`;
  const target = `${documentationBasePath}/expected/`;
  const baseUrl = await startServer((_request, response) => {
    response.writeHead(308, { location: `https://example.invalid${target}` });
    response.end();
  });
  const result = await runUrlCompatibility({
    baseUrl,
    pathnames: [from],
    redirects: { [from]: { location: target, status: 308 } },
  });
  assert.equal(result.ok, false);
  assert.ok(result.failures.some((item) => item.includes('cross-origin')));
});

test('listed redirect rejects a Location carrying an undeclared query string', async () => {
  const from = `${documentationBasePath}/legacy-query/`;
  const target = `${documentationBasePath}/expected/`;
  const baseUrl = await startServer((_request, response) => {
    response.writeHead(308, { location: `${target}?stale=1` });
    response.end();
  });
  const result = await runUrlCompatibility({
    baseUrl,
    pathnames: [from],
    redirects: { [from]: { location: target, status: 308 } },
  });
  assert.equal(result.ok, false);
  assert.ok(result.failures.some((item) => item.includes('?stale=1')));
});

test('listed redirect rejects a Location carrying an undeclared fragment', async () => {
  const from = `${documentationBasePath}/legacy-fragment/`;
  const target = `${documentationBasePath}/expected/`;
  const baseUrl = await startServer((_request, response) => {
    response.writeHead(308, { location: `${target}#section` });
    response.end();
  });
  const result = await runUrlCompatibility({
    baseUrl,
    pathnames: [from],
    redirects: { [from]: { location: target, status: 308 } },
  });
  assert.equal(result.ok, false);
  assert.ok(result.failures.some((item) => item.includes('#section')));
});

test('a 304 does not satisfy a redirect rule that omits status', async () => {
  const from = `${documentationBasePath}/legacy-not-modified/`;
  const target = `${documentationBasePath}/expected/`;
  const baseUrl = await startServer((_request, response) => {
    response.writeHead(304, { location: target });
    response.end();
  });
  const result = await runUrlCompatibility({
    baseUrl,
    pathnames: [from],
    redirects: { [from]: { location: target } },
  });
  assert.equal(result.ok, false);
  assert.ok(result.failures.some((item) => item.includes('expected redirect')));
});

test('a permitted redirect status satisfies a rule that omits status', async () => {
  const from = `${documentationBasePath}/legacy-moved/`;
  const target = `${documentationBasePath}/expected/`;
  const baseUrl = await startServer((request, response) => {
    if (new URL(request.url, 'http://localhost').pathname === target) {
      // The declared destination has to serve, or the redirect leads nowhere.
      response.writeHead(200, { 'content-type': 'text/html' });
      response.end('<!doctype html><title>Moved here</title><p>content</p>');
      return;
    }
    response.writeHead(303, { location: target });
    response.end();
  });
  const result = await runUrlCompatibility({
    baseUrl,
    pathnames: [from],
    redirects: { [from]: { location: target } },
  });
  assert.deepEqual(result.failures, []);
  assert.equal(result.ok, true);
});

test('a rejecting reader cancel does not fail a body that already passed', async () => {
  const body = 'x'.repeat(64);
  let cancelCalled = false;
  const fetchImpl = async () => ({
    status: 200,
    ok: true,
    headers: new Headers({ 'content-type': 'text/html' }),
    body: {
      getReader() {
        let sent = false;
        return {
          async read() {
            if (sent) return { done: true, value: undefined };
            sent = true;
            return { done: false, value: new TextEncoder().encode(body) };
          },
          async cancel() {
            cancelCalled = true;
            throw new Error('stream already errored');
          },
        };
      },
    },
  });

  const result = await runUrlCompatibility({
    baseUrl: 'https://example.invalid',
    pathnames: [`${documentationBasePath}/`],
    redirects: {},
    fetchImpl,
  });

  assert.equal(cancelCalled, true, 'the reader cancellation path must run');
  assert.deepEqual(result.failures, []);
  assert.equal(result.ok, true);
});

// A declared move removes the source from the live path list, so these are the
// only assertions standing between a recorded redirect and a broken URL.
function redirectFixture(handleTarget) {
  const from = `${documentationBasePath}/legacy-target/`;
  const to = `${documentationBasePath}/target/`;
  return { from, to, handler: (request, response) => {
    const { pathname } = new URL(request.url, 'http://localhost');
    if (pathname === from) {
      response.writeHead(308, { location: to });
      response.end();
      return;
    }
    if (pathname === to) {
      handleTarget(response);
      return;
    }
    response.writeHead(404);
    response.end('no');
  } };
}

async function runRedirectFixture(handleTarget) {
  const { from, to, handler } = redirectFixture(handleTarget);
  const baseUrl = await startServer(handler);
  const result = await runUrlCompatibility({
    baseUrl,
    pathnames: [from],
    redirects: { [from]: { location: to, status: 308 } },
  });
  return { result, to };
}

test('a redirect into a missing document fails', async () => {
  const { result, to } = await runRedirectFixture((response) => {
    response.writeHead(404);
    response.end('gone');
  });
  assert.equal(result.ok, false);
  assert.ok(
    result.failures.some((item) => item.includes(to) && item.includes('redirect target status 404')),
    result.failures.join('; '),
  );
});

test('a redirect into an empty document fails', async () => {
  const { result, to } = await runRedirectFixture((response) => {
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end('');
  });
  assert.equal(result.ok, false);
  assert.ok(
    result.failures.some((item) => item.includes(to) && item.includes('body is empty or too small')),
    result.failures.join('; '),
  );
});

test('a redirect into another redirect fails', async () => {
  const { result, to } = await runRedirectFixture((response) => {
    response.writeHead(302, { location: `${documentationBasePath}/onwards/` });
    response.end();
  });
  assert.equal(result.ok, false);
  assert.ok(
    result.failures.some((item) => item.includes(to) && item.includes('redirected again')),
    result.failures.join('; '),
  );
});

test('a redirect into a served document passes', async () => {
  const { result } = await runRedirectFixture((response) => {
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end('<!doctype html><title>Page not found</title><p>recovery</p>');
  });
  assert.deepEqual(result.failures, []);
  assert.equal(result.ok, true);
});

test('a redirect into a non-200 success status fails', async () => {
  const { result, to } = await runRedirectFixture((response) => {
    // Body is ample; the status alone must fail, so `ok` is not enough.
    response.writeHead(201, { 'content-type': 'text/html' });
    response.end('<!doctype html><title>Created</title><p>not the document</p>');
  });
  assert.equal(result.ok, false);
  assert.ok(
    result.failures.some((item) => item.includes(to) && item.includes('redirect target status 201, expected 200')),
    result.failures.join('; '),
  );
});
