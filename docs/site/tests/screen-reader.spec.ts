// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * What the guide sounds like, rather than what it looks like.
 *
 * The rule-based audit in documentation.spec.ts reads every published page and
 * finds nothing, because the things a screen reader needs here are not things a
 * rule can see: whether a control that calls itself a menu behaves like one,
 * whether a name states the value it is standing for, whether a landmark leads
 * anywhere, and whether a page says out loud what it has just shown. Each test
 * below is one of those, checked against the accessibility tree the browser
 * actually builds.
 */
import { expect, test, type Page } from '@playwright/test';

import { documentationBase } from '../site-config.mjs';

const phone = { width: 390, height: 844 };
const desktop = { width: 1280, height: 900 };
const base = documentationBase;

function routeUrl(route: string): string {
  return route ? `${base}${route}/` : base;
}

/** The element the browser would hand a screen reader as focused. */
async function focusedName(page: Page): Promise<string> {
  return page.evaluate(() => {
    const active = document.activeElement;
    if (!active) return '';
    return (active.getAttribute('aria-label') ?? active.textContent ?? '').trim();
  });
}

/**
 * Expressive Code marks an overflowing block a region from a debounced
 * observer, and the name follows the role, so a page sampled before that pass
 * has run has no named region yet through no fault of the page.
 *
 * The wait is only for that pass to settle. Whether the page has an overflowing
 * block at all is a property of the page, so the caller asserts it and reads a
 * failed expectation rather than a timeout.
 */
async function awaitCodeRegions(page: Page): Promise<number> {
  let overflowing: (string | null)[] = [];
  await expect
    .poll(async () => {
      overflowing = await page
        .locator('.sl-markdown-content pre[data-language]')
        .evaluateAll((blocks) =>
          blocks
            .filter((block) => block.scrollWidth > block.clientWidth)
            .map((block) => block.getAttribute('aria-label')),
        );
      return overflowing.every((label) => label !== null);
    })
    .toBe(true);
  return overflowing.length;
}

test('the colour-scheme menu behaves like the menu it says it is', async ({ page }) => {
  await page.goto(base);
  const trigger = page.getByRole('button', { name: 'Color scheme' });
  const menu = page.getByRole('menu', { name: 'Color Scheme' });
  const option = (name: string) => menu.getByRole('menuitemradio', { name });

  // A menu button opens on the down arrow, onto the choice already in force
  // rather than onto the top of a list the reader has not been read yet.
  await trigger.focus();
  await page.keyboard.press('ArrowDown');
  await expect(trigger).toHaveAttribute('aria-expanded', 'true');
  await expect(option('System')).toBeFocused();

  // The arrows move within the menu and wrap at both ends.
  await page.keyboard.press('ArrowDown');
  await expect(option('Light')).toBeFocused();
  await page.keyboard.press('End');
  await expect(option('Dark')).toBeFocused();
  await page.keyboard.press('ArrowDown');
  await expect(option('System')).toBeFocused();
  await page.keyboard.press('ArrowUp');
  await expect(option('Dark')).toBeFocused();
  await page.keyboard.press('Home');
  await expect(option('System')).toBeFocused();

  // Escape closes it and hands the reader back where they were.
  await page.keyboard.press('Escape');
  await expect(menu).toBeHidden();
  await expect(trigger).toBeFocused();

  // Tab leaves the whole menu rather than stepping through what is left of it.
  await page.keyboard.press('ArrowDown');
  await expect(option('System')).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(menu).toBeHidden();
  expect(await focusedName(page)).not.toBe('System');
});

test('the version menu carries the changelog inside the menu it opens', async ({ page }) => {
  await page.goto(base);
  const trigger = page.getByRole('button', { name: /^Version v\d+\.\d+\.\d+$/ });
  const menu = page.getByRole('menu', { name: 'Version' });

  await trigger.focus();
  await page.keyboard.press('ArrowUp');
  // Opening upwards lands on the last item, which is what changed.
  await expect(menu.getByRole('menuitem', { name: 'Changelog' })).toBeFocused();

  await page.keyboard.press('ArrowDown');
  const release = menu.getByRole('menuitem').first();
  await expect(release).toBeFocused();
  await expect(release).toHaveAttribute('aria-current', 'true');

  // Everything the panel offers is reachable by the menu's own keys, so nothing
  // in it is stranded behind a Tab that would close it.
  await page.keyboard.press('End');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(
    new RegExp(`${base.replaceAll('/', '\\/')}changelog\\/$`),
  );
});

