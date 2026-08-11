// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * Production promotion and rollback identity helpers (A8 / A9).
 *
 * Both paths reuse a `version_id` already uploaded by preview or an earlier
 * deploy. They never rebuild site-dist or run `wrangler deploy` /
 * `versions upload`. Wrangler CLI: `versions deploy <id>@100 --yes`.
 *
 * Rollback points the same exclusive traffic split at a previous version_id
 * and verifies the requested identities still match the active production
 * deployment immediately before moving traffic.
 */
import { readFile } from 'node:fs/promises';

const VERSION_ID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Cloudflare Worker version IDs are UUIDs; reject shell-metacharacter noise. */
export function assertVersionId(value) {
  const id = String(value ?? '').trim();
  if (!VERSION_ID_PATTERN.test(id)) {
    throw new Error(
      `version_id must be a UUID (got ${JSON.stringify(value ?? '')})`,
    );
  }
  return id.toLowerCase();
}

/**
 * Args for `npx wrangler …` (caller prepends wrangler). Traffic promotion is
 * always exclusive: the listed version gets 100%, everything else 0.
 */
export function wranglerPromoteArgs({
  versionId,
  percentage = 100,
  message,
  yes = true,
}) {
  const id = assertVersionId(versionId);
  if (percentage !== 100) {
    throw new Error(`percentage must be 100 (got ${percentage})`);
  }
  const args = ['versions', 'deploy', `${id}@${percentage}`];
  if (yes) args.push('--yes');
  if (message) args.push('--message', String(message));
  return args;
}

/**
 * Return the version receiving all production traffic in the latest
 * deployment. An empty deployment history is the first-deploy case.
 */
export function currentProductionVersionId(jsonText) {
  let deployments;
  try {
    deployments = JSON.parse(jsonText);
  } catch (error) {
    throw new Error(`deployment list is not JSON: ${error.message}`);
  }
  if (!Array.isArray(deployments)) {
    throw new Error('deployment list must be a JSON array');
  }
  if (deployments.length === 0) return null;

  const versions = deployments.at(-1)?.versions;
  if (
    !Array.isArray(versions)
    || versions.length !== 1
    || versions[0]?.percentage !== 100
  ) {
    throw new Error(
      'latest deployment must have exactly one version at 100% traffic',
    );
  }
  return assertVersionId(versions[0].version_id);
}

/** Validate operator-supplied rollback identities and reject a same-id pair. */
export function assertRollbackTarget({ previousVersionId, departingVersionId }) {
  const previous = assertVersionId(previousVersionId);
  if (departingVersionId == null || departingVersionId === '') {
    return { previous, departing: null };
  }
  const departing = assertVersionId(departingVersionId);
  if (previous === departing) {
    throw new Error(
      `rollback previous_version_id and departing_version_id must differ (both ${previous})`,
    );
  }
  return { previous, departing };
}

/**
 * Confirm the rollback still describes live production immediately before
 * moving traffic.
 */
export function assertRollbackProductionState({
  previousVersionId,
  departingVersionId,
  activeVersionId,
}) {
  const { previous, departing } = assertRollbackTarget({
    previousVersionId,
    departingVersionId,
  });
  if (activeVersionId == null || activeVersionId === '') {
    throw new Error('cannot roll back: no active production version exists');
  }
  const active = assertVersionId(activeVersionId);
  if (previous === active) {
    throw new Error(
      `cannot roll back: previous_version_id ${previous} is already the active production version`,
    );
  }
  if (departing && departing !== active) {
    throw new Error(
      `cannot roll back: departing_version_id ${departing} does not match the active production version ${active}`,
    );
  }
  return { previous, departing, active };
}

export function wranglerRollbackArgs({
  previousVersionId,
  departingVersionId,
  message,
  yes = true,
}) {
  const { previous, departing } = assertRollbackTarget({
    previousVersionId,
    departingVersionId,
  });
  const defaultMessage = departing
    ? `rollback ${departing} → ${previous}`
    : `rollback to ${previous}`;
  return wranglerPromoteArgs({
    versionId: previous,
    percentage: 100,
    message: message == null || String(message).trim() === ''
      ? defaultMessage
      : message,
    yes,
  });
}

