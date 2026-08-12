// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Locator, type Page } from '@playwright/test';

const basemapPattern = 'https://tiles.example.invalid/**';
const transparentTile = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
  'base64',
);

test.beforeEach(async ({ page }) => {
  await page.route(basemapPattern, async (route) => {
    await route.fulfill({ status: 200, contentType: 'image/png', body: transparentTile });
  });
});

async function tabTo(page: Page, target: Locator, maximumTabs = 80): Promise<void> {
  for (let index = 0; index < maximumTabs; index += 1) {
    await page.keyboard.press('Tab');
    if (await target.evaluate((element) => document.activeElement === element)) return;
  }
  throw new Error('keyboard focus did not reach the expected operator control');
}

async function expectFreshTaskTarget(page: Page): Promise<void> {
  await expect(page.locator('#selection-fields')).toContainText('Entity stateFRESH', {
    timeout: 10_000,
  });
  await expect(page.getByRole('button', { name: 'Prepare task' })).toBeEnabled();
}

async function openTaskConfirmation(page: Page, message: string): Promise<Locator> {
  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  await page
    .getByTestId('entity-list')
    .getByRole('button', { name: /^Synthetic task target/ })
    .click();
  await page.locator('#arm-tasking').check();
  await page.locator('#task-command').selectOption('echo');
  await page.getByLabel('Message').fill(message);
  await page.getByRole('button', { name: 'Prepare task' }).click();
  const dialog = page.getByTestId('confirm-dialog');
  await expect(dialog).toBeVisible();
  return dialog;
}


test('renders bounded state and requires two explicit task phases', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  await expect(page.locator('[data-entity-key]')).toHaveCount(4, { timeout: 10_000 });
  await expect(page.locator('.leaflet-marker-icon.entity-marker')).toHaveCount(4);
  await expect(page.locator('.entity-marker.category-track')).toHaveCount(1);
  await expect(page.locator('.entity-marker.category-detection')).toHaveCount(1);
  await expect(page.locator('.entity-marker.category-device')).toHaveCount(1);
  await expect(page.locator('.entity-marker.category-location-only')).toHaveCount(1);
  await expect(page.locator('#basemap-status')).toHaveText('Configured basemap');
  await expect(page.locator('#basemap-attribution')).toHaveText('Synthetic test map data');
  await expect(page.getByTestId('diagnostics')).toContainText('runtime_started');
  await expect(page.locator('[data-xss-canary]')).toHaveCount(0);
  await expect(page.getByTestId('entity-list')).toContainText(
    'Synthetic <strong data-xss-canary>markup</strong> detection',
  );

  await page
    .getByTestId('entity-list')
    .getByRole('button', { name: /^Synthetic task target/ })
    .click();
  await expect(page.getByTestId('selection-panel')).toContainText('DEVICE');
  await expect(page.getByTestId('selection-panel')).toContainText('mock-target');
  await expect(page.locator('#selection-metadata')).toContainText('task_capable');

  const arm = page.locator('#arm-tasking');
  const command = page.locator('#task-command');
  await expect(arm).not.toBeChecked();
  await expect(command).toBeDisabled();

  await arm.check();
  await command.selectOption('echo');
  const message = page.getByLabel('Message');
  await message.fill('synthetic operator request');
  await message.focus();
  await page.waitForTimeout(500);
  await expect(message).toBeFocused();
  await expect(message).toHaveValue('synthetic operator request');
  await expectFreshTaskTarget(page);
  const [discardedResponse] = await Promise.all([
    page.waitForResponse((response) => response.url().endsWith('/api/tasks/prepare')),
    page.getByRole('button', { name: 'Prepare task' }).click(),
  ]);
  expect(discardedResponse.status()).toBe(200);
  const discardedToken = (await discardedResponse.json()).preparation_token;
  const discardedView = await discardedResponse.request().headerValue('x-operator-view');
  const discardedViewGeneration = await discardedResponse
    .request()
    .headerValue('x-operator-view-generation');
  if (!discardedView || !discardedViewGeneration) {
    throw new Error('prepare request omitted its operator view identity');
  }

  const dialog = page.getByTestId('confirm-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('Synthetic task target');
  await expect(dialog).toContainText('synthetic operator request');
  const confirm = page.getByRole('button', { name: 'Confirm and send once' });
  await expect(confirm).toBeDisabled();
  await page.getByRole('button', { name: 'Cancel' }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByTestId('task-outcome')).toContainText('nothing was published');
  const discardedStatus = await page.evaluate(
    async ({ token, viewId, viewGeneration }) => {
      const response = await fetch('/api/tasks/confirm', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Operator-Intent': 'confirm',
          'X-Operator-View': viewId,
          'X-Operator-View-Generation': viewGeneration,
        },
        body: JSON.stringify({ preparation_token: token, confirmed: true }),
      });
      return response.status;
    },
    {
      token: discardedToken,
      viewId: discardedView,
      viewGeneration: discardedViewGeneration,
    },
  );
  expect(discardedStatus).toBe(409);
  await expect(page.locator('[data-task-status]')).toHaveCount(0);

  await expectFreshTaskTarget(page);
  await page.getByRole('button', { name: 'Prepare task' }).click();
  await expect(dialog).toBeVisible();
  await page.locator('#confirm-check').check();
  await expect(confirm).toBeEnabled();
  await confirm.click();

  await expect(page.getByTestId('task-outcome')).toContainText('SUCCESS', {
    timeout: 10_000,
  });
  await expect(page.locator('[data-task-status="SUCCESS"]')).toHaveCount(1);
  await expect(dialog).not.toBeVisible();

  await command.selectOption('echo_ack');
  await page.getByLabel('Message').fill('synthetic acknowledgment request');
  await expectFreshTaskTarget(page);
  await page.getByRole('button', { name: 'Prepare task' }).click();
  await expect(dialog).toContainText('waits for exactly one acknowledgment');
  await page.locator('#confirm-check').check();
  await confirm.click();
  await expect(page.getByTestId('task-outcome')).toContainText('ACK', {
    timeout: 10_000,
  });
  await expect(page.locator('[data-task-status="ACK"]')).toHaveCount(1);
  await page.waitForTimeout(500);
  await expect(page.locator('[data-task-status="ACK"]')).toHaveCount(1);
  for (const marker of await page.locator('.leaflet-marker-icon.entity-marker').all()) {
    await marker.hover();
  }
});

test('keeps the operator view after a definite preparation rejection', async ({ page }) => {
  await page.route('**/api/tasks/prepare', async (route) => {
    await route.fulfill({
      status: 409,
      json: {
        detail: 'MQTT connection changed; nothing was published and a fresh prepare is required',
      },
    });
  });
  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  await page
    .getByTestId('entity-list')
    .getByRole('button', { name: /^Synthetic task target/ })
    .click();
  await page.locator('#arm-tasking').check();
  await page.locator('#task-command').selectOption('echo');
  await page.getByLabel('Message').fill('definite preparation rejection');
  const prepare = page.getByRole('button', { name: 'Prepare task' });
  await prepare.click();

  await expect(page.getByTestId('task-outcome')).toContainText('MQTT connection changed');
  await expect(page.getByTestId('connection-state')).toContainText('ready');
  await expect(prepare).toBeEnabled();
});

