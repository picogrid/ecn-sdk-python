// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * Fixture crawl: local HTTP servers stand in for production ReadMe and a
 * candidate Worker so A6 never needs live network or Cloudflare hosts in CI.
 */
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { createServer } from 'node:http';
import { after, test } from 'node:test';
import { promisify } from 'node:util';

import {
  crawlReadmeForEcnLinks,
  extractEcnSdkTarget,
  isCrawlableReadmePath,
  isEcnSdkPath,
  normalizeCandidateBaseUrl,
  resolveDocumentLink,
  rewriteEcnTargetToCandidate,
  runReadmeEcnCrawler,
  validateEcnTargetsOnCandidate,
} from './readme-ecn-crawler.mjs';
import { documentationBase, documentationBasePath, documentationOrigin } from './site-config.mjs';

const execFileAsync = promisify(execFile);

const fixtures = [];
after(async () => {
  for (const server of fixtures) {
    server.closeAllConnections();
    await new Promise((closed) => server.close(closed));
  }
});

async function startServer(handler) {
  const server = createServer(handler);
  await new Promise((listening) => server.listen(0, '127.0.0.1', listening));
  fixtures.push(server);
  const { port } = server.address();
  return { origin: `http://127.0.0.1:${port}`, port };
}

test('path helpers recognize the /ecn-sdk mount only', () => {
  assert.equal(isEcnSdkPath(documentationBasePath), true);
  assert.equal(isEcnSdkPath(`${documentationBasePath}/`), true);
  assert.equal(isEcnSdkPath(`${documentationBasePath}/getting-started/installation/`), true);
  assert.equal(isEcnSdkPath('/'), false);
  assert.equal(isEcnSdkPath('/concepts/auth/'), false);
  assert.equal(isEcnSdkPath('/ecn-sdkish/'), false);
  assert.equal(isCrawlableReadmePath('/guides/intro/'), true);
  assert.equal(isCrawlableReadmePath(`${documentationBase}foo/`), false);
  assert.equal(isCrawlableReadmePath('/static/app.css'), false);
});

test('resolve and extract ECN targets from absolute and relative hrefs', () => {
  const page = `${documentationOrigin}/product/overview/`;
  const absolute = resolveDocumentLink(`${documentationOrigin}${documentationBase}quickstarts/observe-data/`, page);
  assert.equal(
    extractEcnSdkTarget(absolute, documentationOrigin),
    `${documentationBasePath}/quickstarts/observe-data/`,
  );
  const relative = resolveDocumentLink(`${documentationBase}reference/api/`, page);
  assert.equal(
    extractEcnSdkTarget(relative, documentationOrigin),
    `${documentationBasePath}/reference/api/`,
  );
  const offHost = resolveDocumentLink('https://example.invalid/ecn-sdk/x/', page);
  assert.equal(extractEcnSdkTarget(offHost, documentationOrigin), null);
  assert.equal(
    rewriteEcnTargetToCandidate(`${documentationBasePath}/`, 'http://127.0.0.1:9'),
    `http://127.0.0.1:9${documentationBase}`,
  );
});

test('resolveDocumentLink rejects fragments, empty hrefs, and disallowed schemes', () => {
  const page = `${documentationOrigin}/product/overview/`;
  for (const href of [
    '#anchor',
    'mailto:a@b.invalid',
    'javascript:alert(1)',
    'data:text/html,x',
    'tel:+15550100',
    '',
  ]) {
    assert.equal(resolveDocumentLink(href, page), null, href);
  }
});

test('normalizeCandidateBaseUrl rejects non-https remote schemes', () => {
  assert.throws(
    () => normalizeCandidateBaseUrl('http://example.invalid'),
    /must be https/,
  );
  assert.throws(
    () => normalizeCandidateBaseUrl('ftp://localhost'),
    /must be https/,
  );
  assert.equal(normalizeCandidateBaseUrl('https://example.invalid/path'), 'https://example.invalid');
  assert.equal(normalizeCandidateBaseUrl('http://127.0.0.1:9/'), 'http://127.0.0.1:9');
});

test('decodeHref unescapes attribute ampersands used in query strings', () => {
  const page = `${documentationOrigin}/product/overview/`;
  const absolute = resolveDocumentLink(
    `${documentationBase}x?a=1&amp;b=2`,
    page,
  );
  assert.equal(
    extractEcnSdkTarget(absolute, documentationOrigin),
    `${documentationBasePath}/x?a=1&b=2`,
  );
});

