// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/** CLI wrapper: smoke-check a candidate docs Worker preview over HTTP. */
import { runPreviewSmoke } from './preview-smoke.mjs';

const baseUrl =
  process.argv.slice(2).find((argument) => argument.startsWith('--base-url='))?.slice('--base-url='.length) ??
  process.env.PREVIEW_BASE_URL;

let result;
try {
  result = await runPreviewSmoke({ baseUrl });
} catch (error) {
  process.stderr.write(`preview smoke could not run: ${error.message}\n`);
  if (error.cause) {
    process.stderr.write(`cause: ${error.cause.message ?? error.cause}\n`);
  }
  process.exit(1);
}

const { failures, checked } = result;

if (failures.length > 0) {
  for (const failure of failures) {
    process.stderr.write(`${failure}\n`);
  }
  process.stderr.write(`preview smoke failed: ${failures.length} problem(s)\n`);
  process.exitCode = 1;
} else {
  process.stdout.write(`preview smoke passed: ${checked.length} requests against ${baseUrl}\n`);
}
