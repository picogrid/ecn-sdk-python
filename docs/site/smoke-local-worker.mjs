// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * Smoke the Worker locally: run `wrangler dev` over the built site and apply
 * the preview checks to it. Needs no Cloudflare credentials, so pull requests
 * exercise real Workers Static Assets behaviour (MIME, 404 page, caching)
 * before any candidate is uploaded.
 */
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

import { runPreviewSmoke } from './preview-smoke.mjs';
import { documentationBase } from './site-config.mjs';

const PORT = Number(process.env.SMOKE_PORT ?? 8788);
const HOST = '127.0.0.1';
const READY_TIMEOUT_MS = 60_000;
const baseUrl = `http://${HOST}:${PORT}`;

async function waitUntilServing(signal) {
  while (true) {
    signal.throwIfAborted();
    try {
      const response = await fetch(`${baseUrl}${documentationBase}`, { signal });
      if (response.ok) return;
    } catch {
      if (signal.aborted) {
        throw signal.reason;
      }
      // Server not listening yet.
    }
    try {
      await delay(500, undefined, { signal });
    } catch {
      throw signal.reason;
    }
  }
}

const worker = spawn(
  'npx',
  ['wrangler', 'dev', '--port', String(PORT), '--ip', HOST],
  { stdio: ['ignore', 'inherit', 'inherit'] },
);

const readiness = new AbortController();
const abortReadiness = (error) => {
  if (!readiness.signal.aborted) {
    readiness.abort(error);
  }
};
const readinessTimeout = setTimeout(
  () =>
    abortReadiness(
      new Error(`wrangler dev did not serve ${documentationBase} within 60s`),
    ),
  READY_TIMEOUT_MS,
);
worker.once('error', (error) => {
  abortReadiness(new Error(`wrangler dev failed to start: ${error.message}`));
});
worker.once('exit', (code, signal) => {
  const result = signal ? `signal ${signal}` : `exit code ${code}`;
  abortReadiness(new Error(`wrangler dev exited before serving ${documentationBase} (${result})`));
});

try {
  await waitUntilServing(readiness.signal);
  const { failures, checked } = await runPreviewSmoke({ baseUrl });
  for (const failure of failures) {
    process.stderr.write(`${failure}\n`);
  }
  if (failures.length > 0) {
    process.stderr.write(`local Worker smoke failed: ${failures.length} problem(s)\n`);
    process.exitCode = 1;
  } else {
    process.stdout.write(`local Worker smoke passed: ${checked.length} requests\n`);
  }
} catch (error) {
  process.stderr.write(`local Worker smoke could not run: ${error.message}\n`);
  process.exitCode = 1;
} finally {
  clearTimeout(readinessTimeout);
  abortReadiness(new Error('local Worker smoke finished'));
  if (worker.exitCode === null && worker.signalCode === null) {
    worker.kill();
  }
}
