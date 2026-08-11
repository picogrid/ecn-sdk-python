// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * Preview identity for the docs Worker: derive an upload alias from a git ref
 * and turn Wrangler's ND-JSON output into a recorded version.
 *
 * `wrangler versions upload` has no --json flag, so the identity is read from
 * the ND-JSON file Wrangler writes when WRANGLER_OUTPUT_FILE_PATH is set.
 */
import { appendFile, readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';

/** Workers preview aliases accept lowercase letters, digits, and hyphens. */
const ALIAS_MAX_LENGTH = 63;
const ALIAS_DIGEST_LENGTH = 8;
const ALIAS_PREFIX_MAX_LENGTH = ALIAS_MAX_LENGTH - ALIAS_DIGEST_LENGTH - 1;

function trimmedUploadField(upload, field, { optional = false } = {}) {
  if (optional && !Object.hasOwn(upload, field)) return null;

  const value = upload[field];
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error(
      `version-upload field ${field} must be a non-empty string; received ${typeof value}`,
    );
  }
  return value.trim();
}

export function previewAliasFromRef(ref) {
  const originalRef = String(ref ?? '');
  const normalized = originalRef
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');

  if (!normalized) {
    throw new Error(`ref does not yield a usable preview alias: ${ref}`);
  }

  const prefix = normalized.slice(0, ALIAS_PREFIX_MAX_LENGTH).replace(/-$/, '');
  const digest = createHash('sha256')
    .update(originalRef)
    .digest('hex')
    .slice(0, ALIAS_DIGEST_LENGTH);
  return `${prefix}-${digest}`;
}

/**
 * Last `version-upload` entry wins: a retried upload in the same job appends
 * another line rather than replacing the file.
 */
export function readVersionUpload(ndjson) {
  const entries = String(ndjson)
    .split('\n')
    .filter((line) => line.trim())
    .map((line, index) => {
      try {
        return JSON.parse(line);
      } catch (error) {
        throw new Error(`wrangler output line ${index + 1} is not JSON: ${error.message}`);
      }
    });

  const uploads = entries.filter((entry) => entry?.type === 'version-upload');
  const upload = uploads.at(-1);
  if (!upload) {
    throw new Error('wrangler output contains no version-upload entry');
  }
  let previewUrl;
  try {
    previewUrl = trimmedUploadField(upload, 'preview_url');
  } catch (error) {
    if (!Object.hasOwn(upload, 'preview_url')) {
      throw new Error(
        'version-upload entry is missing preview_url; enable preview URLs and the Workers ' +
          `subdomain; ${error.message}`,
      );
    }
    throw error;
  }

  return {
    ...upload,
    version_id: trimmedUploadField(upload, 'version_id'),
    preview_url: previewUrl,
    preview_alias_url: trimmedUploadField(upload, 'preview_alias_url', { optional: true }),
  };
}

export function buildPreviewRecord({ upload, ref, sha, alias, uploadedAt }) {
  // A preview record that cannot name its source revision cannot be promoted
  // or audited later, so identity is required rather than defaulted to null.
  const gitRef = typeof ref === 'string' ? ref.trim() : '';
  const gitSha = typeof sha === 'string' ? sha.trim() : '';
  if (!gitRef) throw new Error('preview record requires --ref=<git ref>');
  if (!gitSha) throw new Error('preview record requires --sha=<commit sha>');

  // An explicit --uploaded-at= bypasses the caller's default, so an unusable
  // timestamp would otherwise reach the audit artifact. Normalize to ISO-8601.
  const rawUploadedAt = typeof uploadedAt === 'string' ? uploadedAt.trim() : '';
  if (!rawUploadedAt) {
    throw new Error('preview record requires --uploaded-at=<ISO-8601 timestamp>');
  }
  const uploadedAtMs = Date.parse(rawUploadedAt);
  if (Number.isNaN(uploadedAtMs)) {
    throw new Error(`preview record uploaded-at is not a valid timestamp: ${rawUploadedAt}`);
  }
  const uploadedAtIso = new Date(uploadedAtMs).toISOString();

  const derivedAlias = previewAliasFromRef(gitRef);
  const suppliedAlias = typeof alias === 'string' ? alias.trim() : '';
  if (suppliedAlias && suppliedAlias !== derivedAlias) {
    throw new Error(
      `preview alias ${suppliedAlias} does not match the alias derived from ${gitRef} (${derivedAlias})`,
    );
  }

  // Promotion and audit both address the version by worker name, so a record
  // without one cannot be acted on later even though Wrangler may omit it.
  const workerName = typeof upload.worker_name === 'string' ? upload.worker_name.trim() : '';
  if (!workerName) {
    throw new Error('preview record requires a worker name in the wrangler upload output');
  }

  const versionId = trimmedUploadField(upload, 'version_id');
  const previewUrl = trimmedUploadField(upload, 'preview_url');
  const previewAliasUrl = trimmedUploadField(upload, 'preview_alias_url', { optional: true });

  return {
    worker_name: workerName,
    version_id: versionId,
    preview_url: previewUrl,
    preview_alias_url: previewAliasUrl,
    preview_alias: derivedAlias,
    git_ref: gitRef,
    git_sha: gitSha,
    uploaded_at: uploadedAtIso,
    promoted: false,
  };
}

/** URL smoke tests must target the exact version, never its mutable alias. */
export function smokeTargetUrl(record) {
  return record.preview_url;
}

/** `key=value` lines for GITHUB_OUTPUT. */
export function githubOutputLines(record) {
  const outputs = {
    version_id: record.version_id,
    preview_url: smokeTargetUrl(record),
  };
  for (const [field, value] of Object.entries(outputs)) {
    if (typeof value !== 'string' || !value || /[\r\n=]/.test(value)) {
      throw new Error(`${field} cannot be represented safely as a GITHUB_OUTPUT key=value line`);
    }
  }
  return Object.entries(outputs).map(([key, value]) => `${key}=${value}\n`).join('');
}

function parseArguments(argv) {
  const options = new Map();
  for (const argument of argv) {
    const match = /^--([a-z-]+)=(.*)$/.exec(argument);
    if (!match) {
      throw new Error(`unrecognized argument: ${argument}`);
    }
    options.set(match[1], match[2]);
  }
  return options;
}

async function main(argv) {
  const [command, ...rest] = argv;
  const options = parseArguments(rest);

  if (command === 'alias') {
    process.stdout.write(`${previewAliasFromRef(options.get('ref'))}\n`);
    return;
  }

  if (command === 'record') {
    const wranglerOutput = options.get('wrangler-output');
    const destination = options.get('output');
    if (!wranglerOutput || !destination) {
      throw new Error('record requires --wrangler-output=<file> and --output=<file>');
    }
    const record = buildPreviewRecord({
      upload: readVersionUpload(await readFile(wranglerOutput, 'utf8')),
      ref: options.get('ref'),
      sha: options.get('sha'),
      alias: options.get('alias'),
      uploadedAt: options.get('uploaded-at') ?? new Date().toISOString(),
    });
    await writeFile(destination, `${JSON.stringify(record, null, 2)}\n`);
    if (process.env.GITHUB_OUTPUT) {
      await appendFile(process.env.GITHUB_OUTPUT, githubOutputLines(record));
    }
    process.stdout.write(`${JSON.stringify(record, null, 2)}\n`);
    return;
  }

  throw new Error(`unknown command: ${command ?? '(none)'}; expected alias or record`);
}

if (process.argv[1] && import.meta.url === `file://${process.argv[1]}`) {
  main(process.argv.slice(2)).catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
