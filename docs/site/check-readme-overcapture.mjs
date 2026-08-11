// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/** CLI: confirm a non-/ecn-sdk docs path is not served by the ECN Worker. */
import {
  DEFAULT_README_PROBE_PATH,
  runReadmeOvercaptureProbe,
} from './readme-overcapture-probe.mjs';
import { documentationOrigin } from './site-config.mjs';

function readFlag(name) {
  const prefix = `--${name}=`;
  const hit = process.argv.slice(2).find((argument) => argument.startsWith(prefix));
  return hit ? hit.slice(prefix.length) : undefined;
}

const origin =
  readFlag('origin')
  ?? process.env.DOCS_PROBE_ORIGIN
  ?? process.env.PREVIEW_BASE_URL
  ?? documentationOrigin;

const probePath =
  readFlag('path')
  ?? process.env.README_PROBE_PATH
  ?? DEFAULT_README_PROBE_PATH;

let result;
try {
  result = await runReadmeOvercaptureProbe({ origin, probePath });
} catch (error) {
  process.stderr.write(`readme over-capture probe could not run: ${error.message}\n`);
  process.exit(1);
}

if (!result.ok) {
  for (const failure of result.failures) {
    process.stderr.write(`${failure}\n`);
  }
  process.stderr.write(`readme over-capture probe failed for ${result.url}\n`);
  process.exitCode = 1;
} else {
  process.stdout.write(
    `readme over-capture probe passed: ${result.url} is not ECN Worker-owned\n`,
  );
}
