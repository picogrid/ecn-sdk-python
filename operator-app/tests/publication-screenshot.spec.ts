// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import { writeFile } from 'node:fs/promises';

import { expect, test, type Page } from '@playwright/test';

const generatedAt = '2026-01-01T12:00:00.000Z';
const recordedAt = '2026-01-01T11:59:58.000Z';

async function normalizeOpaqueRgbPng(page: Page, png: Buffer): Promise<Buffer> {
  const encoded = await page.evaluate(async (sourcePng) => {
    const sourceBinary = atob(sourcePng);
    const sourceBytes = new Uint8Array(sourceBinary.length);
    for (let index = 0; index < sourceBinary.length; index += 1) {
      sourceBytes[index] = sourceBinary.charCodeAt(index);
    }

    const bitmap = await createImageBitmap(new Blob([sourceBytes], { type: 'image/png' }));
    try {
      const inspectionCanvas = document.createElement('canvas');
      inspectionCanvas.width = bitmap.width;
      inspectionCanvas.height = bitmap.height;
      const inspectionContext = inspectionCanvas.getContext('2d', { willReadFrequently: true });
      if (inspectionContext === null) throw new Error('could not inspect the screenshot');
      inspectionContext.drawImage(bitmap, 0, 0);
      const pixels = inspectionContext.getImageData(0, 0, bitmap.width, bitmap.height).data;
      for (let index = 3; index < pixels.length; index += 4) {
        if (pixels[index] !== 255) throw new Error('screenshot contains transparent pixels');
      }

      const outputCanvas = document.createElement('canvas');
      outputCanvas.width = bitmap.width;
      outputCanvas.height = bitmap.height;
      const outputContext = outputCanvas.getContext('2d', { alpha: false });
      if (outputContext === null) throw new Error('could not encode the screenshot');
      outputContext.drawImage(bitmap, 0, 0);
      const outputBlob = await new Promise<Blob>((resolve, reject) => {
        outputCanvas.toBlob((blob) => {
          if (blob === null) reject(new Error('could not encode the screenshot'));
          else resolve(blob);
        }, 'image/png');
      });
      const outputBytes = new Uint8Array(await outputBlob.arrayBuffer());
      let outputBinary = '';
      for (let offset = 0; offset < outputBytes.length; offset += 32_768) {
        outputBinary += String.fromCharCode(...outputBytes.subarray(offset, offset + 32_768));
      }
      return btoa(outputBinary);
    } finally {
      bitmap.close();
    }
  }, png.toString('base64'));

  const normalized = Buffer.from(encoded, 'base64');
  if (normalized.length < 26 || normalized.readUInt8(25) !== 2) {
    throw new Error('browser did not encode an RGB screenshot');
  }
  return normalized;
}

async function saveScreenshot(page: Page, path: string): Promise<void> {
  const screenshot = await page.screenshot({
    animations: 'disabled',
    fullPage: false,
    omitBackground: false,
  });
  await writeFile(path, await normalizeOpaqueRgbPng(page, screenshot));
}

const configuration = {
  mode: 'mock',
  read_only: true,
  tasking_enabled: false,
  integrations: ['mock-sensor', 'mock-target'],
  categories: ['TRACK', 'DETECTION', 'DEVICE'],
  stale_after_seconds: 30,
  maximum_entities: 256,
  commands: [],
  basemap_url_template: null,
  basemap_attribution: 'Offline graticule · WGS 84',
};

function entity(
  id: string,
  integration: string,
  category: string | null,
  name: string | null,
  type: string | null,
  latitude: number,
  longitude: number,
  affiliation: string,
) {
  const locationOnly = category === null;
  return {
    key: `${integration}/${id}`,
    entity_id: id,
    integration,
    category,
    affiliation,
    status: locationOnly ? 'UNKNOWN' : 'ACTIVE',
    type,
    name,
    fingerprint: null,
    metadata: locationOnly ? {} : { properties: { synthetic: true, source: 'fixture' } },
    location: {
      latitude,
      longitude,
      altitude: category === 'TRACK' ? 250 : null,
      bearing: category === 'TRACK' ? 84 : null,
      accuracy: null,
      source: 'synthetic-publication-fixture',
      recorded_at: recordedAt,
    },
    location_only: locationOnly,
    entity_recorded_at: locationOnly ? null : recordedAt,
    last_observed_at: recordedAt,
    age_seconds: 2,
    freshness: 'fresh',
    entity_age_seconds: locationOnly ? null : 2,
    entity_freshness: locationOnly ? null : 'fresh',
    location_age_seconds: 2,
    location_freshness: 'fresh',
  };
}

