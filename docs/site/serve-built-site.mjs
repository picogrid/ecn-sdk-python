// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import { createReadStream } from 'node:fs';
import { stat } from 'node:fs/promises';
import { createServer } from 'node:http';
import { extname, relative, resolve, sep } from 'node:path';

import { assetsOutputDirectory, assetsOutputRoot, documentationBasePath } from './site-config.mjs';

// Serve the Workers Static Assets root so /ecn-sdk/* maps under site-dist/ecn-sdk/.
const root = assetsOutputRoot;
const notFound = resolve(root, '404.html');
const host = '127.0.0.1';
const port = Number.parseInt(process.env.DOCS_PORT ?? '4321', 10);

const contentTypes = new Map([
  ['.css', 'text/css; charset=utf-8'],
  ['.html', 'text/html; charset=utf-8'],
  ['.js', 'text/javascript; charset=utf-8'],
  ['.json', 'application/json; charset=utf-8'],
  ['.png', 'image/png'],
  ['.svg', 'image/svg+xml'],
  ['.wasm', 'application/wasm'],
  ['.woff2', 'font/woff2'],
  ['.xml', 'application/xml; charset=utf-8'],
]);

function insideRoot(path) {
  const candidate = relative(root, path);
  return candidate === '' || (candidate !== '..' && !candidate.startsWith(`..${sep}`));
}

async function responseFile(url) {
  const pathname = decodeURIComponent(new URL(url, `http://${host}`).pathname);
  const target = resolve(root, `.${pathname}`);
  if (!insideRoot(target)) return null;
  const candidates = pathname.endsWith('/')
    ? [resolve(target, 'index.html')]
    : [target, resolve(target, 'index.html'), resolve(`${target}.html`)];
  for (const candidate of candidates) {
    if (!insideRoot(candidate)) continue;
    try {
      if ((await stat(candidate)).isFile()) return candidate;
    } catch {
      // Try the next deterministic candidate.
    }
  }
  return null;
}

const server = createServer(async (request, response) => {
  try {
    const file = await responseFile(request.url ?? '/');
    const selected = file ?? notFound;
    response.writeHead(file ? 200 : 404, {
      'Cache-Control': 'no-store',
      'Content-Type': contentTypes.get(extname(selected)) ?? 'application/octet-stream',
      'X-Content-Type-Options': 'nosniff',
    });
    createReadStream(selected).pipe(response);
  } catch {
    response.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' });
    response.end('documentation test server error');
  }
});

function close() {
  server.close(() => process.exit(0));
}

process.on('SIGINT', close);
process.on('SIGTERM', close);
server.listen(port, host, () => {
  const base = `http://${host}:${port}`;
  console.log(`serving ${assetsOutputDirectory}/ at ${base}/`);
  console.log(`  root redirect  ${base}/`);
  console.log(`  guide          ${base}${documentationBasePath}/`);
});
