// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import assert from 'node:assert/strict';
import { test } from 'node:test';

// Fixture hosts are example.invalid exactly: the release address scan allows
// only the listed synthetic hosts, not subdomains of them.
import {
  buildPreviewRecord,
  githubOutputLines,
  previewAliasFromRef,
  readVersionUpload,
  smokeTargetUrl,
} from './preview-version.mjs';

const uploadLine = JSON.stringify({
  type: 'version-upload',
  version: 1,
  worker_name: 'ecn-sdk',
  worker_tag: 'abc123',
  version_id: '11111111-2222-3333-4444-555555555555',
  preview_url: 'https://example.invalid/preview/version',
  preview_alias_url: 'https://example.invalid/preview/alias',
});

const sessionLine = JSON.stringify({
  type: 'wrangler-session',
  version: 1,
  wrangler_version: '4.120.0',
  command_line_args: ['versions', 'upload'],
});

test('preview alias', async (t) => {
  await t.test('sanitizes a branch ref and appends a digest', () => {
    assert.match(
      previewAliasFromRef('docs/ecn-sdk-a4_Preview'),
      /^docs-ecn-sdk-a4-preview-[a-f0-9]{8}$/,
    );
  });

  await t.test('collapses runs and trims edge separators', () => {
    assert.match(previewAliasFromRef('///feature//x++'), /^feature-x-[a-f0-9]{8}$/);
  });

  await t.test('keeps aliases within the Cloudflare length limit', () => {
    const alias = previewAliasFromRef(`${'a'.repeat(62)}/tail`);
    assert.equal(alias.length, 63);
    assert.match(alias, /^[a-z0-9]+(?:-[a-z0-9]+)*$/);
  });

  await t.test('distinguishes refs that normalize to the same prefix', () => {
    assert.notEqual(previewAliasFromRef('feature/a_b'), previewAliasFromRef('feature/a-b'));
  });

  await t.test('distinguishes refs that differ only beyond the alias limit', () => {
    const sharedPrefix = 'feature/'.concat('a'.repeat(63));
    assert.notEqual(
      previewAliasFromRef(`${sharedPrefix}-first`),
      previewAliasFromRef(`${sharedPrefix}-second`),
    );
  });

  await t.test('is deterministic for the same ref', () => {
    const ref = 'feature/repeatable';
    assert.equal(previewAliasFromRef(ref), previewAliasFromRef(ref));
  });

  await t.test('rejects a ref with no usable characters', () => {
    assert.throws(() => previewAliasFromRef('///'), /usable preview alias/);
  });
});

test('wrangler output parsing', async (t) => {
  await t.test('reads the upload entry alongside session lines', () => {
    const upload = readVersionUpload(`${sessionLine}\n${uploadLine}\n`);
    assert.equal(upload.version_id, '11111111-2222-3333-4444-555555555555');
  });

  await t.test('prefers the last upload when a retry appended a line', () => {
    const retry = JSON.stringify({
      type: 'version-upload',
      version_id: 'second',
      preview_url: 'https://example.invalid/second',
    });
    assert.equal(readVersionUpload(`${uploadLine}\n${retry}`).version_id, 'second');
  });

  await t.test('fails when no upload happened', () => {
    assert.throws(() => readVersionUpload(sessionLine), /no version-upload entry/);
  });

  await t.test('fails on malformed output rather than guessing', () => {
    assert.throws(() => readVersionUpload('{not json'), /line 1 is not JSON/);
  });

  await t.test('rejects an upload with only a preview alias URL', () => {
    const aliasOnly = JSON.stringify({
      type: 'version-upload',
      version_id: 'x',
      preview_alias_url: 'https://example.invalid/alias',
    });
    assert.throws(
      () => readVersionUpload(aliasOnly),
      /missing preview_url; enable preview URLs and the Workers subdomain/,
    );
  });

  for (const field of ['version_id', 'preview_url', 'preview_alias_url']) {
    for (const value of [{}, true, '', '   ']) {
      await t.test(`rejects ${field} when it is ${JSON.stringify(value)}`, () => {
        const upload = {
          ...JSON.parse(uploadLine),
          [field]: value,
        };
        assert.throws(
          () => readVersionUpload(JSON.stringify(upload)),
          new RegExp(`${field} must be a non-empty string; received ${typeof value}`),
        );
      });
    }
  }

  await t.test('trims upload identity fields', () => {
    const upload = readVersionUpload(JSON.stringify({
      ...JSON.parse(uploadLine),
      version_id: '  version-id  ',
      preview_url: '  https://example.invalid/version  ',
      preview_alias_url: '  https://example.invalid/alias  ',
    }));
    assert.equal(upload.version_id, 'version-id');
    assert.equal(upload.preview_url, 'https://example.invalid/version');
    assert.equal(upload.preview_alias_url, 'https://example.invalid/alias');
  });

  await t.test('normalizes an absent preview alias URL to null', () => {
    const upload = JSON.parse(uploadLine);
    delete upload.preview_alias_url;
    assert.equal(readVersionUpload(JSON.stringify(upload)).preview_alias_url, null);
  });
});

