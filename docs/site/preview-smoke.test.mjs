// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * Fixture Worker: a real HTTP server applying the same header policy, so the
 * smoke checks are exercised in CI without Cloudflare credentials.
 */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { after, test } from 'node:test';

import { normalizeBaseUrl, runPreviewSmoke, sharedHostFor } from './preview-smoke.mjs';
import { knownJourneyPathnames } from './public-url-manifest.mjs';
import { documentationBase, documentationCanonicalBase, documentationOrigin } from './site-config.mjs';

const { applyResponseHeaders } = await import(
  new URL('../cloudflare/headers.ts', import.meta.url).href
);

const HASHED_ASSET = `${documentationBase}_astro/theme.CH2UqZMg.css`;
const HASHED_SCRIPT = `${documentationBase}_astro/app.AB12cd34.js`;

const homePage = `<!doctype html><html><head>
<link rel="canonical" href="${documentationCanonicalBase}" />
<link rel="stylesheet" href="${HASHED_ASSET}" />
</head><body>Guide</body></html>`;

const notFoundPage = '<!doctype html><html><head><title>Page not found</title></head><body><h1>Page not found</h1></body></html>';

const rootRedirect = (
  await readFile(new URL('root-redirect.html', import.meta.url), 'utf8')
)
  .replaceAll('__CANONICAL__', documentationCanonicalBase)
  .replaceAll('__MOUNT__', documentationBase);

function fixtureBody(pathname) {
  if (pathname === documentationBase) return [200, 'text/html; charset=utf-8', homePage];
  if (pathname === '/') return [200, 'text/html; charset=utf-8', rootRedirect];
  if (pathname === HASHED_ASSET) return [200, 'text/css; charset=utf-8', 'body{}'];
  if (pathname === `${documentationBase}brand/picogrid-app-icon-192.png`) {
    return [200, 'image/png', 'PNG'];
  }
  if (knownJourneyPathnames.includes(pathname)) {
    return [200, 'text/html; charset=utf-8', '<!doctype html><html><body>Page</body></html>'];
  }
  return [404, 'text/html; charset=utf-8', notFoundPage];
}

/** `mutate` lets a test break exactly one part of the contract. */
async function startFixture(mutate = () => {}) {
  const server = createServer((request, response) => {
    const { pathname } = new URL(request.url, 'http://localhost');
    const [status, type, body] = fixtureBody(pathname);
    const headers = applyResponseHeaders(
      new Headers({ 'content-type': type }),
      pathname,
      status,
    );
    const outgoing = Object.fromEntries(headers.entries());
    const result = mutate({ pathname, status, headers: outgoing, body }) ?? {};
    response.writeHead(result.status ?? status, result.headers ?? outgoing);
    response.end(result.body ?? body);
  });
  await new Promise((resolveListening) => server.listen(0, '127.0.0.1', resolveListening));
  const { port } = server.address();
  return { server, baseUrl: `http://127.0.0.1:${port}` };
}

const fixtures = [];
after(async () => {
  for (const server of fixtures) {
    await new Promise((closed) => server.close(closed));
  }
});

async function smokeAgainst(mutate, options = {}) {
  const { server, baseUrl } = await startFixture(mutate);
  fixtures.push(server);
  return runPreviewSmoke({ baseUrl, ...options });
}

test('a compliant preview passes', async () => {
  const { failures, checked } = await smokeAgainst();
  assert.deepEqual(failures, []);
  assert.ok(checked.includes(documentationBase));
  assert.ok(checked.includes(HASHED_ASSET));
});

test('HTML referencing only an unhashed Astro asset fails closed', async () => {
  const unhashedAsset = `${documentationBase}_astro/theme.css`;
  const { failures } = await smokeAgainst(({ pathname, headers }) => {
    if (pathname === documentationBase) {
      return { body: homePage.replace(HASHED_ASSET, unhashedAsset) };
    }
    if (pathname === unhashedAsset) {
      return {
        status: 200,
        headers: { ...headers, 'content-type': 'text/css; charset=utf-8' },
        body: 'body{}',
      };
    }
    return {};
  });
  assert.ok(
    failures.some((failure) => failure.includes('fingerprinted')),
    failures.join('; '),
  );
});

test('a text/plain missing-page response fails', async () => {
  const { failures } = await smokeAgainst(({ status, headers }) =>
    status === 404 ? { headers: { ...headers, 'content-type': 'text/plain' } } : {},
  );
  assert.ok(
    failures.some(
      (failure) =>
        failure.includes('smoke-check-missing-page') && failure.includes('expected HTML'),
    ),
    failures.join('; '),
  );
});

