// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  assertRollbackTarget,
  assertRollbackProductionState,
  assertVersionId,
  currentProductionVersionId,
  loadPreviewRecord,
  markPromoted,
  markRolledBack,
  wranglerPromoteArgs,
  wranglerRollbackArgs,
} from './promote-version.mjs';

const VERSION = '11111111-2222-3333-4444-555555555555';
const PREVIOUS_VERSION = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';
const PREVIOUS = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';

test('assertVersionId accepts UUID versions only', () => {
  assert.equal(assertVersionId(VERSION), VERSION);
  assert.equal(assertVersionId(VERSION.toUpperCase()), VERSION);
  assert.throws(() => assertVersionId('latest'), /UUID/);
  assert.throws(() => assertVersionId('11111111; rm -rf /'), /UUID/);
  assert.throws(() => assertVersionId(''), /UUID/);
});

test('wranglerPromoteArgs accepts only an exclusive 100% split', () => {
  assert.deepEqual(
    wranglerPromoteArgs({ versionId: VERSION }),
    ['versions', 'deploy', `${VERSION}@100`, '--yes'],
  );
  assert.deepEqual(
    wranglerPromoteArgs({
      versionId: VERSION,
      percentage: 100,
      message: 'cutover',
      yes: false,
    }),
    ['versions', 'deploy', `${VERSION}@100`, '--message', 'cutover'],
  );
  assert.throws(
    () => wranglerPromoteArgs({ versionId: VERSION, percentage: 50 }),
    /percentage/,
  );
  assert.throws(
    () => wranglerPromoteArgs({ versionId: VERSION, percentage: 0 }),
    /percentage/,
  );
});

test('currentProductionVersionId reads the latest exclusive deployment', () => {
  const deployments = JSON.stringify([
    {
      created_on: '2026-08-08T00:00:00.000000Z',
      versions: [{ version_id: VERSION, percentage: 100 }],
    },
    {
      created_on: '2026-08-09T00:00:00.000000Z',
      versions: [{ version_id: PREVIOUS_VERSION, percentage: 100 }],
    },
  ]);

  assert.equal(currentProductionVersionId(deployments), PREVIOUS_VERSION);
  assert.equal(currentProductionVersionId('[]'), null);
});

test('currentProductionVersionId rejects an unsafe current traffic split', () => {
  assert.throws(
    () => currentProductionVersionId(JSON.stringify([{
      created_on: '2026-08-09T00:00:00.000000Z',
      versions: [
        { version_id: VERSION, percentage: 20 },
        { version_id: PREVIOUS_VERSION, percentage: 80 },
      ],
    }])),
    /exactly one version at 100%/,
  );
  assert.throws(
    () => currentProductionVersionId('{"versions":[]}'),
    /JSON array/,
  );
});

test('loadPreviewRecord and markPromoted preserve promote identity', () => {
  const record = loadPreviewRecord(JSON.stringify({
    worker_name: 'ecn-sdk',
    version_id: VERSION,
    preview_url: 'https://example.invalid/preview',
    promoted: false,
  }));
  assert.equal(record.version_id, VERSION);

  const promoted = markPromoted(record, {
    promotedAt: '2026-08-09T00:00:00.000Z',
    message: 'promote preview after smoke',
  });
  assert.equal(promoted.promoted, true);
  assert.equal(promoted.promoted_at, '2026-08-09T00:00:00.000Z');
  assert.equal(promoted.promote_message, 'promote preview after smoke');
  assert.equal(promoted.version_id, VERSION);

  assert.throws(
    () => loadPreviewRecord(JSON.stringify({ version_id: 'nope' })),
    /UUID/,
  );
});

test('rollback refuses a no-op restore to the departing version', () => {
  assert.deepEqual(
    assertRollbackTarget({ previousVersionId: PREVIOUS, departingVersionId: VERSION }),
    { previous: PREVIOUS, departing: VERSION },
  );
  assert.throws(
    () => assertRollbackTarget({
      previousVersionId: VERSION,
      departingVersionId: VERSION,
    }),
    /must differ/,
  );
  assert.deepEqual(
    assertRollbackTarget({ previousVersionId: PREVIOUS }),
    { previous: PREVIOUS, departing: null },
  );
});

test('rollback production state requires an active version', () => {
  assert.throws(
    () => assertRollbackProductionState({
      previousVersionId: PREVIOUS,
      departingVersionId: VERSION,
      activeVersionId: null,
    }),
    /cannot roll back: no active production version exists/,
  );
});

test('rollback production state rejects an already-active previous version', () => {
  assert.throws(
    () => assertRollbackProductionState({
      previousVersionId: PREVIOUS,
      departingVersionId: VERSION,
      activeVersionId: PREVIOUS,
    }),
    new RegExp(
      `cannot roll back: previous_version_id ${PREVIOUS} is already the active production version`,
    ),
  );
});

test('rollback production state rejects a stale departing version', () => {
  assert.throws(
    () => assertRollbackProductionState({
      previousVersionId: PREVIOUS,
      departingVersionId: VERSION,
      activeVersionId: PREVIOUS_VERSION,
    }),
    new RegExp(
      `cannot roll back: departing_version_id ${VERSION} does not match the active production version ${PREVIOUS_VERSION}`,
    ),
  );
});

test('rollback production state accepts current departing identity', () => {
  assert.deepEqual(
    assertRollbackProductionState({
      previousVersionId: PREVIOUS,
      departingVersionId: VERSION,
      activeVersionId: VERSION,
    }),
    { previous: PREVIOUS, departing: VERSION, active: VERSION },
  );
});

test('wranglerRollbackArgs points 100% at the previous version', () => {
  assert.deepEqual(
    wranglerRollbackArgs({
      previousVersionId: PREVIOUS,
      departingVersionId: VERSION,
    }),
    [
      'versions',
      'deploy',
      `${PREVIOUS}@100`,
      '--yes',
      '--message',
      `rollback ${VERSION} → ${PREVIOUS}`,
    ],
  );
  assert.deepEqual(
    wranglerRollbackArgs({
      previousVersionId: PREVIOUS,
      departingVersionId: VERSION,
      message: '',
    }),
    [
      'versions',
      'deploy',
      `${PREVIOUS}@100`,
      '--yes',
      '--message',
      `rollback ${VERSION} → ${PREVIOUS}`,
    ],
  );
  assert.deepEqual(
    wranglerRollbackArgs({
      previousVersionId: PREVIOUS,
      message: '   ',
      yes: false,
    }),
    [
      'versions',
      'deploy',
      `${PREVIOUS}@100`,
      '--message',
      `rollback to ${PREVIOUS}`,
    ],
  );
  assert.deepEqual(
    wranglerRollbackArgs({
      previousVersionId: PREVIOUS,
      message: 'drill restore',
      yes: false,
    }),
    ['versions', 'deploy', `${PREVIOUS}@100`, '--message', 'drill restore'],
  );
});

test('markRolledBack records restore identity', () => {
  const rolled = markRolledBack(
    { worker_name: 'ecn-sdk', version_id: VERSION },
    {
      previousVersionId: PREVIOUS,
      departingVersionId: VERSION,
      rolledBackAt: '2026-08-09T12:00:00.000Z',
      message: 'drill',
    },
  );
  assert.equal(rolled.version_id, PREVIOUS);
  assert.equal(rolled.promoted, true);
  assert.equal(rolled.rolled_back, true);
  assert.equal(rolled.rollback_from, VERSION);
  assert.equal(rolled.rolled_back_at, '2026-08-09T12:00:00.000Z');
  assert.equal(rolled.rollback_message, 'drill');
});