export function loadPreviewRecord(jsonText) {
  let record;
  try {
    record = JSON.parse(jsonText);
  } catch (error) {
    throw new Error(`preview record is not JSON: ${error.message}`);
  }
  if (!record || typeof record !== 'object') {
    throw new Error('preview record must be a JSON object');
  }
  return {
    ...record,
    version_id: assertVersionId(record.version_id),
  };
}

export async function loadPreviewRecordFile(path) {
  return loadPreviewRecord(await readFile(path, 'utf8'));
}

export function markPromoted(record, { promotedAt, message } = {}) {
  const versionId = assertVersionId(record.version_id);
  return {
    ...record,
    version_id: versionId,
    promoted: true,
    promoted_at: promotedAt ?? new Date().toISOString(),
    promote_message: message ?? null,
  };
}

export function markRolledBack(record, {
  previousVersionId,
  departingVersionId,
  rolledBackAt,
  message,
} = {}) {
  const { previous, departing } = assertRollbackTarget({
    previousVersionId: previousVersionId ?? record?.version_id,
    departingVersionId,
  });
  return {
    ...record,
    version_id: previous,
    promoted: true,
    rolled_back: true,
    rolled_back_at: rolledBackAt ?? new Date().toISOString(),
    rollback_from: departing,
    rollback_message: message ?? null,
  };
}

function parseArguments(argv) {
  const options = new Map();
  const positionals = [];
  for (const argument of argv) {
    const match = /^--([a-z-]+)=(.*)$/.exec(argument);
    if (match) options.set(match[1], match[2]);
    else positionals.push(argument);
  }
  return { options, positionals };
}

async function main(argv) {
  const [command, ...rest] = argv;
  const { options } = parseArguments(rest);

  if (command === 'validate') {
    const id = assertVersionId(options.get('version-id'));
    process.stdout.write(`${id}\n`);
    return;
  }

  if (command === 'validate-rollback') {
    const { previous, departing } = assertRollbackTarget({
      previousVersionId: options.get('previous-version-id'),
      departingVersionId: options.get('departing-version-id'),
    });
    process.stdout.write(`previous=${previous}\n`);
    if (departing) process.stdout.write(`departing=${departing}\n`);
    return;
  }

  if (command === 'validate-rollback-production') {
    const path = options.get('deployments');
    if (!path) {
      throw new Error(
        'validate-rollback-production requires --deployments=<file>',
      );
    }
    const activeVersionId = currentProductionVersionId(
      await readFile(path, 'utf8'),
    );
    const { active } = assertRollbackProductionState({
      previousVersionId: options.get('previous-version-id'),
      departingVersionId: options.get('departing-version-id'),
      activeVersionId,
    });
    process.stdout.write(`active=${active}\n`);
    return;
  }

  if (command === 'args') {
    const args = wranglerPromoteArgs({
      versionId: options.get('version-id'),
      percentage: options.has('percentage')
        ? Number(options.get('percentage'))
        : 100,
      message: options.get('message'),
      yes: options.get('yes') !== 'false',
    });
    process.stdout.write(`${args.map((part) => JSON.stringify(part)).join(' ')}\n`);
    return;
  }

  if (command === 'rollback-args') {
    const args = wranglerRollbackArgs({
      previousVersionId: options.get('previous-version-id'),
      departingVersionId: options.get('departing-version-id'),
      message: options.get('message'),
      yes: options.get('yes') !== 'false',
    });
    process.stdout.write(`${args.map((part) => JSON.stringify(part)).join(' ')}\n`);
    return;
  }

  if (command === 'current') {
    const path = options.get('deployments');
    if (!path) throw new Error('current requires --deployments=<file>');
    const versionId = currentProductionVersionId(await readFile(path, 'utf8'));
    process.stdout.write(versionId ? `${versionId}\n` : '');
    return;
  }

  if (command === 'from-record') {
    const path = options.get('record');
    if (!path) throw new Error('from-record requires --record=<file>');
    const record = await loadPreviewRecordFile(path);
    process.stdout.write(`${record.version_id}\n`);
    return;
  }

  throw new Error(
    `unknown command: ${command ?? '(none)'}; expected validate, validate-rollback, `
    + `validate-rollback-production, args, rollback-args, current, or from-record`,
  );
}

if (process.argv[1] && import.meta.url === `file://${process.argv[1]}`) {
  main(process.argv.slice(2)).catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