test('keeps a received reconnect response definite', async ({ page }) => {
  await page.route('**/api/tasks/confirm', async (route) => {
    await route.fulfill({
      status: 409,
      json: {
        detail: 'MQTT connection changed; nothing was published and a fresh prepare is required',
        outcome_status: 'RECONNECT',
      },
    });
  });
  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  await page
    .getByTestId('entity-list')
    .getByRole('button', { name: /^Synthetic task target/ })
    .click();
  await page.locator('#arm-tasking').check();
  await page.locator('#task-command').selectOption('echo');
  await page.getByLabel('Message').fill('definite reconnect response');
  await page.getByRole('button', { name: 'Prepare task' }).click();
  const dialog = page.getByTestId('confirm-dialog');
  await expect(dialog).toBeVisible();
  await dialog.locator('#confirm-check').check();
  await dialog.getByRole('button', { name: 'Confirm and send once' }).click();

  await expect(page.getByTestId('task-outcome')).toContainText('RECONNECT');
  await expect(page.getByTestId('task-outcome')).not.toContainText(
    'task delivery outcome is unknown',
  );
  await expect(dialog).not.toBeVisible();
});

test('strands an unclassified confirmation response', async ({ page }) => {
  await page.route('**/api/tasks/confirm', async (route) => {
    await route.fulfill({
      status: 500,
      json: { detail: 'synthetic unclassified server failure' },
    });
  });
  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  await page
    .getByTestId('entity-list')
    .getByRole('button', { name: /^Synthetic task target/ })
    .click();
  await page.locator('#arm-tasking').check();
  await page.locator('#task-command').selectOption('echo');
  await page.getByLabel('Message').fill('unclassified confirmation response');
  await page.getByRole('button', { name: 'Prepare task' }).click();
  const dialog = page.getByTestId('confirm-dialog');
  const recover = dialog.getByRole('button', { name: 'Reconnect the operator view' });
  await expect(dialog).toBeVisible();
  await dialog.locator('#confirm-check').check();
  await dialog.getByRole('button', { name: 'Confirm and send once' }).click();

  await expect(page.getByTestId('task-outcome')).toContainText(
    'synthetic unclassified server failure',
  );
  await expect(page.getByTestId('task-outcome')).toContainText(
    'task delivery outcome is unknown',
  );
  await expect(dialog).toBeVisible();
  await expect(recover).toBeEnabled();
});

for (const status of [408, 499]) {
  test(`strands an unclassified ${status} confirmation response`, async ({ page }) => {
    await page.route('**/api/tasks/confirm', async (route) => {
      await route.fulfill({
        status,
        json: { detail: `synthetic unclassified ${status} response` },
      });
    });
    const dialog = await openTaskConfirmation(page, `unclassified ${status} response`);
    const recover = dialog.getByRole('button', { name: 'Reconnect the operator view' });
    await dialog.locator('#confirm-check').check();
    await dialog.getByRole('button', { name: 'Confirm and send once' }).click();

    await expect(page.getByTestId('task-outcome')).toContainText(
      `synthetic unclassified ${status} response`,
    );
    await expect(page.getByTestId('task-outcome')).toContainText(
      'task delivery outcome is unknown',
    );
    await expect(dialog).toBeVisible();
    await expect(recover).toBeEnabled();
  });
}

test('strands an unrecognized confirmation outcome status', async ({ page }) => {
  await page.route('**/api/tasks/confirm', async (route) => {
    await route.fulfill({
      status: 503,
      json: {
        detail: 'synthetic response with an unrecognized outcome',
        outcome_status: 'NOT_A_TASK_OUTCOME',
      },
    });
  });
  const dialog = await openTaskConfirmation(page, 'unrecognized outcome response');
  const recover = dialog.getByRole('button', { name: 'Reconnect the operator view' });
  await dialog.locator('#confirm-check').check();
  await dialog.getByRole('button', { name: 'Confirm and send once' }).click();

  await expect(page.getByTestId('task-outcome')).toContainText(
    'synthetic response with an unrecognized outcome',
  );
  await expect(page.getByTestId('task-outcome')).toContainText(
    'task delivery outcome is unknown',
  );
  await expect(dialog).toBeVisible();
  await expect(recover).toBeEnabled();
});

for (const status of [400, 403, 409, 413, 422]) {
  test(`keeps explicit ${status} no-publication confirmation response definite`, async ({
    page,
  }) => {
    await page.route('**/api/tasks/confirm', async (route) => {
      await route.fulfill({
        status,
        json: { detail: `synthetic ${status} denial; nothing was published` },
      });
    });
    const dialog = await openTaskConfirmation(page, `definite ${status} denial`);
    await dialog.locator('#confirm-check').check();
    await dialog.getByRole('button', { name: 'Confirm and send once' }).click();

    await expect(page.getByTestId('task-outcome')).toContainText(
      `synthetic ${status} denial`,
    );
    await expect(page.getByTestId('task-outcome')).not.toContainText(
      'task delivery outcome is unknown',
    );
    await expect(dialog).not.toBeVisible();
  });
}

for (const [statusCode, outcomeStatus] of [
  [503, 'RECONNECT'],
  [409, 'OUTCOME_UNKNOWN'],
  [504, 'TIMEOUT'],
  [502, 'FAILED'],
] as const) {
  test(`keeps recognized ${outcomeStatus} confirmation outcome definite`, async ({ page }) => {
    await page.route('**/api/tasks/confirm', async (route) => {
      await route.fulfill({
        status: statusCode,
        json: {
          detail: `synthetic definite ${outcomeStatus} response`,
          outcome_status: outcomeStatus,
        },
      });
    });
    const dialog = await openTaskConfirmation(page, `recognized ${outcomeStatus} response`);
    await dialog.locator('#confirm-check').check();
    await dialog.getByRole('button', { name: 'Confirm and send once' }).click();

    await expect(page.getByTestId('task-outcome')).toContainText(outcomeStatus);
    await expect(dialog).not.toBeVisible();
  });
}

test('keeps a definite reconnect preparation response on the current view', async ({ page }) => {
  const retirementRequests: string[] = [];
  await page.route('**/api/view/retire', async (route) => {
    retirementRequests.push(route.request().url());
    await route.fulfill({ json: { retired: true } });
  });
  await page.route('**/api/tasks/prepare', async (route) => {
    await route.fulfill({
      status: 503,
      json: {
        detail: 'MQTT was not ready; nothing was published',
        outcome_status: 'RECONNECT',
      },
    });
  });
  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  await page
    .getByTestId('entity-list')
    .getByRole('button', { name: /^Synthetic task target/ })
    .click();
  await page.locator('#arm-tasking').check();
  await page.locator('#task-command').selectOption('echo');
  await page.getByLabel('Message').fill('definite preparation reconnect');
  const prepare = page.getByRole('button', { name: 'Prepare task' });
  await prepare.click();

  await expect(page.getByTestId('task-outcome')).toContainText('RECONNECT');
  await expect(page.getByTestId('task-outcome')).toContainText('nothing was published');
  await expect(page.getByTestId('task-outcome')).not.toContainText('result was not proven');
  await expect(page.getByTestId('connection-state')).toContainText('ready');
  await expect(page.getByTestId('confirm-dialog')).not.toBeVisible();
  await expect(prepare).toBeEnabled();
  expect(retirementRequests).toEqual([]);
});

