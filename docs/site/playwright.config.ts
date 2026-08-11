// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import { defineConfig } from '@playwright/test';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';

import { documentationBase, documentationWorkspaceRoot } from './site-config.mjs';

const browserPath = process.env.DOCS_TEST_BROWSER_PATH;
const outputDir = process.env.DOCS_TEST_OUTPUT_DIR
  ?? resolve(tmpdir(), `picogrid-ecn-sdk-docs-playwright-${process.pid}`);

function configuredWorkers(): number | undefined {
  const rawWorkers = process.env.PLAYWRIGHT_WORKERS;
  if (rawWorkers === undefined) {
    return undefined;
  }

  const workers = Number(rawWorkers);
  if (!Number.isSafeInteger(workers) || workers < 1) {
    throw new Error('PLAYWRIGHT_WORKERS must be a positive integer');
  }
  return workers;
}

export default defineConfig({
  testDir: './tests',
  outputDir,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  workers: configuredWorkers(),
  forbidOnly: true,
  retries: 0,
  reporter: 'line',
  use: {
    baseURL: 'http://127.0.0.1:4321',
    browserName: 'chromium',
    colorScheme: 'light',
    trace: 'retain-on-failure',
    ...(browserPath ? { launchOptions: { executablePath: browserPath } } : {}),
  },
  webServer: {
    command: 'node site/serve-built-site.mjs',
    cwd: documentationWorkspaceRoot,
    url: `http://127.0.0.1:4321${documentationBase}`,
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