test('compliant ReadMe → candidate mapping passes', async () => {
  const candidate = await startServer((request, response) => {
    const { pathname } = new URL(request.url, 'http://localhost');
    if (pathname === `${documentationBasePath}/` || pathname === `${documentationBasePath}/getting-started/installation/`) {
      response.writeHead(200, { 'content-type': 'text/html' });
      response.end('<html><body>ok</body></html>');
      return;
    }
    response.writeHead(404, { 'content-type': 'text/html' });
    response.end('missing');
  });

  const source = await startServer((request, response) => {
    const { pathname } = new URL(request.url, 'http://localhost');
    if (pathname === '/') {
      response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      response.end(`<!doctype html><html><body>
        <a href="/guides/auth/">Auth guide</a>
        <a href="${documentationBase}">ECN home</a>
      </body></html>`);
      return;
    }
    if (pathname === '/guides/auth/') {
      response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      response.end(`<!doctype html><html><body>
        <a href="${documentationBase}getting-started/installation/">Install</a>
        <a href="/other/">Other</a>
      </body></html>`);
      return;
    }
    if (pathname === '/other/') {
      response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      response.end('<!doctype html><html><body>no ecn</body></html>');
      return;
    }
    response.writeHead(404);
    response.end('no');
  });

  const result = await runReadmeEcnCrawler({
    sourceBaseUrl: source.origin,
    candidateBaseUrl: candidate.origin,
  });
  assert.equal(result.ok, true, result.failures.join('; '));
  assert.equal(result.ecnLinkCount, 2);
  assert.deepEqual(
    [...result.pagesVisited].sort(),
    [`${source.origin}/`, `${source.origin}/guides/auth/`, `${source.origin}/other/`].sort(),
  );
  assert.deepEqual(result.failures, []);
});

test('missing candidate ECN path fails with source attribution', async () => {
  const candidate = await startServer((_request, response) => {
    response.writeHead(404, { 'content-type': 'text/html' });
    response.end('nope');
  });
  const source = await startServer((request, response) => {
    const { pathname } = new URL(request.url, 'http://localhost');
    if (pathname === '/') {
      response.writeHead(200, { 'content-type': 'text/html' });
      response.end(`<a href="${documentationBase}missing-page/">broken</a>`);
      return;
    }
    response.writeHead(404);
    response.end('');
  });

  const result = await runReadmeEcnCrawler({
    sourceBaseUrl: source.origin,
    candidateBaseUrl: candidate.origin,
  });
  assert.equal(result.ok, false);
  assert.ok(
    result.failures.some((failure) => failure.includes(`${documentationBasePath}/missing-page/`)),
    result.failures.join('; '),
  );
  assert.ok(
    result.failures.some((failure) => failure.includes('from ReadMe:')),
    result.failures.join('; '),
  );
});

test('empty crawl fails closed', async () => {
  const candidate = await startServer((_request, response) => {
    response.writeHead(200);
    response.end('ok');
  });
  const source = await startServer((_request, response) => {
    response.writeHead(503, { 'content-type': 'text/html' });
    response.end('down');
  });

  const result = await runReadmeEcnCrawler({
    sourceBaseUrl: source.origin,
    candidateBaseUrl: candidate.origin,
  });
  assert.equal(result.ok, false);
  assert.ok(result.failures.some((failure) => failure.includes('zero HTML pages')));
  assert.ok(
    result.failures.some((failure) => failure.includes('crawl status 503')),
    result.failures.join('; '),
  );
  // The zero-page path promotes crawl diagnostics to failures, so repeating
  // them as warnings would print each one twice.
  assert.deepEqual(result.warnings, []);
});

test('zero ECN links is ok unless requireEcnLinks', async () => {
  const candidate = await startServer((_request, response) => {
    response.writeHead(200);
    response.end('ok');
  });
  const source = await startServer((request, response) => {
    if (new URL(request.url, 'http://localhost').pathname === '/') {
      response.writeHead(200, { 'content-type': 'text/html' });
      response.end('<a href="/only-readme/">x</a>');
      return;
    }
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end('done');
  });

  const soft = await runReadmeEcnCrawler({
    sourceBaseUrl: source.origin,
    candidateBaseUrl: candidate.origin,
  });
  assert.equal(soft.ok, true, soft.failures.join('; '));
  assert.equal(soft.ecnLinkCount, 0);
  assert.deepEqual(soft.warnings, []);

  const strict = await runReadmeEcnCrawler({
    sourceBaseUrl: source.origin,
    candidateBaseUrl: candidate.origin,
    requireEcnLinks: true,
  });
  assert.equal(strict.ok, false);
  assert.ok(strict.failures.some((failure) => failure.includes('no /ecn-sdk links')));
  assert.deepEqual(strict.warnings, []);
});