test('fails closed on an unclassified preparation 5xx response', async ({ page }) => {
  await page.route('**/api/tasks/prepare', async (route) => {
    await route.fulfill({
      status: 502,
      json: { detail: 'synthetic unclassified preparation failure' },
    });
  });
  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  await page
    .getByTestId('entity-list')
    .getByRole('button', { name: /^Synthetic task target/ })
    .click();
  await page.locator('#arm-tasking').check();
  await page.locator('#task-command').selectOption('echo');
  await page.getByLabel('Message').fill('unclassified preparation failure');
  await page.getByRole('button', { name: 'Prepare task' }).click();

  await expect(page.getByTestId('task-outcome')).toContainText(
    'preparation result was not proven',
  );
  await expect(page.getByTestId('connection-state')).toContainText('reconnect required');
  await expect(page.getByRole('button', { name: 'Prepare task' })).toBeDisabled();
  await expect(page.getByTestId('confirm-dialog')).not.toBeVisible();
});

test('offers reconnect recovery only after confirmation is stranded', async ({ page }) => {
  const [configurationResponse, stateResponse] = await Promise.all([
    page.request.get('http://127.0.0.1:8080/api/config'),
    page.request.get('http://127.0.0.1:8080/api/state'),
  ]);
  const configuration = await configurationResponse.json();
  const state = await stateResponse.json();
  await page.route('**/api/config', async (route) => {
    await route.fulfill({ json: configuration });
  });
  await page.route('**/api/state', async (route) => {
    await route.fulfill({ json: state });
  });
  let socketAttempts = 0;
  await page.routeWebSocket('**/ws/state?view_id=*', (webSocket) => {
    socketAttempts += 1;
    if (socketAttempts === 1) {
      setTimeout(() => webSocket.send(JSON.stringify(state)), 50);
      return;
    }
    if (socketAttempts === 2) {
      void webSocket.close({
        code: 1011,
        reason: 'synthetic successor failure',
      });
      return;
    }
    void webSocket.close({
      code: 1013,
      reason: 'operator view identity is already in use',
    });
  });
  await page.route('**/api/tasks/prepare', async (route) => {
    await route.fulfill({
      json: {
        preparation_token: 'synthetic-stranded-preparation',
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        target_key: 'mock-target/00000000-0000-4000-8000-000000000201',
        target_label: 'Synthetic task target',
        command: 'echo',
        mode: 'complete',
        payload: { message: 'stranded confirmation' },
        warning: 'Review before sending.',
      },
    });
  });
  let retirementAttempts = 0;
  await page.route('**/api/view/retire', async (route) => {
    retirementAttempts += 1;
    await route.fulfill({ json: { retired: true } });
  });
  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  await page
    .getByTestId('entity-list')
    .getByRole('button', { name: /^Synthetic task target/ })
    .click();
  await page.locator('#arm-tasking').check();
  await page.locator('#task-command').selectOption('echo');
  await page.getByLabel('Message').fill('stranded confirmation');
  await page.getByRole('button', { name: 'Prepare task' }).click();
  const dialog = page.getByTestId('confirm-dialog');
  const recover = dialog.getByRole('button', { name: 'Reconnect the operator view' });
  await expect(dialog).toBeVisible();
  await expect(recover).toBeDisabled();

  await page.route('**/api/tasks/confirm', async (route) => route.abort('failed'));
  await dialog.locator('#confirm-check').check();
  await dialog.getByRole('button', { name: 'Confirm and send once' }).click();

  await expect(page.getByTestId('task-outcome')).toContainText(
    'task delivery outcome is unknown',
  );
  await expect(dialog).toBeVisible();
  await expect(recover).toBeEnabled();

  await recover.click();
  await expect.poll(() => socketAttempts).toBe(2);
  await expect(recover).toBeEnabled();
  expect(retirementAttempts).toBe(1);

  await recover.click();
  await expect.poll(() => socketAttempts).toBe(3);
  await expect(page.getByTestId('connection-state')).toContainText(
    'view identity conflict',
  );
  await expect(recover).toBeDisabled();
  expect(retirementAttempts).toBe(1);
  await recover.dispatchEvent('click');
  await expect.poll(() => socketAttempts).toBe(3);
});

test('canonicalizes and clears a restored retirement marker after acknowledgement', async ({
  page,
}) => {
  const viewId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
  const generation = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
  await page.addInitScript(
    ({ storedViewId, storedGeneration }) => {
      window.sessionStorage.setItem('picogrid-ecn-operator-view-id', storedViewId);
      window.sessionStorage.setItem(
        'picogrid-ecn-operator-view-generation',
        storedGeneration.toUpperCase(),
      );
      window.sessionStorage.setItem(
        'picogrid-ecn-operator-view-retirement',
        storedGeneration.toUpperCase(),
      );
    },
    { storedViewId: viewId, storedGeneration: generation },
  );
  let markRetirementStarted!: () => void;
  let releaseRetirement!: () => void;
  const retirementStarted = new Promise<void>((resolve) => {
    markRetirementStarted = resolve;
  });
  const retirementRelease = new Promise<void>((resolve) => {
    releaseRetirement = resolve;
  });
  await page.route('**/api/view/retire', async (route) => {
    markRetirementStarted();
    await retirementRelease;
    await route.fulfill({ json: { retired: true } });
  });

  await page.goto('/');
  await retirementStarted;
  expect(
    await page.evaluate(() =>
      window.sessionStorage.getItem('picogrid-ecn-operator-view-retirement'),
    ),
  ).toBe(generation);
  releaseRetirement();
  await expect(page.getByTestId('connection-state')).toContainText('ready');
  expect(
    await page.evaluate(() =>
      window.sessionStorage.getItem('picogrid-ecn-operator-view-retirement'),
    ),
  ).toBeNull();
});

test('removes a persisted identity when retirement intent cannot be stored', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  const storedBefore = await page.evaluate(() => ({
    id: window.sessionStorage.getItem('picogrid-ecn-operator-view-id'),
    generation: window.sessionStorage.getItem('picogrid-ecn-operator-view-generation'),
  }));
  expect(storedBefore.id).not.toBeNull();
  expect(storedBefore.generation).not.toBeNull();

  const storedAfter = await page.evaluate(() => {
    const originalSetItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key: string, value: string): void {
      if (key === 'picogrid-ecn-operator-view-retirement') {
        throw new DOMException('synthetic quota exceeded', 'QuotaExceededError');
      }
      originalSetItem.call(this, key, value);
    };
    window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: false }));
    return {
      id: window.sessionStorage.getItem('picogrid-ecn-operator-view-id'),
      generation: window.sessionStorage.getItem(
        'picogrid-ecn-operator-view-generation',
      ),
    };
  });

  expect(storedAfter).toEqual({ id: null, generation: null });
});