test('Space activates a link in the version menu without scrolling the page', async ({ page }) => {
  await page.goto(base);

  const trigger = page.getByRole('button', { name: /^Version v\d+\.\d+\.\d+$/ });
  const changelog = page.getByRole('menu', { name: 'Version' }).getByRole('menuitem', {
    name: 'Changelog',
  });
  await trigger.focus();
  await page.keyboard.press('ArrowUp');
  await expect(changelog).toBeFocused();
  await page.evaluate(() => {
    sessionStorage.setItem('version-menu-space-scrolled', 'false');
    window.addEventListener(
      'scroll',
      () => sessionStorage.setItem('version-menu-space-scrolled', 'true'),
      { passive: true },
    );
  });

  const repeatedActivations = await changelog.evaluate((item) => {
    let activations = 0;
    const recordActivation = (event: Event): void => {
      event.preventDefault();
      activations += 1;
    };
    item.addEventListener('click', recordActivation);
    item.dispatchEvent(
      new KeyboardEvent('keydown', { key: ' ', repeat: true, bubbles: true, cancelable: true }),
    );
    item.removeEventListener('click', recordActivation);
    return activations;
  });
  expect(repeatedActivations).toBe(0);

  await page.keyboard.press('Space');
  await expect(page).toHaveURL(
    new RegExp(`${base.replaceAll('/', '\\/')}changelog\\/$`),
  );
  await expect
    .poll(() => page.evaluate(() => sessionStorage.getItem('version-menu-space-scrolled')))
    .toBe('false');
});

test('a menu is one stop on the way through the band, not one per item', async ({ page }) => {
  await page.goto(base);
  const trigger = page.getByRole('button', { name: 'Color scheme' });
  await trigger.click();
  await expect(page.getByRole('menu', { name: 'Color Scheme' })).toBeVisible();

  // Only the item focus rests on is tabbable; the rest are reached by arrow.
  const tabbable = await page
    .locator('picogrid-color-scheme [role="menuitemradio"]')
    .evaluateAll((items) => items.map((item) => (item as HTMLElement).tabIndex));
  expect(tabbable.filter((index) => index === 0)).toHaveLength(1);
});

test('the header controls state the value they stand for', async ({ page }) => {
  await page.goto(routeUrl('getting-started/installation'));

  // The version button reads as a version rather than as a bare number.
  await expect(page.getByRole('button', { name: /^Version v\d+\.\d+\.\d+$/ })).toBeVisible();

  // Only the active scheme's glyph is drawn, so the button already says which
  // one is on to a reader who can see it; the name says it to everyone else.
  const trigger = page.getByRole('button', { name: 'Color scheme' });
  await expect(trigger).toHaveAttribute('aria-label', 'Color scheme: System');
  await trigger.click();
  await page.getByRole('menuitemradio', { name: 'Light' }).click();
  await expect(trigger).toHaveAttribute('aria-label', 'Color scheme: Light');

  // The choice is the theme's own, so the name still states it on the next page.
  await page.goto(routeUrl('concepts/tasks'));
  await expect(page.getByRole('button', { name: 'Color scheme' })).toHaveAttribute(
    'aria-label',
    'Color scheme: Light',
  );
});

