// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import { stat } from 'node:fs/promises';

import { assetsOutputRoot } from './site-config.mjs';

const message =
  'Missing site-dist; build the documentation or download the verified site-dist artifact before running this command.';

let builtSite;
try {
  builtSite = await stat(assetsOutputRoot);
} catch (error) {
  if (error.code !== 'ENOENT') {
    throw error;
  }
}

if (!builtSite?.isDirectory()) {
  process.stderr.write(`${message}\n`);
  process.exitCode = 1;
}