test('removes a persisted identity when an accepted generation cannot be stored', async ({
  page,
}) => {
  await page.addInitScript(() => {
    const originalSetItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key: string, value: string): void {
      if (key === 'picogrid-ecn-operator-view-generation') {
        throw new DOMException('synthetic quota exceeded', 'QuotaExceededError');
      }
      originalSetItem.call(this, key, value);
    };
  });
  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  expect(
    await page.evaluate(() => ({
      id: window.sessionStorage.getItem('picogrid-ecn-operator-view-id'),
      generation: window.sessionStorage.getItem(
        'picogrid-ecn-operator-view-generation',
      ),
      retirement: window.sessionStorage.getItem(
        'picogrid-ecn-operator-view-retirement',
      ),
    })),
  ).toEqual({ id: null, generation: null, retirement: null });
  await expect(page.locator('#arm-tasking')).toBeDisabled();
});

test('repairs a malformed stored view identity without disabling tasking', async ({ page }) => {
  await page.addInitScript(() => {
    window.sessionStorage.setItem('picogrid-ecn-operator-view-id', 'malformed-view-id');
  });
  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  const storedViewId = await page.evaluate(() =>
    window.sessionStorage.getItem('picogrid-ecn-operator-view-id'),
  );
  expect(storedViewId).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
  );
  expect(storedViewId).not.toBe('malformed-view-id');

  await page
    .getByTestId('entity-list')
    .getByRole('button', { name: /^Synthetic task target/ })
    .click();
  await expect(page.locator('#arm-tasking')).toBeEnabled();
  await page.locator('#arm-tasking').check();
  await page.locator('#task-command').selectOption('echo');
  await page.getByLabel('Message').fill('repaired view identity');
  await expect(page.getByRole('button', { name: 'Prepare task' })).toBeEnabled();
});

test('supports explicit light and dark themes without changing task safety', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  const toggle = page.locator('#theme-toggle');
  const initialTheme = await page.locator('html').getAttribute('data-theme');
  expect(['light', 'dark']).toContain(initialTheme);

  await toggle.click();
  await expect(page.locator('html')).toHaveAttribute(
    'data-theme',
    initialTheme === 'dark' ? 'light' : 'dark',
  );
  await expect(page.locator('#arm-tasking')).not.toBeChecked();
  await expect(page.locator('#task-command')).toBeDisabled();
});

test('supports one complete operator task confirmation using only the keyboard', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  const target = page
    .getByTestId('entity-list')
    .getByRole('button', { name: /^Synthetic task target/ });
  await tabTo(page, target);
  await expect(target).toBeFocused();
  const focusedRowText = await target.textContent();
  // The row re-renders on every state snapshot; a loaded CI runner can starve
  // the render loop past Playwright's default 5s poll budget.
  await expect
    .poll(() => target.textContent(), { timeout: 10_000 })
    .not.toBe(focusedRowText);
  await expect(target).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.getByTestId('selection-panel')).toContainText('mock-target');

  const arm = page.locator('#arm-tasking');
  await tabTo(page, arm);
  await page.keyboard.press('Space');
  await expect(arm).toBeChecked();

  const command = page.locator('#task-command');
  await tabTo(page, command);
  await page.keyboard.type('Synthetic echo');
  await expect(command).toHaveValue('echo');

  const message = page.getByLabel('Message');
  await tabTo(page, message);
  await page.keyboard.type('keyboard-only synthetic request');

  const prepare = page.getByRole('button', { name: 'Prepare task' });
  await tabTo(page, prepare);
  await page.keyboard.press('Enter');
  const dialog = page.getByRole('dialog', { name: 'Confirm one MQTT task dispatch' });
  await expect(dialog).toBeVisible();

  const reviewed = dialog.locator('#confirm-check');
  await tabTo(page, reviewed);
  await page.keyboard.press('Space');
  const confirm = dialog.getByRole('button', { name: 'Confirm and send once' });
  await tabTo(page, confirm);
  await page.keyboard.press('Enter');
  await expect(page.getByTestId('task-outcome')).toContainText('SUCCESS', {
    timeout: 10_000,
  });
  await expect(dialog).not.toBeVisible();
});

test('reduced-motion preference suppresses operator animation and transitions', async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await page.goto('/');
  const probe = page.locator('body').locator('#motion-probe');
  await page.locator('body').evaluate((body) => {
    const element = document.createElement('div');
    element.id = 'motion-probe';
    element.style.animationDuration = '2s';
    element.style.scrollBehavior = 'smooth';
    element.style.transitionDuration = '2s';
    body.append(element);
  });
  const normal = await probe.evaluate((element) => ({
    animationDuration: getComputedStyle(element).animationDuration,
    scrollBehavior: getComputedStyle(element).scrollBehavior,
    transitionDuration: getComputedStyle(element).transitionDuration,
  }));
  expect(Number.parseFloat(normal.animationDuration)).toBe(2);
  expect(Number.parseFloat(normal.transitionDuration)).toBe(2);
  expect(normal.scrollBehavior).toBe('smooth');

  await page.emulateMedia({ reducedMotion: 'reduce' });
  const reduced = await probe.evaluate((element) => ({
    animationDuration: getComputedStyle(element).animationDuration,
    scrollBehavior: getComputedStyle(element).scrollBehavior,
    transitionDuration: getComputedStyle(element).transitionDuration,
  }));
  expect(Number.parseFloat(reduced.animationDuration)).toBeLessThanOrEqual(0.00001);
  expect(Number.parseFloat(reduced.transitionDuration)).toBeLessThanOrEqual(0.00001);
  expect(reduced.scrollBehavior).toBe('auto');
});

test('loads the exact configured HTTPS basemap under the production CSP', async ({ page }) => {
  let tileRequests = 0;
  const policyResponse = await page.request.get('/');
  const policy = policyResponse.headers()['content-security-policy'];
  expect(policy).toContain("img-src 'self' data: https://tiles.example.invalid");
  expect(policy).not.toContain('*.example.invalid');
  expect(policy).not.toContain('{z}');
  page.on('request', (request) => {
    if (request.url().startsWith('https://tiles.example.invalid/')) tileRequests += 1;
  });
  const cspErrors: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error' && message.text().includes('Content Security Policy')) {
      cspErrors.push(message.text());
    }
  });

  await page.goto('/');
  await expect(page.locator('#basemap-status')).toHaveText('Configured basemap', {
    timeout: 10_000,
  });
  await expect.poll(() => tileRequests).toBeGreaterThan(0);
  expect(cspErrors).toEqual([]);
});

test('falls back to the local graticule when a configured basemap is unavailable', async ({
  page,
}) => {
  await page.unroute(basemapPattern);
  await page.route('https://tiles.example.invalid/**', async (route) => route.abort('failed'));

  await page.goto('/');
  await expect(page.locator('#basemap-status')).toContainText(
    'Basemap unavailable · offline graticule',
    { timeout: 10_000 },
  );
  await expect(page.locator('#basemap-attribution')).toHaveText(
    'Offline graticule · WGS 84',
  );
  await expect(page.locator('.leaflet-marker-icon.entity-marker')).toHaveCount(4, {
    timeout: 10_000,
  });
});

test('exits connecting when a state socket misses its local handshake deadline', async ({
  page,
}) => {
  let socketAttempts = 0;
  await page.clock.install();
  await page.routeWebSocket('**/ws/state?view_id=*', () => {
    socketAttempts += 1;
  });

  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('connecting');
  expect(socketAttempts).toBe(1);

  await page.clock.fastForward(9_999);

  await expect(page.getByTestId('connection-state')).not.toContainText('connecting');
  await expect(page.getByRole('button', { name: 'Reconnect view' })).toBeEnabled();
});

