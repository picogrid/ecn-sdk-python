// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import { defineConfig } from '@playwright/test';

const operatorRoot = new URL('..', import.meta.url).pathname;
const browserExecutable = process.env.OPERATOR_TEST_BROWSER_PATH;

export default defineConfig({
  testDir: '.',
  testMatch: 'publication-screenshot.spec.ts',
  outputDir: '../test-results',
  workers: 1,
  retries: 0,
  reporter: 'list',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    browserName: 'chromium',
    launchOptions: browserExecutable ? { executablePath: browserExecutable } : undefined,
    colorScheme: 'dark',
    deviceScaleFactor: 1,
    locale: 'en-US',
    timezoneId: 'UTC',
    viewport: { width: 1440, height: 920 },
  },
  webServer: {
    command: 'npm run dev',
    cwd: operatorRoot,
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