test('a text/plain root redirect response fails', async () => {
  const { failures } = await smokeAgainst(({ pathname, headers }) =>
    pathname === '/' ? { headers: { ...headers, 'content-type': 'text/plain' } } : {},
  );
  assert.ok(
    failures.some(
      (failure) => failure.startsWith('/: content-type') && failure.includes('expected HTML'),
    ),
    failures.join('; '),
  );
});

test('a text/htmlish home-page response fails', async () => {
  const { failures } = await smokeAgainst(({ pathname, headers }) =>
    pathname === documentationBase
      ? { headers: { ...headers, 'content-type': 'text/htmlish; charset=utf-8' } }
      : {},
  );
  assert.ok(
    failures.some(
      (failure) =>
        failure.startsWith(`${documentationBase}: content-type`) &&
        failure.includes('expected HTML'),
    ),
    failures.join('; '),
  );
});

test('a text/htmlish journey-page response fails', async () => {
  const target = knownJourneyPathnames.find((pathname) => pathname !== documentationBase);
  const { failures } = await smokeAgainst(({ pathname, headers }) =>
    pathname === target
      ? { headers: { ...headers, 'content-type': 'text/htmlish; charset=utf-8' } }
      : {},
  );
  assert.ok(
    failures.some(
      (failure) => failure.startsWith(`${target}: content-type`) && failure.includes('expected HTML'),
    ),
    failures.join('; '),
  );
});

test('the documentation home page served at root fails', async () => {
  const { failures } = await smokeAgainst(({ pathname }) =>
    pathname === '/' ? { body: homePage } : {},
  );
  assert.ok(
    failures.some((failure) => failure.includes('redirect document')),
    failures.join('; '),
  );
});

test('a root redirect to a path below the documentation mount fails', async () => {
  const wrongTarget = `${documentationBase}introduction/`;
  const { failures } = await smokeAgainst(({ pathname }) =>
    pathname === '/' ? { body: rootRedirect.replaceAll(documentationBase, wrongTarget) } : {},
  );
  assert.ok(
    failures.some((failure) => failure.includes(`expected ${documentationBase}`)),
    failures.join('; '),
  );
});

test('a missing security header fails', async () => {
  const { failures } = await smokeAgainst(({ headers }) => {
    // Headers.entries() lowercases names, so drop the key case-insensitively.
    const { 'content-security-policy': _removed, ...rest } = headers;
    return { headers: rest };
  });
  assert.ok(
    failures.some((failure) => failure.includes('Content-Security-Policy is (absent)')),
    failures.join('; '),
  );
});

test('a journey page missing one security header fails', async () => {
  const target = knownJourneyPathnames.find((pathname) => pathname !== documentationBase);
  const { failures } = await smokeAgainst(({ pathname, headers }) => {
    if (pathname !== target) return {};
    const { 'content-security-policy': _removed, ...rest } = headers;
    return { headers: rest };
  });
  assert.ok(
    failures.some(
      (failure) =>
        failure.includes(target) && failure.includes('Content-Security-Policy is (absent)'),
    ),
    failures.join('; '),
  );
});

test('the PNG static asset missing one security header fails', async () => {
  const target = `${documentationBase}brand/picogrid-app-icon-192.png`;
  const { failures } = await smokeAgainst(({ pathname, headers }) => {
    if (pathname !== target) return {};
    const { 'x-content-type-options': _removed, ...rest } = headers;
    return { headers: rest };
  });
  assert.ok(
    failures.some(
      (failure) =>
        failure.includes(target) && failure.includes('X-Content-Type-Options is (absent)'),
    ),
    failures.join('; '),
  );
});

test('a fingerprinted asset without immutable caching fails', async () => {
  const { failures } = await smokeAgainst(({ pathname, headers }) =>
    pathname === HASHED_ASSET
      ? { headers: { ...headers, 'Cache-Control': 'public, max-age=0, must-revalidate' } }
      : {},
  );
  assert.ok(
    failures.some((failure) => failure.includes(HASHED_ASSET) && failure.includes('Cache-Control')),
    failures.join('; '),
  );
});

test('a text/cssish fingerprinted stylesheet fails', async () => {
  const { failures } = await smokeAgainst(({ pathname, headers }) =>
    pathname === HASHED_ASSET
      ? { headers: { ...headers, 'content-type': 'text/cssish; charset=utf-8' } }
      : {},
  );
  assert.ok(
    failures.some((failure) => failure.includes(HASHED_ASSET) && failure.includes('expected text/css')),
    failures.join('; '),
  );
});

