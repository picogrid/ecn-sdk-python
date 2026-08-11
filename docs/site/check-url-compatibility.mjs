// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * CLI: assert every public URL manifest path succeeds on a candidate base.
 */
import { runUrlCompatibility } from './url-compatibility.mjs';

const baseUrl =
  process.argv.slice(2).find((argument) => argument.startsWith('--base-url='))?.slice('--base-url='.length)
  ?? process.env.PREVIEW_BASE_URL
  ?? process.env.COMPATIBILITY_BASE_URL;

let result;
try {
  result = await runUrlCompatibility({ baseUrl });
} catch (error) {
  process.stderr.write(`url compatibility could not run: ${error.message}\n`);
  process.exit(1);
}

const { failures, checked, ok } = result;

if (!ok) {
  for (const failure of failures) {
    process.stderr.write(`${failure}\n`);
  }
  process.stderr.write(
    `url compatibility failed: ${failures.length} problem(s) across ${checked.length} path(s)\n`,
  );
  process.exitCode = 1;
} else {
  process.stdout.write(
    `url compatibility passed: ${checked.length} public path(s) against ${baseUrl}\n`,
  );
}