test('does not follow /ecn-sdk pages as ReadMe crawl depth', async () => {
  let candidateHits = 0;
  const candidate = await startServer((request, response) => {
    candidateHits += 1;
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end('guide');
  });
  const source = await startServer((request, response) => {
    const { pathname } = new URL(request.url, 'http://localhost');
    if (pathname === '/') {
      response.writeHead(200, { 'content-type': 'text/html' });
      // Link into ECN; crawler must validate on candidate, not fetch as source depth.
      response.end(`<a href="${documentationBase}deep/">deep</a>`);
      return;
    }
    // If crawl mistakenly fetched source /ecn-sdk/deep/, this would be served —
    // and we would enqueue more. Count via pagesVisited only containing source /.
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end(`<a href="${documentationBase}should-not-crawl/">no</a>`);
  });

  const result = await runReadmeEcnCrawler({
    sourceBaseUrl: source.origin,
    candidateBaseUrl: candidate.origin,
  });
  assert.equal(result.ok, true, result.failures.join('; '));
  assert.deepEqual(result.pagesVisited, [`${source.origin}/`]);
  assert.equal(result.ecnLinkCount, 1);
  assert.equal(candidateHits, 1);
});

test('partial crawl failures are surfaced as non-failing warnings', async () => {
  const candidate = await startServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end('ok');
  });
  const source = await startServer((request, response) => {
    const { pathname } = new URL(request.url, 'http://localhost');
    if (pathname === '/') {
      response.writeHead(200, { 'content-type': 'text/html' });
      response.end(`
        <a href="/unavailable/">Unavailable</a>
        <a href="${documentationBase}available/">Available ECN page</a>
      `);
      return;
    }
    response.writeHead(503, { 'content-type': 'text/html' });
    response.end('down');
  });

  const result = await runReadmeEcnCrawler({
    sourceBaseUrl: source.origin,
    candidateBaseUrl: candidate.origin,
  });
  assert.equal(result.ok, true, result.failures.join('; '));
  assert.deepEqual(result.failures, []);
  assert.ok(
    result.warnings.some((warning) => warning.includes('crawl status 503')),
    result.warnings.join('; '),
  );
});

test('concurrency must be a finite positive integer', async () => {
  const candidate = await startServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end('ok');
  });
  const source = await startServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end('No ECN links');
  });

  for (const concurrency of [0, -1, Number.NaN, 1.5]) {
    await assert.rejects(
      runReadmeEcnCrawler({
        sourceBaseUrl: source.origin,
        candidateBaseUrl: candidate.origin,
        concurrency,
      }),
      /concurrency must be a finite integer greater than or equal to 1/,
    );
  }
});

test('max-pages must be a finite positive integer', async () => {
  const candidate = await startServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end('ok');
  });
  const source = await startServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end('No ECN links');
  });

  for (const [maxPages, displayedValue] of [
    [Number('abc'), 'NaN'],
    [0, '0'],
    [-1, '-1'],
    [1.5, '1.5'],
    ['', '""'],
  ]) {
    await assert.rejects(
      runReadmeEcnCrawler({
        sourceBaseUrl: source.origin,
        candidateBaseUrl: candidate.origin,
        maxPages,
      }),
      (error) => {
        assert.match(error.message, /max-pages must be a finite integer greater than or equal to 1/);
        assert.match(error.message, new RegExp(`received ${displayedValue.replace('.', '\\.')}$`));
        assert.doesNotMatch(error.message, /zero HTML pages/);
        return true;
      },
    );
  }
});

test('direct crawl rejects invalid max-pages values before fetching', async () => {
  for (const maxPages of [0, Number.NaN, '10']) {
    let fetchCalls = 0;
    await assert.rejects(
      crawlReadmeForEcnLinks({
        sourceBaseUrl: 'http://127.0.0.1:9',
        maxPages,
        fetchImpl: async () => {
          fetchCalls += 1;
          throw new Error('must not fetch');
        },
      }),
      /max-pages must be a finite integer greater than or equal to 1/,
    );
    assert.equal(fetchCalls, 0);
  }
});

