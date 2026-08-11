// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { bootstrapOriginFrom, smokeTarget } from './worker-smoke-target.mjs';
import { documentationOrigin } from './site-config.mjs';

// A synthetic Worker name and bootstrap host: what is under test is the
// matching rule, not the production names.
const WORKER = 'ecn';
const BOOTSTRAP = 'https://ecn.example';

// The real shape: Wrangler's telemetry notice sits alongside the deployed URL,
// which is what made an earlier "the one origin that is not the docs host"
// rule resolve two candidates and fail a live deploy.
const DEPLOY_LOG = [
  ' ⛅️ wrangler 4.42.2',
  'Cloudflare collects anonymous telemetry about your usage of Wrangler.'
  + ' Learn more at https://github.com/cloudflare/workers-sdk/tree/main/packages/wrangler/telemetry.md',
  `Uploaded ${WORKER} (10.98 sec)`,
  `Deployed ${WORKER} triggers (0.49 sec)`,
  `  ${BOOTSTRAP}`,
  'Current Version ID: 0d1d18df-a3e4-420a-b26b-d67ebe611b26',
].join('\n');

test('a bootstrap origin is preferred while workers_dev is enabled', () => {
  assert.equal(
    smokeTarget({ wrangler: { name: WORKER, workers_dev: true }, deployLog: DEPLOY_LOG }),
    BOOTSTRAP,
  );
});

test('the canonical host is the target once workers_dev is disabled', () => {
  for (const workers_dev of [false, undefined]) {
    assert.equal(
      smokeTarget({ wrangler: { name: WORKER, workers_dev }, deployLog: DEPLOY_LOG }),
      documentationOrigin,
      String(workers_dev),
    );
  }
});

test('unrelated links in the Wrangler output are not mistaken for the Worker', () => {
  assert.equal(bootstrapOriginFrom(DEPLOY_LOG, WORKER), BOOTSTRAP);
});

test('a repeated mention of the same origin resolves once', () => {
  assert.equal(bootstrapOriginFrom(`${DEPLOY_LOG}\nalso ${BOOTSTRAP}/ecn-sdk/`, WORKER), BOOTSTRAP);
});

test('a name that merely prefixes the deployed label is not accepted', () => {
  // The label is `ecn`; a Worker named `ec` must not match it.
  assert.throws(() => bootstrapOriginFrom(DEPLOY_LOG, 'ec'), /found 0/);
});

test('an ambiguous or missing origin fails closed', () => {
  const heading = `Deployed ${WORKER} triggers (0.10 sec)`;
  assert.throws(() => bootstrapOriginFrom(`${heading}\n  (none)\n`, WORKER), /found 0/);
  assert.throws(
    () => bootstrapOriginFrom(`${heading}\n  ${BOOTSTRAP}\n  ${BOOTSTRAP}:8443\n`, WORKER),
    /found 2/,
  );
});

test('a same-label URL printed before the trigger listing is not selected', () => {
  // A documentation or dashboard link can share the Worker's first label; only
  // what Wrangler lists under the trigger heading is a deployed origin.
  const log = [
    `See ${BOOTSTRAP} for details`,
    `Uploaded ${WORKER} (1.00 sec)`,
    `Deployed ${WORKER} triggers (0.10 sec)`,
    '  docs.picogrid.com/ecn-sdk',
  ].join('\n');
  assert.throws(() => bootstrapOriginFrom(log, WORKER), /found 0/);
});

test('output that announces no triggers fails closed', () => {
  assert.throws(
    () => bootstrapOriginFrom(`Uploaded ${WORKER}\n  ${BOOTSTRAP}\n`, WORKER),
    /did not announce any deployed triggers/,
  );
});

test('a same-label URL printed after the trigger listing is not selected', () => {
  // The listing ends at the first line back at column zero. Anything Wrangler
  // prints afterwards is not a deployed origin, even with a matching label.
  const log = [
    `Deployed ${WORKER} triggers (0.10 sec)`,
    `  ${BOOTSTRAP}`,
    'Current Version ID: 0d1d18df-a3e4-420a-b26b-d67ebe611b26',
    `Docs: ${BOOTSTRAP}:8443/guide`,
  ].join('\n');
  assert.equal(bootstrapOriginFrom(log, WORKER), BOOTSTRAP);
});

test('a trigger heading with no indented entries fails closed', () => {
  const log = [`Deployed ${WORKER} triggers (0.10 sec)`, 'Current Version ID: abc'].join('\n');
  assert.throws(() => bootstrapOriginFrom(log, WORKER), /found 0/);
});