test('a code block wide enough to scroll is a region that says what it holds', async ({ page }) => {
  await page.setViewportSize(phone);
  await page.goto(routeUrl('concepts/locations'));

  // The page has to hold a block worth scrolling, or the rest asserts nothing.
  expect(await awaitCodeRegions(page)).toBeGreaterThan(0);
  const labels = async () =>
    page
      .locator('.sl-markdown-content pre[data-language]')
      .evaluateAll((blocks) => blocks.map((block) => block.getAttribute('aria-label')));

  const narrow = await labels();
  const named = narrow.filter((label): label is string => label !== null);
  expect(named.length).toBeGreaterThan(0);
  expect(named.every((label) => label.trim().length > 0)).toBe(true);
  // A landmark list is only worth reading if two entries can be told apart.
  expect(new Set(named).size).toBe(named.length);
  expect(named.some((label) => /code/i.test(label))).toBe(true);

  // The label belongs to the region, so a block that is not one has no stray
  // name left on it where the attribute would not be allowed.
  await expect(
    page.locator('.sl-markdown-content pre:not([role="region"])[aria-label]'),
  ).toHaveCount(0);

  // Which blocks overflow depends on the width, so a block that is still a
  // region after a resize is the same region, under the same name.
  await page.setViewportSize(desktop);
  await expect
    .poll(async () => (await labels()).filter((label) => label !== null).length)
    .toBeLessThanOrEqual(named.length);
  for (const [index, label] of (await labels()).entries()) {
    if (label !== null) expect(label).toBe(narrow[index]);
  }
});

test('the scrollable code region is a focus stop that is drawn as one', async ({ page }) => {
  await page.setViewportSize(phone);
  await page.goto(routeUrl('concepts/locations'));

  const region = page.locator('.sl-markdown-content pre[role="region"]').first();
  await expect(region).toHaveAttribute('tabindex', '0');
  await region.evaluate((element) => (element as HTMLElement).focus());

  // Reached by keyboard it takes the guide's own ring rather than whatever the
  // browser draws around a `pre`.
  const outline = await region.evaluate((element) => {
    element.classList.add('focus-probe');
    return getComputedStyle(element, null).outlineWidth;
  });
  expect(Number.parseFloat(outline)).toBeGreaterThan(0);
});

test('a search says how many results it found', async ({ page }) => {
  await page.goto(base);
  await page.getByRole('button', { name: 'Search' }).first().click();
  const dialog = page.getByRole('dialog', { name: 'Search' });
  await expect(dialog).toBeVisible();

  // Pagefind writes the count into the dialog and marks it as nothing, so the
  // guide reports it from a region a screen reader is already watching.
  const status = dialog.getByRole('status');
  await dialog.locator('input').first().fill('broker ACLs');
  await expect(status).toHaveText(/\d+ results? for broker ACLs/);

  await dialog.locator('input').first().fill('acls');
  await expect(status).toHaveText(/\d+ results? for acls/);
});

// A landmark is a promise that there is something there to go to.
for (const [label, viewport] of [
  ['a phone', phone],
  ['a desktop', desktop],
] as const) {
  test(`every landmark published to ${label} leads somewhere`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto(routeUrl('concepts/locations'));

    const empty = await page.evaluate(() =>
      [
        ...document.querySelectorAll(
          'nav, aside, [role="navigation"], [role="complementary"], [role="region"]',
        ),
      ]
        .filter((element) => {
          // `checkVisibility` walks the ancestors, so a landmark inside a pane
          // the layout has switched off is not one a reader is offered.
          if (!element.checkVisibility({ visibilityProperty: true })) return false;
          if (element.closest('[hidden], [inert], [aria-hidden="true"]')) return false;
          // A wrapper whose contents are positioned measures nothing itself and
          // still leads somewhere, so what has to render is the subtree.
          return ![element, ...element.querySelectorAll('*')].some(
            (node) => node.getClientRects().length > 0,
          );
        })
        .map((element) => element.className.toString() || element.tagName.toLowerCase()),
    );
    expect(empty).toEqual([]);
  });
}

test('the guide opens on a way past the band', async ({ page }) => {
  await page.goto(routeUrl('reference/api'));

  // The first thing a reader reaches is the way out of the navigation, and it
  // shows itself when it is reached rather than staying hidden under focus.
  await page.keyboard.press('Tab');
  const skip = page.getByRole('link', { name: 'Skip to content' });
  await expect(skip).toBeFocused();
  await expect(skip).toBeVisible();

  const target = await skip.evaluate((element) => element.getAttribute('href'));
  await expect(page.locator(`main ${target}`)).toHaveCount(1);
});