test('crawl fetch budget includes failed and non-HTML pages', async () => {
  const observedPaths = [];
  const source = await startServer((request, response) => {
    const { pathname } = new URL(request.url, 'http://localhost');
    observedPaths.push(pathname);
    if (pathname === '/') {
      response.writeHead(200, { 'content-type': 'text/html' });
      response.end(
        Array.from({ length: 5 }, (_, index) => `<a href="/page-${index}/">page</a>`).join(''),
      );
      return;
    }
    if (pathname === '/page-0/') {
      response.writeHead(503, { 'content-type': 'text/html' });
      response.end('down');
      return;
    }
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end('{}');
  });

  const maxPages = 3;
  await crawlReadmeForEcnLinks({
    sourceBaseUrl: source.origin,
    maxPages,
  });

  assert.equal(observedPaths.length, maxPages);
});

test('crawl follows href attributes but still discovers ECN targets in src attributes', async () => {
  const observedPaths = [];
  const source = await startServer((request, response) => {
    const { pathname } = new URL(request.url, 'http://localhost');
    observedPaths.push(pathname);
    if (pathname === '/') {
      response.writeHead(200, { 'content-type': 'text/html' });
      response.end(`
        <script src="/analytics"></script>
        <iframe src="/embed"></iframe>
        <img src="${documentationBase}from-src/">
        <a href="/linked/">linked page</a>
      `);
      return;
    }
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end('done');
  });

  const result = await crawlReadmeForEcnLinks({
    sourceBaseUrl: source.origin,
    maxPages: 5,
  });

  assert.deepEqual(observedPaths, ['/', '/linked/']);
  assert.deepEqual([...result.ecnTargets.keys()], [`${documentationBasePath}/from-src/`]);
});

test('candidate validation tolerates locked, disturbed, and rejecting HEAD bodies', async () => {
  for (const state of ['locked', 'disturbed', 'rejecting']) {
    let cancelCalls = 0;
    const body = {
      locked: state === 'locked',
      async cancel() {
        cancelCalls += 1;
        throw new TypeError('Invalid state: ReadableStream is locked');
      },
    };
    const result = await validateEcnTargetsOnCandidate({
      candidateBaseUrl: 'http://127.0.0.1:9',
      ecnTargets: new Map([[`${documentationBasePath}/${state}/`, new Set(['/source/'])]]),
      fetchImpl: async () => ({
        body,
        bodyUsed: state === 'disturbed',
        headers: new Headers(),
        ok: true,
        status: 200,
        url: `http://127.0.0.1:9${documentationBase}${state}/`,
      }),
    });

    assert.deepEqual(result.failures, [], state);
    assert.equal(cancelCalls, state === 'rejecting' ? 1 : 0, state);
  }
});

test('malformed redirect locations still cancel the response body', async () => {
  let cancelCalls = 0;
  const result = await crawlReadmeForEcnLinks({
    sourceBaseUrl: 'http://127.0.0.1:9',
    fetchImpl: async (url) => ({
      body: {
        locked: false,
        async cancel() {
          cancelCalls += 1;
        },
      },
      bodyUsed: false,
      headers: new Headers({ location: 'http://' }),
      ok: false,
      status: 302,
      url,
    }),
  });

  assert.equal(cancelCalls, 1);
  assert.match(result.crawlFailures.join('\n'), /Invalid URL/);
});

test('candidate validation rejects redirects to non-HTTP schemes', async () => {
  const candidate = await startServer((_request, response) => {
    response.writeHead(302, { location: 'data:text/html,ok' });
    response.end();
  });
  const result = await validateEcnTargetsOnCandidate({
    candidateBaseUrl: candidate.origin,
    ecnTargets: new Map([[`${documentationBasePath}/redirect/`, new Set(['/source/'])]]),
  });

  assert.equal(result.failures.length, 1);
  assert.match(result.failures[0], /redirect uses unsupported protocol/);
});