test('shows disconnected and stale state until manual reconnect', async ({ page, context }) => {
  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  await expect(page.locator('[data-entity-key]')).toHaveCount(4, { timeout: 10_000 });

  await page
    .getByTestId('entity-list')
    .getByRole('button', { name: /^Synthetic task target/ })
    .click();
  const arm = page.locator('#arm-tasking');
  const command = page.locator('#task-command');
  await arm.check();
  await command.selectOption('echo');
  await page.getByLabel('Message').fill('discard on disconnect');
  const preparationResponse = page.waitForResponse((response) =>
    response.url().endsWith('/api/tasks/prepare'),
  );
  await page.getByRole('button', { name: 'Prepare task' }).click();
  const preparedResponse = await preparationResponse;
  expect(preparedResponse.status()).toBe(200);
  const preparedToken = (await preparedResponse.json()).preparation_token;
  const preparedView = await preparedResponse.request().headerValue('x-operator-view');
  const preparedViewGeneration = await preparedResponse
    .request()
    .headerValue('x-operator-view-generation');
  if (!preparedView || !preparedViewGeneration) {
    throw new Error('prepare request omitted its operator view identity');
  }
  const dialog = page.getByTestId('confirm-dialog');
  await expect(dialog).toBeVisible();
  await page.locator('#confirm-check').check();

  await context.setOffline(true);
  await expect(page.getByTestId('connection-state')).toContainText('offline');
  await expect(dialog).not.toBeVisible();
  await expect(arm).not.toBeChecked();
  await expect(command).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Prepare task' })).toBeDisabled();
  await expect(page.getByTestId('task-outcome')).toContainText(
    'backend invalidation could not be confirmed',
  );
  await expect(page.locator('#stale-count')).not.toHaveText('0', { timeout: 10_000 });
  await expect(page.getByTestId('connection-state')).toContainText('offline');

  await context.setOffline(false);
  await expect(page.getByTestId('connection-state')).toContainText('reconnect required');
  await expect
    .poll(async () =>
      page.evaluate(async () => {
        const response = await fetch('/api/state');
        const current = await response.json();
        return current.diagnostics.some(
          (item: { code?: string }) => item.code === 'task_preparation_discarded',
        );
      }),
    )
    .toBe(true);
  const disconnectedStatus = await page.evaluate(
    async ({ token, viewId, viewGeneration }) => {
      const response = await fetch('/api/tasks/confirm', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Operator-Intent': 'confirm',
          'X-Operator-View': viewId,
          'X-Operator-View-Generation': viewGeneration,
        },
        body: JSON.stringify({ preparation_token: token, confirmed: true }),
      });
      return response.status;
    },
    {
      token: preparedToken,
      viewId: preparedView,
      viewGeneration: preparedViewGeneration,
    },
  );
  expect(disconnectedStatus).toBe(409);
  await page.waitForTimeout(1_200);
  await expect(page.getByTestId('connection-state')).toContainText('reconnect required');
  await page.getByRole('button', { name: 'Reconnect view' }).click();
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  await expect(arm).not.toBeChecked();
  await expect(command).toBeDisabled();
  await arm.check();
  await command.selectOption('echo');
  await expect(page.getByRole('button', { name: 'Prepare task' })).toBeEnabled();
  const reconnectedPreparation = page.waitForResponse((response) =>
    response.url().endsWith('/api/tasks/prepare'),
  );
  await page.getByRole('button', { name: 'Prepare task' }).click();
  const reconnectedResponse = await reconnectedPreparation;
  expect(reconnectedResponse.status()).toBe(200);
  await expect(dialog).toBeVisible();
  await page.getByRole('button', { name: 'Cancel' }).click();
  await expect(dialog).not.toBeVisible();
});

test('retires the exact prior view before a synthetic persisted-page restoration', async ({
  page,
}) => {
  const socketUrls: string[] = [];
  const restoreOrder: string[] = [];
  let observeRestore = false;
  let retirementHeaders: Record<string, string> | null = null;
  page.on('websocket', (webSocket) => {
    socketUrls.push(webSocket.url());
    if (observeRestore) restoreOrder.push('successor socket');
  });
  await page.route('**/api/view/retire', async (route) => {
    retirementHeaders = route.request().headers();
    restoreOrder.push('prior view retirement');
    await route.fulfill({ json: { retired: true } });
  });

  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  await expect(page.locator('.leaflet-container')).toHaveCount(1);
  await expect.poll(() => socketUrls.length).toBe(1);
  const priorSocket = new URL(socketUrls[0]!);
  expect(
    await page.evaluate(() =>
      window.sessionStorage.getItem('picogrid-ecn-operator-view-retirement'),
    ),
  ).toBeNull();

  await page.evaluate(() => {
    window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true }));
  });
  expect(
    await page.evaluate(() =>
      window.sessionStorage.getItem('picogrid-ecn-operator-view-retirement'),
    ),
  ).toBe(priorSocket.searchParams.get('view_generation'));
  await expect(page.getByTestId('connection-state')).not.toContainText('ready');
  observeRestore = true;
  await page.evaluate(() => {
    window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
  });

  await expect.poll(() => restoreOrder.slice(0, 2)).toEqual([
    'prior view retirement',
    'successor socket',
  ]);
  expect(retirementHeaders).toMatchObject({
    'x-operator-view': priorSocket.searchParams.get('view_id'),
    'x-operator-view-generation': priorSocket.searchParams.get('view_generation'),
  });
  await expect(page.locator('.leaflet-container')).toHaveCount(1);
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  expect(
    await page.evaluate(() =>
      window.sessionStorage.getItem('picogrid-ecn-operator-view-retirement'),
    ),
  ).toBeNull();
});

test('retires the persisted accepted generation before a full-page reload successor', async ({
  page,
}) => {
  const socketUrls: string[] = [];
  const reloadOrder: string[] = [];
  let observeReload = false;
  let retirementHeaders: Record<string, string> | null = null;
  page.on('websocket', (webSocket) => {
    socketUrls.push(webSocket.url());
    if (observeReload) reloadOrder.push('successor socket');
  });
  await page.route('**/api/view/retire', async (route) => {
    retirementHeaders = route.request().headers();
    reloadOrder.push('prior view retirement');
    await route.continue();
  });

  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  await expect.poll(() => socketUrls.length).toBe(1);
  const priorSocket = new URL(socketUrls[0]!);

  observeReload = true;
  await page.reload();

  await expect.poll(() => reloadOrder.slice(0, 2)).toEqual([
    'prior view retirement',
    'successor socket',
  ]);
  expect(retirementHeaders).toMatchObject({
    'x-operator-view': priorSocket.searchParams.get('view_id'),
    'x-operator-view-generation': priorSocket.searchParams.get('view_generation'),
  });
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
});

