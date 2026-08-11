// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * Delivery journeys against a Worker origin: local `wrangler dev` by default, or
 * a remote origin when DOCS_DELIVERY_BASE_URL / PREVIEW_BASE_URL is set.
 *
 * Deliberately separate from site/playwright.config.ts so the full offline
 * content suite is never pointed at Cloudflare.
 */
import { defineConfig } from "@playwright/test";
import { tmpdir } from "node:os";
import { resolve } from "node:path";

import { documentationBase, documentationWorkspaceRoot } from "./site-config.mjs";

const browserPath = process.env.DOCS_TEST_BROWSER_PATH;
const outputDir =
  process.env.DOCS_DELIVERY_OUTPUT_DIR ??
  resolve(tmpdir(), `picogrid-ecn-sdk-docs-delivery-${process.pid}`);

const remoteBase =
  process.env.DOCS_DELIVERY_BASE_URL || process.env.PREVIEW_BASE_URL || "";
const remoteOrigin = remoteBase ? new URL(remoteBase).origin : "";
const port = Number(process.env.DELIVERY_PORT ?? 8789);
const localOrigin = `http://127.0.0.1:${port}`;
const origin = remoteOrigin || localOrigin;

export default defineConfig({
  testDir: "./tests",
  testMatch: "delivery.spec.ts",
  outputDir,
  timeout: 45_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  retries: 0,
  reporter: "line",
  use: {
    baseURL: origin,
    browserName: "chromium",
    colorScheme: "light",
    trace: "retain-on-failure",
    ...(browserPath ? { launchOptions: { executablePath: browserPath } } : {}),
  },
  ...(remoteOrigin
    ? {}
    : {
        webServer: {
          command: `npx wrangler dev --port ${port} --ip 127.0.0.1`,
          cwd: documentationWorkspaceRoot,
          url: `${localOrigin}${documentationBase}`,
          reuseExistingServer: false,
          timeout: 120_000,
        },
      }),
});