test('an unapproved JavaScript media type fails', async () => {
  const { failures } = await smokeAgainst(({ pathname, headers }) => {
    if (pathname === documentationBase) {
      return { body: homePage.replace(HASHED_ASSET, HASHED_SCRIPT) };
    }
    if (pathname === HASHED_SCRIPT) {
      return {
        status: 200,
        headers: { ...headers, 'content-type': 'application/notjavascript; charset=utf-8' },
        body: 'export {};',
      };
    }
    return {};
  });
  assert.ok(
    failures.some(
      (failure) => failure.includes(HASHED_SCRIPT) && failure.includes('expected JavaScript'),
    ),
    failures.join('; '),
  );
});

test('an image/pngish static asset fails', async () => {
  const target = `${documentationBase}brand/picogrid-app-icon-192.png`;
  const { failures } = await smokeAgainst(({ pathname, headers }) =>
    pathname === target
      ? { headers: { ...headers, 'content-type': 'image/pngish; charset=utf-8' } }
      : {},
  );
  assert.ok(
    failures.some((failure) => failure.includes(target) && failure.includes('expected PNG')),
    failures.join('; '),
  );
});

test('the missing page must use the revalidation cache policy', async () => {
  const { failures } = await smokeAgainst(({ pathname, headers }) =>
    pathname.includes('smoke-check-missing-page')
      ? { headers: { ...headers, 'Cache-Control': 'public, max-age=31536000, immutable' } }
      : {},
  );
  assert.ok(
    failures.some(
      (failure) =>
        failure.includes('smoke-check-missing-page') && failure.includes('Cache-Control'),
    ),
    failures.join('; '),
  );
});

test('the root redirect must use the revalidation cache policy', async () => {
  const { failures } = await smokeAgainst(({ pathname, headers }) =>
    pathname === '/'
      ? { headers: { ...headers, 'Cache-Control': 'public, max-age=31536000, immutable' } }
      : {},
  );
  assert.ok(
    failures.some((failure) => failure.startsWith('/: Cache-Control')),
    failures.join('; '),
  );
});

test('an SPA-style 404 rewrite to the index fails', async () => {
  const { failures } = await smokeAgainst(({ status }) =>
    status === 404 ? { status: 200, body: homePage } : {},
  );
  assert.ok(
    failures.some((failure) => failure.includes('expected 404')),
    failures.join('; '),
  );
  assert.ok(
    failures.some((failure) => failure.includes('served the index page')),
    failures.join('; '),
  );
});

test('a journey page rewritten to the index fails', async () => {
  const target = knownJourneyPathnames.find((pathname) => pathname !== documentationBase);
  const { failures } = await smokeAgainst(({ pathname }) =>
    pathname === target ? { body: homePage } : {},
  );
  assert.ok(
    failures.some((failure) => failure.includes(target) && failure.includes('served the index page')),
    failures.join('; '),
  );
});

test('a cross-origin fingerprinted asset fails', async () => {
  const foreign = 'https://example.invalid/ecn-sdk/_astro/theme.abcdefgh.css';
  const { failures } = await smokeAgainst(({ pathname }) =>
    pathname === documentationBase
      ? {
          body: homePage.replace(HASHED_ASSET, foreign),
        }
      : {},
  );
  assert.ok(
    failures.some((failure) => failure.includes('differs from preview origin')),
    failures.join('; '),
  );
});

test('a broken guide page fails even when the index is fine', async () => {
  const target = knownJourneyPathnames.find((pathname) => pathname !== documentationBase);
  const { failures } = await smokeAgainst(({ pathname }) =>
    pathname === target ? { status: 500 } : {},
  );
  assert.ok(
    failures.some((failure) => failure.startsWith(`${target}: status 500`)),
    failures.join('; '),
  );
});

test('HTML that lost its canonical origin fails', async () => {
  const { failures } = await smokeAgainst(({ pathname }) =>
    pathname === documentationBase
      ? { body: homePage.replace(documentationCanonicalBase, 'https://example.invalid/') }
      : {},
  );
  assert.ok(
    failures.some((failure) => failure.includes('canonical origin')),
    failures.join('; '),
  );
});

test('a home canonical below the configured canonical URL fails', async () => {
  const wrongCanonical = `${documentationCanonicalBase}obsolete/`;
  const { failures } = await smokeAgainst(({ pathname }) =>
    pathname === documentationBase
      ? { body: homePage.replace(documentationCanonicalBase, wrongCanonical) }
      : {},
  );
  assert.ok(
    failures.some((failure) => failure.includes('canonical')),
    failures.join('; '),
  );
});

test('a canonical URL elsewhere cannot mask a wrong canonical link', async () => {
  const { failures } = await smokeAgainst(({ pathname }) =>
    pathname === documentationBase
      ? {
          body: homePage
            .replace(documentationCanonicalBase, 'https://example.invalid/')
            .replace('</body>', `${documentationCanonicalBase}</body>`),
        }
      : {},
  );
  assert.ok(
    failures.some((failure) => failure.includes('canonical origin')),
    failures.join('; '),
  );
});