test('candidate validation retries HEAD 405, 403, and 404 responses with GET', async () => {
  for (const headStatus of [405, 403, 404]) {
    const observedMethods = [];
    const candidate = await startServer((request, response) => {
      observedMethods.push(request.method);
      response.writeHead(request.method === 'HEAD' ? headStatus : 200, {
        'content-type': 'text/html',
      });
      response.end(request.method === 'HEAD' ? '' : 'ok');
    });

    const result = await validateEcnTargetsOnCandidate({
      candidateBaseUrl: candidate.origin,
      ecnTargets: new Map([[
        `${documentationBasePath}/fallback-${headStatus}/`,
        new Set(['/source/']),
      ]]),
    });

    assert.deepEqual(result.failures, [], String(headStatus));
    assert.deepEqual(observedMethods, ['HEAD', 'GET'], String(headStatus));
  }
});

test('candidate validation does not retry a GET 404 after HEAD fallback', async () => {
  const observedMethods = [];
  const candidate = await startServer((request, response) => {
    observedMethods.push(request.method);
    response.writeHead(request.method === 'HEAD' ? 403 : 404, {
      'content-type': 'text/html',
    });
    response.end(request.method === 'HEAD' ? '' : 'missing');
  });

  const result = await validateEcnTargetsOnCandidate({
    candidateBaseUrl: candidate.origin,
    ecnTargets: new Map([[
      `${documentationBasePath}/fallback-get-404/`,
      new Set(['/source/']),
    ]]),
  });

  assert.equal(result.failures.length, 1);
  assert.deepEqual(observedMethods, ['HEAD', 'GET']);
});

test('a valid max-pages value still crawls', async () => {
  const candidate = await startServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end('ok');
  });
  const source = await startServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end('No ECN links');
  });

  const result = await runReadmeEcnCrawler({
    sourceBaseUrl: source.origin,
    candidateBaseUrl: candidate.origin,
    maxPages: 1,
  });
  assert.equal(result.ok, true, result.failures.join('; '));
  assert.deepEqual(result.pagesVisited, [`${source.origin}/`]);
});

test('CLI rejects invalid max-pages input before crawling', async () => {
  for (const [arguments_, environment, displayedValue] of [
    [['--max-pages=abc'], {}, 'NaN'],
    [[], { README_CRAWL_MAX_PAGES: '' }, '0'],
  ]) {
    await assert.rejects(
      execFileAsync(
        process.execPath,
        [
          'site/check-readme-ecn-links.mjs',
          '--base-url=https://example.invalid',
          ...arguments_,
        ],
        {
          cwd: new URL('..', import.meta.url),
          env: { ...process.env, ...environment },
          timeout: 3_000,
        },
      ),
      (error) => {
        assert.equal(error.killed, false, 'CLI validation should fail before the timeout');
        assert.equal(error.signal, null, 'CLI validation should exit without a timeout signal');
        assert.match(error.stderr, /max-pages must be a finite integer greater than or equal to 1/);
        assert.match(error.stderr, new RegExp(`received ${displayedValue}$`, 'm'));
        assert.doesNotMatch(error.stderr, /zero HTML pages/);
        return true;
      },
    );
  }
});

test('failure attribution reports omitted ReadMe source pages', async () => {
  const candidate = await startServer((_request, response) => {
    response.writeHead(404, { 'content-type': 'text/html' });
    response.end('missing');
  });
  const target = `${documentationBasePath}/missing/`;
  const sourcePages = [
    'https://example.invalid/one/',
    'https://example.invalid/two/',
    'https://example.invalid/three/',
    'https://example.invalid/four/',
  ];

  const truncated = await validateEcnTargetsOnCandidate({
    candidateBaseUrl: candidate.origin,
    ecnTargets: new Map([[target, new Set(sourcePages)]]),
  });
  assert.match(truncated.failures[0], /… and 1 more/);

  const complete = await validateEcnTargetsOnCandidate({
    candidateBaseUrl: candidate.origin,
    ecnTargets: new Map([[target, new Set(sourcePages.slice(0, 3))]]),
  });
  assert.doesNotMatch(complete.failures[0], /… and \d+ more/);
});

test('candidate results preserve sorted target order regardless of response order', async () => {
  const candidate = await startServer(async (request, response) => {
    const { pathname } = new URL(request.url, 'http://localhost');
    if (pathname.endsWith('/a/')) {
      await new Promise((resolve) => setTimeout(resolve, 30));
    }
    response.writeHead(pathname.endsWith('/b/') ? 502 : 503, { 'content-type': 'text/html' });
    response.end('down');
  });
  const source = await startServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end(`
      <a href="${documentationBase}b/">B</a>
      <a href="${documentationBase}a/">A</a>
    `);
  });

  const result = await runReadmeEcnCrawler({
    sourceBaseUrl: source.origin,
    candidateBaseUrl: candidate.origin,
    concurrency: 2,
  });
  assert.deepEqual(result.checked, [
    `${candidate.origin}${documentationBase}a/`,
    `${candidate.origin}${documentationBase}b/`,
  ]);
  assert.deepEqual(result.failures, [...result.failures].sort());
});