const snapshot = {
  generated_at: generatedAt,
  connection: {
    state: 'ready',
    ready: true,
    mqtt_connected: true,
    changed_at: recordedAt,
  },
  entities: [
    entity(
      '00000000-0000-4000-8000-000000000004',
      'mock-sensor',
      null,
      null,
      null,
      34.0,
      -118.302,
      'UNKNOWN',
    ),
    entity(
      '00000000-0000-4000-8000-000000000003',
      'mock-target',
      'DEVICE',
      'Synthetic task target',
      'synthetic-task-target',
      34.025,
      -118.275,
      'FRIEND',
    ),
    entity(
      '00000000-0000-4000-8000-000000000002',
      'mock-sensor',
      'DETECTION',
      'Synthetic detection',
      'synthetic-detection',
      34.075,
      -118.19,
      'SUSPECT',
    ),
    entity(
      '00000000-0000-4000-8000-000000000001',
      'mock-sensor',
      'TRACK',
      'Synthetic moving track',
      'synthetic-track',
      34.05,
      -118.205,
      'FRIEND',
    ),
  ],
  diagnostics: [
    {
      timestamp: recordedAt,
      level: 'info',
      code: 'runtime_started',
      message: 'operator runtime started with bounded MQTT watchers',
    },
    {
      timestamp: recordedAt,
      level: 'info',
      code: 'mqtt_state',
      message: 'MQTT connection state is ready',
    },
  ],
  task_outcomes: [
    {
      task_id: 'synthetic-screenshot-task',
      target_key: 'mock-target/00000000-0000-4000-8000-000000000003',
      command: 'synthetic_echo_ack',
      mode: 'acknowledgment',
      status: 'ACK',
      detail: 'one synthetic acknowledgment observed; completion is not reported',
      completed_at: recordedAt,
    },
  ],
  health: {
    entity_watcher_active: true,
    location_watcher_active: true,
    entity_scope_pairs: 6,
    location_scope_filters: 4,
    entity_dropped_events: 0,
    location_dropped_events: 0,
    entity_decode_errors: 0,
    location_decode_errors: 0,
    browser_clients: 1,
    browser_dropped_updates: 0,
  },
};

test('rejects transparent publication screenshots', async ({ page }) => {
  const transparentPng = await page.evaluate(() => {
    const canvas = document.createElement('canvas');
    canvas.width = 1;
    canvas.height = 1;
    return canvas.toDataURL('image/png').split(',')[1];
  });
  if (transparentPng === undefined) throw new Error('could not create the test screenshot');

  await expect(normalizeOpaqueRgbPng(page, Buffer.from(transparentPng, 'base64'))).rejects.toThrow(
    'transparent pixels',
  );
});

test('generates sanitized deterministic desktop and mobile screenshots', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.addInitScript(() => {
    Date.now = () => Date.parse('2026-01-01T12:00:00.000Z');
  });
  await page.route('**/api/config', async (route) => {
    await route.fulfill({ json: configuration });
  });
  await page.route('**/api/state', async (route) => {
    await route.fulfill({ json: snapshot });
  });
  await page.routeWebSocket('**/ws/state?view_id=*', (socket) => {
    setTimeout(() => socket.send(JSON.stringify(snapshot)), 50);
  });

  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready');
  await expect(page.locator('[data-entity-key]')).toHaveCount(4);
  await page
    .getByTestId('entity-list')
    .getByRole('button', { name: /^Synthetic moving track/ })
    .click();
  await expect(page.getByTestId('selection-panel')).toContainText(
    '00000000-0000-4000-8000-000000000001',
  );

  await saveScreenshot(page, new URL('../docs/operator-mock.png', import.meta.url).pathname);

  await page.getByRole('button', { name: 'Use light theme' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await saveScreenshot(page, new URL('../docs/operator-mock-light.png', import.meta.url).pathname);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByTestId('operator-map')).toBeVisible();
  await saveScreenshot(
    page,
    new URL('../docs/operator-mock-mobile-light.png', import.meta.url).pathname,
  );

  await page.getByRole('button', { name: 'Use dark theme' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await saveScreenshot(
    page,
    new URL('../docs/operator-mock-mobile-dark.png', import.meta.url).pathname,
  );
});