test('retries from the last accepted generation after a successor socket fails', async ({
  page,
}) => {
  const snapshot = await (await page.request.get('/api/state')).json();
  const socketUrls: string[] = [];
  const retirementGenerations: string[] = [];
  let initialAcceptedGeneration: string | null = null;
  let socketAttempts = 0;
  await page.routeWebSocket('**/ws/state?view_id=*', (webSocket) => {
    socketUrls.push(webSocket.url());
    socketAttempts += 1;
    if (socketAttempts === 2) {
      void webSocket.close({ code: 1011, reason: 'synthetic successor failure' });
      return;
    }
    webSocket.send(JSON.stringify(snapshot));
  });
  await page.route('**/api/view/retire', async (route) => {
    const generation = route.request().headers()['x-operator-view-generation'];
    if (generation) retirementGenerations.push(generation);
    if (generation !== initialAcceptedGeneration) {
      await route.fulfill({
        status: 409,
        json: { detail: 'operator browser view generation is not active' },
      });
      return;
    }
    await route.fulfill({ json: { retired: true } });
  });

  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  await expect.poll(() => socketUrls.length).toBe(1);
  initialAcceptedGeneration = new URL(socketUrls[0]!).searchParams.get('view_generation');

  await page.getByRole('button', { name: 'Reconnect view' }).click();
  await expect.poll(() => socketAttempts).toBe(2);
  await expect(page.getByRole('button', { name: 'Reconnect view' })).toBeEnabled();
  await page.getByRole('button', { name: 'Reconnect view' }).click();

  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  expect(retirementGenerations).toEqual([initialAcceptedGeneration]);
  expect(socketAttempts).toBe(3);
});

test('keeps retirement acknowledgement terminal across later reconnect attempts', async ({
  page,
}) => {
  const snapshot = await (await page.request.get('/api/state')).json();
  let socketAttempts = 0;
  let retirementAttempts = 0;
  await page.routeWebSocket('**/ws/state?view_id=*', (webSocket) => {
    socketAttempts += 1;
    if (socketAttempts === 1) {
      webSocket.send(JSON.stringify(snapshot));
      return;
    }
    if (socketAttempts === 2) {
      void webSocket.close({ code: 1011, reason: 'synthetic successor failure' });
      return;
    }
    void webSocket.close({
      code: 1013,
      reason: 'operator view identity is already in use',
    });
  });
  await page.route('**/api/view/retire', async (route) => {
    retirementAttempts += 1;
    await route.fulfill({ json: { retired: true } });
  });

  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready');
  const reconnect = page.getByRole('button', { name: 'Reconnect view' });
  await reconnect.click();
  await expect.poll(() => socketAttempts).toBe(2);
  await expect(reconnect).toBeEnabled();
  await reconnect.click();
  await expect(page.getByTestId('connection-state')).toContainText(
    'view identity conflict',
  );
  await expect(reconnect).toBeDisabled();
  expect(retirementAttempts).toBe(1);
  expect(socketAttempts).toBe(3);
});

test('keeps restored retirement mandatory after a failed acknowledgement', async ({
  page,
}) => {
  const viewId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
  const generation = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
  await page.addInitScript(
    ({ storedViewId, storedGeneration }) => {
      window.sessionStorage.setItem('picogrid-ecn-operator-view-id', storedViewId);
      window.sessionStorage.setItem(
        'picogrid-ecn-operator-view-generation',
        storedGeneration,
      );
      window.sessionStorage.setItem(
        'picogrid-ecn-operator-view-retirement',
        storedGeneration,
      );
    },
    { storedViewId: viewId, storedGeneration: generation },
  );
  let retirementAttempts = 0;
  const socketUrls: string[] = [];
  page.on('websocket', (webSocket) => socketUrls.push(webSocket.url()));
  await page.route('**/api/view/retire', async (route) => {
    retirementAttempts += 1;
    if (retirementAttempts === 1) {
      await route.fulfill({
        status: 409,
        json: { detail: 'synthetic retirement still pending' },
      });
      return;
    }
    await route.fulfill({ json: { retired: true } });
  });

  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('reconnect');
  expect(retirementAttempts).toBe(1);
  expect(socketUrls).toEqual([]);

  await page.getByRole('button', { name: 'Reconnect view' }).click();
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  expect(retirementAttempts).toBe(2);
  expect(socketUrls).toHaveLength(1);
});

test('blocks retries after a post-retirement duplicate refusal', async ({ page }) => {
  const snapshot = await (await page.request.get('/api/state')).json();
  let socketAttempts = 0;
  await page.routeWebSocket('**/ws/state?view_id=*', (webSocket) => {
    socketAttempts += 1;
    if (socketAttempts === 1) {
      webSocket.send(JSON.stringify(snapshot));
      return;
    }
    void webSocket.close({
      code: 1013,
      reason: 'operator view identity is already in use',
    });
  });
  let retirementAttempts = 0;
  await page.route('**/api/view/retire', async (route) => {
    retirementAttempts += 1;
    await route.fulfill({ json: { retired: true } });
  });

  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready');
  const reconnect = page.getByRole('button', { name: 'Reconnect view' });
  await reconnect.click();
  await expect(page.getByTestId('connection-state')).toContainText(
    'view identity conflict',
  );
  await expect(reconnect).toBeDisabled();
  expect(retirementAttempts).toBe(1);
  expect(socketAttempts).toBe(2);
});