test('preview record', async (t) => {
  const record = buildPreviewRecord({
    upload: {
      ...JSON.parse(uploadLine),
      version_id: '  11111111-2222-3333-4444-555555555555  ',
      preview_url: '  https://example.invalid/preview/version  ',
      preview_alias_url: '  https://example.invalid/preview/alias  ',
    },
    ref: 'docs/ecn-sdk-a4-preview',
    sha: '0'.repeat(40),
    alias: previewAliasFromRef('docs/ecn-sdk-a4-preview'),
    uploadedAt: '2026-08-08T00:00:00.000Z',
  });

  await t.test('records the identity a promotion would need', () => {
    assert.deepEqual(record, {
      worker_name: 'ecn-sdk',
      version_id: '11111111-2222-3333-4444-555555555555',
      preview_url: 'https://example.invalid/preview/version',
      preview_alias_url: 'https://example.invalid/preview/alias',
      preview_alias: previewAliasFromRef('docs/ecn-sdk-a4-preview'),
      git_ref: 'docs/ecn-sdk-a4-preview',
      git_sha: '0'.repeat(40),
      uploaded_at: '2026-08-08T00:00:00.000Z',
      promoted: false,
    });
  });

  await t.test('smoke targets the exact version, not the alias', () => {
    assert.equal(smokeTargetUrl(record), record.preview_url);
  });

  await t.test('never falls back to the alias URL for smoke checks', () => {
    assert.equal(
      smokeTargetUrl({ preview_url: null, preview_alias_url: 'https://example.invalid/alias' }),
      null,
    );
  });

  await t.test('hands the downstream smoke job the version URL', () => {
    assert.equal(
      githubOutputLines(record),
      'version_id=11111111-2222-3333-4444-555555555555\n' +
        'preview_url=https://example.invalid/preview/version\n',
    );
  });

  await t.test('rejects output values that can corrupt key-value lines', () => {
    assert.throws(
      () => githubOutputLines({ ...record, version_id: 'version\ninjection' }),
      /version_id.*key=value/,
    );
    assert.throws(
      () => githubOutputLines({ ...record, preview_url: 'https://example.invalid/?x=y' }),
      /preview_url.*key=value/,
    );
  });
});

test('a preview record must identify its source revision', () => {
  const upload = JSON.parse(uploadLine);
  assert.throws(
    () => buildPreviewRecord({ upload, sha: '0'.repeat(40), uploadedAt: '2026-08-08T00:00:00.000Z' }),
    /requires --ref/,
  );
  assert.throws(
    () => buildPreviewRecord({ upload, ref: 'docs/x', uploadedAt: '2026-08-08T00:00:00.000Z' }),
    /requires --sha/,
  );
});

test('a supplied alias inconsistent with the ref is rejected', () => {
  const upload = JSON.parse(uploadLine);
  assert.throws(
    () => buildPreviewRecord({
      upload,
      ref: 'docs/ecn-sdk-a4-preview',
      sha: '0'.repeat(40),
      alias: 'docs-ecn-sdk-a4-preview',
      uploadedAt: '2026-08-08T00:00:00.000Z',
    }),
    /does not match the alias derived from/,
  );
});

test('a preview record requires a worker name', () => {
  const upload = { ...JSON.parse(uploadLine), worker_name: null };
  assert.throws(
    () => buildPreviewRecord({
      upload,
      ref: 'docs/ecn-sdk-a4-preview',
      sha: '0'.repeat(40),
      uploadedAt: '2026-08-08T00:00:00.000Z',
    }),
    /requires a worker name/,
  );
});

test('a preview record requires a usable upload timestamp', () => {
  const upload = JSON.parse(uploadLine);
  const base = { upload, ref: 'docs/x', sha: '0'.repeat(40) };
  for (const uploadedAt of [undefined, '', '   ', 'not-a-timestamp']) {
    assert.throws(
      () => buildPreviewRecord({ ...base, uploadedAt }),
      /uploaded-at/,
      String(uploadedAt),
    );
  }
});

test('a preview record normalizes the upload timestamp to ISO-8601', () => {
  const record = buildPreviewRecord({
    upload: JSON.parse(uploadLine),
    ref: 'docs/x',
    sha: '0'.repeat(40),
    uploadedAt: '  2026-08-08T00:00:00.000Z  ',
  });
  assert.equal(record.uploaded_at, '2026-08-08T00:00:00.000Z');
});
