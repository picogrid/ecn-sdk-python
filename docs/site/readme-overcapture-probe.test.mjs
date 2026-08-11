// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * Fixture over-capture probe tests (never needs live docs.picogrid.com).
 */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { after, test } from 'node:test';

import {
  assertNonEcnProbePath,
  isWorkerOvercaptureResponse,
  normalizeProbeOrigin,
  runReadmeOvercaptureProbe,
} from './readme-overcapture-probe.mjs';
import { documentationBase, documentationBasePath } from './site-config.mjs';

const { CONTENT_SECURITY_POLICY, buildSecurityHeaders } = await import(
  new URL('../cloudflare/headers.ts', import.meta.url).href
);

const fixtures = [];
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

test('probe path must be outside the ECN mount', () => {
  assert.equal(assertNonEcnProbePath('/'), '/');
  assert.equal(assertNonEcnProbePath('/guides/auth/'), '/guides/auth/');
  assert.throws(() => assertNonEcnProbePath(documentationBase), /under \/ecn-sdk/);
  assert.throws(() => assertNonEcnProbePath(`${documentationBase}x/`), /under \/ecn-sdk/);
  assert.throws(
    () => assertNonEcnProbePath(`${documentationBasePath}?probe=1`),
    /under \/ecn-sdk/,
  );
  assert.throws(
    () => assertNonEcnProbePath(`${documentationBasePath}#section`),
    /under \/ecn-sdk/,
  );
});

test('isWorkerOvercaptureResponse catches Worker CSP and root redirect', () => {
  assert.equal(
    isWorkerOvercaptureResponse({
      pathname: '/',
      status: 200,
      contentType: 'text/html',
      csp: CONTENT_SECURITY_POLICY,
      body: '<html></html>',
      workerCsp: CONTENT_SECURITY_POLICY,
    }),
    `Content-Security-Policy matches the ECN Worker policy on /`,
  );

  assert.ok(
    isWorkerOvercaptureResponse({
      pathname: '/',
      status: 200,
      contentType: 'text/html',
      csp: null,
      body: `<script>location.replace('${documentationBase}')</script>`,
      workerCsp: CONTENT_SECURITY_POLICY,
    })?.includes('root redirect'),
  );

  assert.equal(
    isWorkerOvercaptureResponse({
      pathname: '/',
      status: 200,
      contentType: 'text/html',
      csp: "default-src 'self'",
      body: '<html><body>ReadMe product docs</body></html>',
      workerCsp: CONTENT_SECURITY_POLICY,
    }),
    null,
  );
});

test('isWorkerOvercaptureResponse distinguishes branded and ordinary HTML 404s', () => {
  assert.equal(
    isWorkerOvercaptureResponse({
      pathname: '/missing-worker-page',
      status: 404,
      contentType: 'text/html; charset=utf-8',
      csp: null,
      body: '<html><body><h1>Page not found</h1><p>ecn-sdk</p></body></html>',
      workerCsp: CONTENT_SECURITY_POLICY,
    }),
    'branded ECN 404 served for non-mount path /missing-worker-page',
  );

  assert.equal(
    isWorkerOvercaptureResponse({
      pathname: '/missing-readme-page',
      status: 404,
      contentType: 'text/html; charset=utf-8',
      csp: null,
      body: '<html><body><h1>Page not found</h1><p>ReadMe product docs</p></body></html>',
      workerCsp: CONTENT_SECURITY_POLICY,
    }),
    null,
  );
});

test('ReadMe-like origin passes; Worker-like origin fails', async () => {
  const readme = await startServer((_request, response) => {
    response.writeHead(200, {
      'content-type': 'text/html; charset=utf-8',
      'content-security-policy': "default-src 'self' https:",
    });
    response.end('<!doctype html><html><body>Legion docs home</body></html>');
  });
  const pass = await runReadmeOvercaptureProbe({ origin: readme, probePath: '/', allowLoopback: true });
  assert.equal(pass.ok, true, pass.failures.join('; '));

  const worker = await startServer((_request, response) => {
    const headers = buildSecurityHeaders();
    response.writeHead(200, {
      'content-type': 'text/html; charset=utf-8',
      ...headers,
    });
    response.end(
      `<!doctype html><html><head><meta http-equiv="refresh" content="0; url=${documentationBase}" /></head>`
      + `<body><script>location.replace('${documentationBase}')</script></body></html>`,
    );
  });
  const fail = await runReadmeOvercaptureProbe({ origin: worker, probePath: '/', allowLoopback: true });
  assert.equal(fail.ok, false);
  assert.ok(fail.failures.some((item) => item.includes('over-capture')));
});

test('a loopback probe origin is rejected unless a caller opts in', () => {
  for (const origin of ['http://localhost:8787', 'https://localhost:8787', 'http://127.0.0.1:8787']) {
    assert.throws(
      () => normalizeProbeOrigin(origin),
      /probe origin must not be loopback/,
      origin,
    );
  }
  assert.equal(
    normalizeProbeOrigin('http://localhost:8787', { allowLoopback: true }),
    'http://localhost:8787',
  );
});

test('a non-loopback probe origin must still be https', () => {
  assert.throws(
    () => normalizeProbeOrigin('http://docs.picogrid.com'),
    /probe origin must be https/,
  );
  assert.throws(
    () => normalizeProbeOrigin('http://docs.picogrid.com', { allowLoopback: true }),
    /probe origin must be https/,
  );
});

test('a branded 404 is detected regardless of Content-Type letter case', () => {
  assert.match(
    isWorkerOvercaptureResponse({
      pathname: '/guides/intro/',
      status: 404,
      contentType: 'Text/HTML; charset=utf-8',
      body: '<p>Page not found</p><a href="/ecn-sdk/">ecn-sdk</a>',
      csp: 'default-src \'none\'',
      workerCsp: 'default-src \'self\'',
    }),
    /branded ECN 404/,
  );
});

test('an ordinary ReadMe 404 is not treated as Worker over-capture', () => {
  assert.equal(
    isWorkerOvercaptureResponse({
      pathname: '/guides/intro/',
      status: 404,
      contentType: 'text/html; charset=utf-8',
      body: '<h1>Page Not Found</h1><p>Try the search.</p>',
      csp: 'default-src \'none\'',
      workerCsp: 'default-src \'self\'',
    }),
    null,
  );
});