test('rotates a cloned identity before its initial socket opens', async ({
  context,
  page,
}) => {
  const retirementGenerations: string[] = [];
  await context.route('**/api/view/retire', async (route) => {
    const generation = route.request().headers()['x-operator-view-generation'];
    if (generation) retirementGenerations.push(generation);
    await route.fulfill({ json: { retired: true } });
  });

  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  const originalViewId = await page.evaluate(() =>
    window.sessionStorage.getItem('picogrid-ecn-operator-view-id'),
  );
  const popup = page.waitForEvent('popup');
  await page.evaluate(() => {
    void window.open(window.location.href, '_blank');
  });
  const clone = await popup;
  await expect(clone.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  expect(
    await clone.evaluate(() =>
      window.sessionStorage.getItem('picogrid-ecn-operator-view-id'),
    ),
  ).not.toBe(originalViewId);
  expect(retirementGenerations).toEqual([]);
});


test('renders bounded persisted task and decode outcomes distinctly', async ({ page }) => {
  const [configurationResponse, stateResponse] = await Promise.all([
    page.request.get('http://127.0.0.1:8080/api/config'),
    page.request.get('http://127.0.0.1:8080/api/state'),
  ]);
  const configuration = await configurationResponse.json();
  const state = await stateResponse.json();
  const outcomes = [
    { status: 'ACK', mode: 'acknowledgment' },
    { status: 'SUCCESS', mode: 'complete' },
    { status: 'FAILED', mode: 'complete' },
    { status: 'TIMEOUT', mode: 'complete' },
    { status: 'CANCELLED', mode: 'complete' },
    { status: 'RECONNECT', mode: 'complete' },
    { status: 'OUTCOME_UNKNOWN', mode: 'complete' },
  ];
  state.task_outcomes = outcomes.map(({ status, mode }, index) => ({
    task_id: `synthetic-${index}`,
    target_key: state.entities[0].key,
    command: `synthetic_${status.toLowerCase()}`,
    mode,
    status,
    detail:
      status === 'OUTCOME_UNKNOWN'
        ? `task outcome is unknown at response_pending; task ID synthetic-${index}; do not retry automatically`
        : `bounded synthetic ${status.toLowerCase()} outcome`,
    completed_at: state.generated_at,
  }));
  state.health.entity_decode_errors = 2;
  state.health.location_decode_errors = 1;

  await page.route('**/api/config', async (route) => route.fulfill({ json: configuration }));
  await page.route('**/api/state', async (route) => route.fulfill({ json: state }));
  await page.routeWebSocket('**/ws/state?view_id=*', (webSocket) => {
    setTimeout(() => webSocket.send(JSON.stringify(state)), 50);
  });

  await page.goto('/');
  for (const status of [
    'ACK',
    'SUCCESS',
    'FAILED',
    'TIMEOUT',
    'CANCELLED',
    'RECONNECT',
    'OUTCOME_UNKNOWN',
  ]) {
    await expect(page.locator(`[data-task-status="${status}"]`)).toHaveCount(1);
  }
  const uncertainOutcome = page.locator('[data-task-status="OUTCOME_UNKNOWN"]');
  await expect(uncertainOutcome).toBeVisible();
  await expect(uncertainOutcome).toContainText('response_pending');
  await expect(uncertainOutcome).toContainText('task ID synthetic-6');
  await expect(uncertainOutcome).toContainText('do not retry automatically');
  const uncertainBorder = await uncertainOutcome.evaluate(
    (element) => getComputedStyle(element).borderTopColor,
  );
  const reconnectBorder = await page
    .locator('[data-task-status="RECONNECT"]')
    .evaluate((element) => getComputedStyle(element).borderTopColor);
  const successBorder = await page
    .locator('[data-task-status="SUCCESS"]')
    .evaluate((element) => getComputedStyle(element).borderTopColor);
  expect(uncertainBorder).toBe(reconnectBorder);
  expect(uncertainBorder).not.toBe(successBorder);
  await expect(page.getByTestId('diagnostics')).toContainText('2 decode errors');
  await expect(page.getByTestId('diagnostics')).toContainText('1 decode errors');
});

for (const [summary, mqttConnected] of [
  ['ready', true],
  ['reconnecting', false],
  ['retry scheduled', false],
  ['credentials rejected', false],
  ['subscription denied', false],
  ['credentials unavailable', false],
  ['disconnected', false],
  ['subscription resource-limited', false],
  ['terminal', false],
] as const) {
  test(`renders the fixed ${summary} connection state`, async ({ page }) => {
    const [configurationResponse, stateResponse] = await Promise.all([
      page.request.get('/api/config'),
      page.request.get('/api/state'),
    ]);
    const configuration = await configurationResponse.json();
    const state = await stateResponse.json();
    state.connection_summary = summary;
    state.connection.ready = summary === 'ready';
    state.connection.mqtt_connected = mqttConnected;

    await page.route('**/api/config', async (route) => route.fulfill({ json: configuration }));
    await page.route('**/api/state', async (route) => route.fulfill({ json: state }));
    await page.routeWebSocket('**/ws/state?view_id=*', (webSocket) => {
      setTimeout(() => webSocket.send(JSON.stringify(state)), 50);
    });

    await page.goto('/');
    const connectionState = page.getByTestId('connection-state');
    await expect(connectionState).toHaveText(summary);
    await expect(connectionState).toHaveClass(
      summary === 'ready' ? /(^|\s)connected(\s|$)/ : /(^|\s)disconnected(\s|$)/,
    );
  });
}

test('surfaces a resource-limited watcher while MQTT remains connected and disables tasking', async ({
  page,
}) => {
  const [configurationResponse, stateResponse] = await Promise.all([
    page.request.get('/api/config'),
    page.request.get('/api/state'),
  ]);
  const configuration = await configurationResponse.json();
  const state = await stateResponse.json();
  state.connection_summary = 'subscription resource-limited';
  state.connection.ready = true;
  state.connection.mqtt_connected = true;
  state.health.entity_watcher_active = true;
  state.health.location_watcher_active = false;

  await page.route('**/api/config', async (route) => route.fulfill({ json: configuration }));
  await page.route('**/api/state', async (route) => route.fulfill({ json: state }));
  await page.routeWebSocket('**/ws/state?view_id=*', (webSocket) => {
    setTimeout(() => webSocket.send(JSON.stringify(state)), 50);
  });

  await page.goto('/');
  const connectionState = page.getByTestId('connection-state');
  await expect(connectionState).toHaveText('subscription resource-limited');
  await expect(connectionState).toHaveClass(/(^|\s)disconnected(\s|$)/);
  await page
    .getByTestId('entity-list')
    .getByRole('button', { name: /^Synthetic task target/ })
    .click();
  await expect(page.locator('#arm-tasking')).toBeDisabled();
  await expect(page.locator('#task-policy')).toContainText('both bounded MQTT watchers');
});

test('renders every configured category with distinct glyphs and a complete legend', async ({
  page,
}) => {
  const [configurationResponse, stateResponse] = await Promise.all([
    page.request.get('/api/config'),
    page.request.get('/api/state'),
  ]);
  const configuration = await configurationResponse.json();
  const state = await stateResponse.json();
  const template = state.entities.find((entity: { category: string | null }) =>
    entity.category === 'TRACK',
  );
  const additions = [
    ['SYSTEM', '00000000-0000-4000-8000-000000000010', 34.09, -118.28],
    ['SENSOR', '00000000-0000-4000-8000-000000000011', 34.08, -118.27],
    ['ALERT', '00000000-0000-4000-8000-000000000012', 34.07, -118.26],
    ['GEOMETRIC', '00000000-0000-4000-8000-000000000013', 34.06, -118.25],
  ].map(([category, entityId, latitude, longitude]) => ({
    ...template,
    key: `mock-sensor/${entityId}`,
    entity_id: entityId,
    category,
    affiliation: category === 'GEOMETRIC' ? 'NEUTRAL' : 'UNKNOWN',
    type: `synthetic-${String(category).toLowerCase()}`,
    name: `Synthetic ${String(category).toLowerCase()}`,
    location: { ...template.location, latitude, longitude },
  }));
  state.entities = [...state.entities, ...additions];

  await page.route('**/api/config', async (route) => route.fulfill({ json: configuration }));
  await page.route('**/api/state', async (route) => route.fulfill({ json: state }));
  await page.routeWebSocket('**/ws/state?view_id=*', (webSocket) => {
    setTimeout(() => webSocket.send(JSON.stringify(state)), 50);
  });

  await page.goto('/');
  const expected = {
    track: 'T',
    detection: 'D',
    device: 'V',
    system: 'S',
    sensor: 'N',
    alert: 'A',
    geometric: 'G',
    'location-only': 'L',
  };
  for (const [category, glyph] of Object.entries(expected)) {
    await expect(page.locator(`.entity-marker.category-${category}`)).toHaveCount(1);
    await expect(
      page.locator(`.entity-marker.category-${category} .marker-category-label`),
    ).toHaveText(glyph);
  }
  await expect(page.locator('#category-legend-items [data-legend-category]')).toHaveCount(8);
  await expect(page.locator('#category-legend-items')).toContainText('Geometric');
  await expect(page.locator('#affiliation-legend-items')).toContainText('Neutral');
  await expect(page.locator('[data-affiliation="NEUTRAL"]')).toHaveAttribute(
    'aria-label',
    /affiliation Neutral; freshness FRESH/,
  );
  await expect(page.getByTestId('entity-list')).toContainText('NEUTRAL · FRESH');
  for (const selector of [
    '.entity-marker.category-device .marker-affiliation-label',
    '.entity-marker.category-location-only .marker-freshness-label',
  ]) {
    const combined = await page.locator(selector).evaluate((node) => {
      const parent = node.parentElement;
      if (!parent) throw new Error('marker badge is missing its glyph parent');
      const parentTransform = new DOMMatrix(getComputedStyle(parent).transform);
      const childTransform = new DOMMatrix(getComputedStyle(node).transform);
      const effective = parentTransform.multiply(childTransform);
      return { a: effective.a, b: effective.b, c: effective.c, d: effective.d };
    });
    expect(combined.a).toBeCloseTo(1, 5);
    expect(combined.b).toBeCloseTo(0, 5);
    expect(combined.c).toBeCloseTo(0, 5);
    expect(combined.d).toBeCloseTo(1, 5);
  }
});

test('meets the automated accessibility gate, including the confirmation dialog', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  const initial = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'])
    .analyze();
  expect(initial.violations).toEqual([]);

  await page
    .getByTestId('entity-list')
    .getByRole('button', { name: /^Synthetic task target/ })
    .click();
  await page.locator('#arm-tasking').check();
  await page.locator('#task-command').selectOption('echo');
  await page.getByLabel('Message').fill('accessible synthetic request');
  await page.getByRole('button', { name: 'Prepare task' }).click();
  const dialog = page.getByRole('dialog', { name: 'Confirm one MQTT task dispatch' });
  await expect(dialog).toBeVisible();
  const modal = await new AxeBuilder({ page })
    .include('#confirm-dialog')
    .withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'])
    .analyze();
  expect(modal.violations).toEqual([]);
});

