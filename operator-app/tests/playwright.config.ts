// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import { defineConfig } from '@playwright/test';
import { fileURLToPath } from 'node:url';

const operatorRoot = fileURLToPath(new URL('..', import.meta.url));
const python = process.env.OPERATOR_TEST_PYTHON ?? 'python';
const browserExecutable = process.env.OPERATOR_TEST_BROWSER_PATH;
const runtimeRoot = process.env.OPERATOR_TEST_RUNTIME_DIR ?? operatorRoot;
const operatorCommand = process.env.OPERATOR_TEST_COMMAND ?? `${python} -m operator_app`;
const runtimePath = process.env.OPERATOR_TEST_RUNTIME_PATH ?? process.env.PATH ?? '';
const commandsFile =
  process.env.OPERATOR_TEST_COMMANDS_FILE ??
  fileURLToPath(new URL('../config/commands.example.json', import.meta.url));

export default defineConfig({
  testDir: '.',
  testMatch: 'operator.spec.ts',
  outputDir: '../test-results',
  fullyParallel: false,
  workers: 1,
  // Shared CI runners starve this single-worker suite in several distinct
  // ways (event-loop stalls, render stalls, keep-alive reaps); none reproduce
  // locally. A retried pass is still reported as 'flaky', so it stays visible.
  retries: process.env.CI ? 1 : 0,
  reporter: [['list'], ['html', { outputFolder: '../playwright-report', open: 'never' }]],
  use: {
    baseURL: 'http://127.0.0.1:8080',
    browserName: 'chromium',
    launchOptions: browserExecutable ? { executablePath: browserExecutable } : undefined,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: operatorCommand,
    cwd: runtimeRoot,
    url: 'http://127.0.0.1:8080/healthz',
    reuseExistingServer: false,
    timeout: 30_000,
    env: {
      PATH: runtimePath,
      OPERATOR_MODE: 'mock',
      OPERATOR_ECN_CLIENT_INTEGRATION: 'operator-console',
      OPERATOR_ECN_INTEGRATION_ALLOWLIST: 'mock-sensor,mock-target',
      OPERATOR_ECN_CATEGORY_ALLOWLIST:
        'TRACK,DETECTION,DEVICE,SYSTEM,SENSOR,ALERT,GEOMETRIC',
      OPERATOR_ECN_WIRE_FORMAT: 'json',
      OPERATOR_TASKING_ENABLED: 'true',
      OPERATOR_COMMANDS_FILE: commandsFile,
      OPERATOR_TASK_ENTITY_ALLOWLIST: '00000000-0000-4000-8000-000000000201',
      OPERATOR_ALLOWED_ORIGINS: 'http://127.0.0.1:8080',
      OPERATOR_BASEMAP_URL_TEMPLATE:
        'https://tiles.example.invalid/{z}/{x}/{y}.png',
      OPERATOR_BASEMAP_ATTRIBUTION: 'Synthetic test map data',
      OPERATOR_SYNTHETIC_PERIOD_SECONDS: '0.2',
      // Freshness budget must absorb multi-second event-loop starvation on
      // loaded CI runners, or /api/tasks/prepare intermittently returns 409
      // "target entity observation is stale" and task-flow tests hang.
      OPERATOR_STALE_AFTER_SECONDS: '3',
    },
  },
});
