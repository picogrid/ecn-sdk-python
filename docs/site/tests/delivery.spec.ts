// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * Delivery browser pack: Worker-served journeys under /ecn-sdk/ (A5.1).
 *
 * Complements HTTP edge smoke (A5) and the offline full suite
 * (documentation.spec.ts). Keep this list short — nav, search, assets, 404 —
 * so it is cheap enough for every PR against wrangler dev and for preview/prod.
 */
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";

import { documentationBase } from "../site-config.mjs";

const accessibilityTags = ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"];
const base = documentationBase;
const brandIconHref = `${base}brand/picogrid-app-icon-192.png`;
const installationPath = `${base}getting-started/installation/`;
const observePath = `${base}quickstarts/observe-data/`;
const aclsPath = `${base}concepts/acls/`;

async function tabTo(
  page: Page,
  target: Locator,
  maximumTabs = 40,
): Promise<void> {
  for (let index = 0; index < maximumTabs; index += 1) {
    await page.keyboard.press("Tab");
    if (await target.evaluate((element) => document.activeElement === element))
      return;
  }
  throw new Error("keyboard focus did not reach the expected control");
}

test("home renders under the Worker base path", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  const response = await page.goto(base, { waitUntil: "networkidle" });
  expect(response?.status()).toBe(200);
  await expect(page.locator("main h1")).toHaveCount(1);
  await expect(page.locator(".site-title")).toContainText("Picogrid ECN SDK");
  await expect(page.locator('link[rel~="icon"]')).toHaveAttribute(
    "href",
    brandIconHref,
  );

  const icon = await page.request.get(brandIconHref);
  expect(icon.status()).toBe(200);
  expect(icon.headers()["content-type"] ?? "").toMatch(/image\//);

  expect(consoleErrors).toEqual([]);

  const report = await new AxeBuilder({ page })
    .withTags(accessibilityTags)
    .analyze();
  expect(report.violations, JSON.stringify(report.violations, null, 2)).toEqual(
    [],
  );
});

test("sidebar navigation reaches known journeys", async ({ page }) => {
  await page.goto(`${base}getting-started/configuration/`, {
    waitUntil: "networkidle",
  });
  const navigation = page.getByRole("navigation", { name: "Main" });

  await navigation
    .getByRole("link", { name: "Install the SDK", exact: true })
    .click();
  await expect(page).toHaveURL(
    new RegExp(
      `${base.replaceAll("/", "\\/")}getting-started\\/installation\\/$`,
    ),
  );
  await expect(
    page.getByRole("heading", { level: 1, name: "Install the SDK" }),
  ).toBeVisible();

  await navigation.getByRole("link", { name: "Observe ECN data" }).click();
  await expect(page).toHaveURL(
    new RegExp(`${base.replaceAll("/", "\\/")}quickstarts\\/observe-data\\/$`),
  );
  await expect(
    page.getByRole("heading", { level: 1, name: "Observe ECN data" }),
  ).toBeVisible();
});

test("nested journey pages and trailing slashes are served", async ({
  page,
}) => {
  const response = await page.goto(installationPath, {
    waitUntil: "networkidle",
  });
  expect(response?.status()).toBe(200);
  expect(page.url()).toMatch(/\/ecn-sdk\/getting-started\/installation\/?$/);
  await expect(page.locator("main h1")).toHaveCount(1);

  const observe = await page.goto(observePath, { waitUntil: "networkidle" });
  expect(observe?.status()).toBe(200);
  await expect(page.locator("main h1")).toHaveCount(1);
});

test("mobile menu reaches a sensor quickstart", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${base}getting-started/configuration/`, {
    waitUntil: "networkidle",
  });
  const menu = page.getByRole("button", { name: "Menu" });
  await menu.click();
  await expect(menu).toHaveAttribute("aria-expanded", "true");
  await page.getByRole("link", { name: "Build a sensor publisher" }).click();
  await expect(page).toHaveURL(
    new RegExp(
      `${base.replaceAll("/", "\\/")}quickstarts\\/sensor-publisher\\/$`,
    ),
  );
  await expect(
    page.getByRole("heading", { level: 1, name: "Build a sensor publisher" }),
  ).toBeVisible();
});

test("keyboard-only navigation reaches the installation journey", async ({
  page,
}) => {
  await page.goto(base, { waitUntil: "networkidle" });
  // The overview is a page of the guide, so the sidebar names this route too.
  // The journey under test is the one the overview itself offers.
  const install = page.getByRole("main").getByRole("link", {
    name: "Install the SDK",
    exact: true,
  });
  // The band publishes its own controls ahead of the page, so the reader
  // passes more stops before reaching the overview's action.
  await tabTo(page, install, 80);
  await expect(install).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(
    new RegExp(
      `${base.replaceAll("/", "\\/")}getting-started\\/installation\\/$`,
    ),
  );
});

test("local search reaches broker authorization guidance", async ({ page }) => {
  await page.goto(base, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "Search" }).first().click();
  const search = page.getByRole("dialog", { name: "Search" });
  await expect(search).toBeVisible();
  await search.locator("input").fill("broker ACLs");
  const result = search.getByRole("link", { name: /Broker ACLs/i }).first();
  await expect(result).toBeVisible();
  await result.click();
  await expect(page).toHaveURL(
    new RegExp(`${base.replaceAll("/", "\\/")}concepts\\/acls\\/$`),
  );
  expect(page.url()).toContain(aclsPath.replace(/\/$/, ""));
});

test("branded 404 recovery stays under the guide mount", async ({ page }) => {
  const response = await page.goto(`${base}smoke-delivery-missing-page/`);
  expect(response?.status()).toBe(404);
  await expect(
    page.getByRole("heading", { level: 1, name: "Page not found" }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Return to the guide" }),
  ).toHaveAttribute("href", base);
  await expect(
    page.getByRole("link", { name: "Observe ECN data" }),
  ).toHaveAttribute("href", observePath);
});

test("a representative page shows a code sample", async ({ page }) => {
  await page.goto(installationPath, { waitUntil: "networkidle" });
  const sample = page.locator("main pre, main code").first();
  await expect(sample).toBeVisible();
  await expect(sample).not.toBeEmpty();
});