test('redirect aliases are parsed once under their canonical page URL', async () => {
  const candidate = await startServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end('ok');
  });
  const source = await startServer((request, response) => {
    const { pathname } = new URL(request.url, 'http://localhost');
    if (pathname === '/') {
      response.writeHead(200, { 'content-type': 'text/html' });
      response.end('<a href="/alias-a/">A</a><a href="/alias-b/">B</a>');
      return;
    }
    if (pathname === '/alias-a/' || pathname === '/alias-b/') {
      response.writeHead(302, { location: '/canonical/' });
      response.end();
      return;
    }
    if (pathname === '/canonical/') {
      response.writeHead(200, { 'content-type': 'text/html' });
      response.end(`<a href="${documentationBase}canonical-target/">ECN</a>`);
      return;
    }
    response.writeHead(404);
    response.end('missing');
  });

  const result = await runReadmeEcnCrawler({
    sourceBaseUrl: source.origin,
    candidateBaseUrl: candidate.origin,
  });
  assert.equal(result.ok, true, result.failures.join('; '));
  assert.deepEqual(result.pagesVisited, [`${source.origin}/`, `${source.origin}/canonical/`]);
  assert.equal(result.ecnLinkCount, 1);
});

test('crawl does not follow redirects outside the source origin', async () => {
  let externalRequests = 0;
  let sourceOrigin;
  const external = await startServer((_request, response) => {
    externalRequests += 1;
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end(`<a href="${sourceOrigin}${documentationBase}external-target/">ECN</a>`);
  });
  const source = await startServer((_request, response) => {
    response.writeHead(302, { location: `${external.origin}/external/` });
    response.end();
  });
  sourceOrigin = source.origin;

  const result = await crawlReadmeForEcnLinks({
    sourceBaseUrl: source.origin,
  });

  assert.equal(externalRequests, 0);
  assert.equal(result.ecnTargets.size, 0);
  assert.match(result.crawlFailures.join('\n'), /redirect leaves origin/);
});

test('candidate probes do not follow redirects outside the candidate origin', async () => {
  let externalRequests = 0;
  const external = await startServer((_request, response) => {
    externalRequests += 1;
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end('<p>impostor</p>');
  });
  const candidate = await startServer((_request, response) => {
    response.writeHead(302, { location: `${external.origin}/elsewhere/` });
    response.end();
  });
  const source = await startServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end(`<a href="${documentationBase}guide/">ECN</a>`);
  });

  const result = await runReadmeEcnCrawler({
    sourceBaseUrl: source.origin,
    candidateBaseUrl: candidate.origin,
  });

  assert.equal(externalRequests, 0);
  assert.equal(result.ok, false);
  assert.match(result.failures.join('\n'), /redirect leaves origin/);
});

test('a non-HTML seed response is reported with its content type', async () => {
  const source = await startServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end('{"detail":"bot check"}');
  });
  const candidate = await startServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'text/html' });
    response.end('<p>ok</p>');
  });

  const result = await runReadmeEcnCrawler({
    sourceBaseUrl: source.origin,
    candidateBaseUrl: candidate.origin,
  });

  assert.equal(result.ok, false);
  assert.match(result.failures.join('\n'), /skipped non-HTML content type application\/json/);
});

test('a same-origin redirect loop is bounded rather than spinning', async () => {
  let hits = 0;
  const source = await startServer((request, response) => {
    hits += 1;
    const next = Number(new URL(request.url, 'http://localhost').searchParams.get('n') ?? '0') + 1;
    response.writeHead(302, { location: `/?n=${next}` });
    response.end();
  });

  const result = await crawlReadmeForEcnLinks({ sourceBaseUrl: source.origin });

  assert.match(result.crawlFailures.join('\n'), /too many redirects/);
  assert.ok(hits <= 12, `followed ${hits} redirects`);
  assert.deepEqual(result.pagesVisited, []);
});