test('base URL validation', async (t) => {
  await t.test('requires a URL', async () => {
    await assert.rejects(runPreviewSmoke({ baseUrl: '' }), /base URL is required/);
  });

  await t.test('rejects plaintext for a remote preview', async () => {
    await assert.rejects(runPreviewSmoke({ baseUrl: 'http://example.invalid' }), /must be https/);
  });

  await t.test('rejects unsupported schemes even for loopback hosts', () => {
    assert.throws(() => normalizeBaseUrl('ftp://localhost'), /unsupported scheme ftp:/);
    assert.throws(() => normalizeBaseUrl('ws://localhost'), /unsupported scheme ws:/);
  });

  await t.test('accepts HTTPS for a remote preview', () => {
    assert.equal(normalizeBaseUrl('https://example.invalid/path'), 'https://example.invalid');
  });
});

test('a request that never returns headers times out', async () => {
  await assert.rejects(
    runPreviewSmoke({
      baseUrl: 'https://example.invalid',
      requestTimeoutMs: 20,
      fetchImpl: () => new Promise(() => {}),
    }),
    /timed out/,
  );
});

test('a response body that never finishes times out', async () => {
  await assert.rejects(
    runPreviewSmoke({
      baseUrl: 'https://example.invalid',
      requestTimeoutMs: 20,
      fetchImpl: async () => ({
        status: 200,
        headers: new Headers(),
        text: () => new Promise(() => {}),
      }),
    }),
    /timed out/,
  );
});

// On the canonical host the Worker owns only the mount; the rest of the host
// belongs to the existing documentation product and is not ours to assert.
const BARE_MOUNT = documentationBase.replace(/\/$/, '');

test('on a shared host the root outside the mount is not asserted', async () => {
  const { failures, checked } = await smokeAgainst(({ pathname, headers }) => {
    if (pathname === BARE_MOUNT) {
      return { status: 308, headers: { ...headers, location: documentationBase }, body: '' };
    }
    // The other product answers the root, exactly as it will after cutover.
    if (pathname === '/') {
      return { status: 200, headers: { ...headers, 'content-type': 'text/html' }, body: '<h1>Other product</h1>' };
    }
    return {};
  }, { sharedHost: true });
  assert.deepEqual(failures, []);
  assert.ok(!checked.includes('/'), `expected the root to be left alone, checked ${checked.join(', ')}`);
  assert.ok(checked.includes(BARE_MOUNT));
});

test('on a shared host a missing bare-mount route fails closed', async () => {
  const { failures } = await smokeAgainst(({ pathname, headers }) => {
    if (pathname === BARE_MOUNT) {
      // No bare route bound: the other product answers with its own 404.
      return { status: 404, headers: { ...headers, 'content-type': 'text/html' }, body: 'Page Not Found' };
    }
    return {};
  }, { sharedHost: true });
  assert.ok(
    failures.some((failure) => failure.includes(BARE_MOUNT) && failure.includes('308')),
    failures.join('; '),
  );
});

test('on a shared host a bare mount redirecting elsewhere fails closed', async () => {
  const { failures } = await smokeAgainst(({ pathname, headers }) => {
    if (pathname === BARE_MOUNT) {
      return { status: 308, headers: { ...headers, location: '/somewhere-else/' }, body: '' };
    }
    return {};
  }, { sharedHost: true });
  assert.ok(
    failures.some((failure) => failure.includes('redirects to /somewhere-else/')),
    failures.join('; '),
  );
});

test('a bootstrap origin still asserts the root redirect document', async () => {
  const { checked } = await smokeAgainst();
  assert.ok(checked.includes('/'), 'the bootstrap origin owns the whole host');
});

test('the shared-host mode is derived from the base URL, not configured', () => {
  assert.equal(sharedHostFor(documentationOrigin), true);
  assert.equal(sharedHostFor(`${documentationOrigin}/`), true);
  assert.equal(sharedHostFor('https://ecn.example'), false);
  assert.equal(sharedHostFor('http://127.0.0.1:8787'), false);
});

test('on a shared host a bare-mount redirect missing a security header fails closed', async () => {
  const { failures } = await smokeAgainst(({ pathname, headers }) => {
    if (pathname === BARE_MOUNT) {
      const { 'x-content-type-options': _dropped, ...withoutNosniff } = headers;
      return { status: 308, headers: { ...withoutNosniff, location: documentationBase }, body: '' };
    }
    return {};
  }, { sharedHost: true });
  assert.ok(
    failures.some(
      (failure) => failure.includes(BARE_MOUNT) && /x-content-type-options/i.test(failure),
    ),
    failures.join('; '),
  );
});