test('supports the complete select, prepare, and confirm workflow on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  const target = page
    .getByTestId('entity-list')
    .getByRole('button', { name: /^Synthetic task target/ });
  await target.click();
  const arm = page.locator('#arm-tasking');
  await arm.scrollIntoViewIfNeeded();
  await arm.check();
  await page.locator('#task-command').selectOption('echo');
  await page.getByLabel('Message').fill('mobile synthetic request');
  await page.getByRole('button', { name: 'Prepare task' }).click();
  const dialog = page.getByRole('dialog', { name: 'Confirm one MQTT task dispatch' });
  await expect(dialog).toBeVisible();
  await dialog.locator('#confirm-check').check();
  await dialog.getByRole('button', { name: 'Confirm and send once' }).click();
  await expect(page.getByTestId('task-outcome')).toContainText('SUCCESS', {
    timeout: 10_000,
  });
  await expect(dialog).not.toBeVisible();
});

test('discards a prepared token received after the target changes', async ({ page }) => {
  let racedToken: string | null = null;
  let racedView: string | null = null;
  let racedViewGeneration: string | null = null;
  let markBackendPrepared: () => void = () => undefined;
  const backendPrepared = new Promise<void>((resolve) => {
    markBackendPrepared = resolve;
  });
  let releasePreparedResponse: () => void = () => undefined;
  const preparedResponseGate = new Promise<void>((resolve) => {
    releasePreparedResponse = resolve;
  });
  await page.route('**/api/tasks/prepare', async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    racedToken = body.preparation_token;
    racedView = await route.request().headerValue('x-operator-view');
    racedViewGeneration = await route
      .request()
      .headerValue('x-operator-view-generation');
    markBackendPrepared();
    await preparedResponseGate;
    await route.fulfill({ response, json: body });
  });

  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  await page
    .getByTestId('entity-list')
    .getByRole('button', { name: /^Synthetic task target/ })
    .click();
  const priorOutcomeCount = await page.locator('[data-task-status]').count();
  await page.locator('#arm-tasking').check();
  await page.locator('#task-command').selectOption('echo');
  await page.getByLabel('Message').fill('target changes during prepare');
  const prepare = page.getByRole('button', { name: 'Prepare task' });
  await expect(prepare).toBeEnabled();
  await prepare.evaluate((button) => {
    if (!(button instanceof HTMLButtonElement) || button.disabled) {
      throw new Error('Prepare task must be an enabled button');
    }
    button.click();
  });
  await backendPrepared;
  try {
    await page
      .getByTestId('entity-list')
      .getByRole('button', { name: /^Synthetic moving track/ })
      .click();
  } finally {
    releasePreparedResponse();
  }
  await expect(page.getByTestId('task-outcome')).toContainText('readiness changed');

  if (!racedView || !racedViewGeneration) {
    throw new Error('prepare request omitted its operator view identity');
  }
  const status = await page.evaluate(
    async ({ token, viewId, viewGeneration }) => {
      const response = await fetch('/api/tasks/confirm', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Operator-Intent': 'confirm',
          'X-Operator-View': viewId,
          'X-Operator-View-Generation': viewGeneration,
        },
        body: JSON.stringify({ preparation_token: token, confirmed: true }),
      });
      return response.status;
    },
    { token: racedToken, viewId: racedView, viewGeneration: racedViewGeneration },
  );
  expect(status).toBe(409);
  await expect(page.locator('[data-task-status]')).toHaveCount(priorOutcomeCount);
});

test('awaits backend discard when a prepared target selection changes', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByTestId('connection-state')).toContainText('ready', {
    timeout: 10_000,
  });
  await page
    .getByTestId('entity-list')
    .getByRole('button', { name: /^Synthetic task target/ })
    .click();
  const priorOutcomeCount = await page.locator('[data-task-status]').count();
  await page.locator('#arm-tasking').check();
  await page.locator('#task-command').selectOption('echo');
  await page.getByLabel('Message').fill('discard after selecting another fresh entity');
  const preparationResponse = page.waitForResponse((response) =>
    response.url().endsWith('/api/tasks/prepare'),
  );
  await page.getByRole('button', { name: 'Prepare task' }).click();
  const preparedResponse = await preparationResponse;
  expect(preparedResponse.status()).toBe(200);
  const preparedToken = (await preparedResponse.json()).preparation_token;
  const preparedView = await preparedResponse.request().headerValue('x-operator-view');
  const preparedViewGeneration = await preparedResponse
    .request()
    .headerValue('x-operator-view-generation');
  if (!preparedView || !preparedViewGeneration) {
    throw new Error('prepare request omitted its operator view identity');
  }
  await expect(page.getByTestId('confirm-dialog')).toBeVisible();

  await page
    .getByTestId('entity-list')
    .getByRole('button', { name: /^Synthetic moving track/ })
    .evaluate((button) => {
    button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
  await expect(page.getByTestId('task-outcome')).toContainText(
    'selected target changed; nothing was published',
  );
  const status = await page.evaluate(
    async ({ token, viewId, viewGeneration }) => {
      const response = await fetch('/api/tasks/confirm', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Operator-Intent': 'confirm',
          'X-Operator-View': viewId,
          'X-Operator-View-Generation': viewGeneration,
        },
        body: JSON.stringify({ preparation_token: token, confirmed: true }),
      });
      return response.status;
    },
    {
      token: preparedToken,
      viewId: preparedView,
      viewGeneration: preparedViewGeneration,
    },
  );
  expect(status).toBe(409);
  await expect(page.locator('[data-task-status]')).toHaveCount(priorOutcomeCount);
});
