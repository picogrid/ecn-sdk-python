// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Locator, type Page } from '@playwright/test';

import { readFileSync } from 'node:fs';

import { resolveLegionDocumentation } from '../legion-documentation.mjs';
import { publicGuideRoutes } from '../public-routes.mjs';
import { documentationBase } from '../site-config.mjs';

// The conformance tags do not include the rules a screen reader depends on most
// — that landmarks are distinguishable, that heading levels are not skipped,
// and that a page has one first-level heading — so the best-practice set is
// asked for as well. See site/tests/screen-reader.spec.ts for what it misses.
const accessibilityTags = ['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa', 'best-practice'];
const base = documentationBase;
const brandIconHref = `${base}brand/picogrid-app-icon-192.png`;

function routeUrl(route: string): string {
  return route ? `${base}${route}/` : base;
}

async function expectAccessible(page: Page): Promise<void> {
  // Expressive Code marks an overflowing code block focusable from a debounced
  // observer, so sampling the page before that pass runs reports a scrollable
  // region the theme is about to fix.
  await page.waitForFunction(() => (
    [...document.querySelectorAll('.expressive-code pre')].every((block) => (
      block.scrollWidth <= block.clientWidth || block.hasAttribute('tabindex')
    ))
  ));
  const report = await new AxeBuilder({ page }).withTags(accessibilityTags).analyze();
  expect(report.violations, JSON.stringify(report.violations, null, 2)).toEqual([]);
}

async function tabTo(page: Page, target: Locator, maximumTabs = 40): Promise<void> {
  for (let index = 0; index < maximumTabs; index += 1) {
    await page.keyboard.press('Tab');
    if (await target.evaluate((element) => document.activeElement === element)) return;
  }
  throw new Error('keyboard focus did not reach the expected control');
}

type DocumentationTheme = 'dark' | 'light';

const documentationThemes: DocumentationTheme[] = ['dark', 'light'];

function parseCssRgb(color: string): [number, number, number] {
  if (!/^rgba?\(.*\)$/.test(color)) {
    throw new Error(`expected a computed RGB color, received ${color}`);
  }
  const channels = color.match(/[\d.]+/g)?.slice(0, 3).map(Number);
  if (!channels || channels.length !== 3 || channels.some((channel) => !Number.isFinite(channel))) {
    throw new Error(`expected a computed RGB color, received ${color}`);
  }
  return channels as [number, number, number];
}

function relativeLuminance(color: string): number {
  const channels = parseCssRgb(color).map((channel) => {
    const srgb = channel / 255;
    return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function contrastRatio(foreground: string, background: string): number {
  const foregroundLuminance = relativeLuminance(foreground);
  const backgroundLuminance = relativeLuminance(background);
  const lighter = Math.max(foregroundLuminance, backgroundLuminance);
  const darker = Math.min(foregroundLuminance, backgroundLuminance);
  return (lighter + 0.05) / (darker + 0.05);
}

function versionTrigger(page: Page): Locator {
  return page.getByRole('button', { name: /^Version v\d+\.\d+\.\d+/ });
}

for (const route of publicGuideRoutes) {
  test(`${route || 'home'} renders one useful document outline`, async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text());
    });
    await page.emulateMedia({ reducedMotion: 'reduce' });

    const response = await page.goto(routeUrl(route), { waitUntil: 'networkidle' });
    expect(response?.status()).toBe(200);
    await expect(page.locator('main h1')).toHaveCount(1);

    const desktopToc = page.locator('starlight-toc');
    if (await desktopToc.count()) {
      const links = await desktopToc.locator('a').evaluateAll((anchors) => (
        anchors.map((anchor) => anchor.getAttribute('href'))
      ));
      expect(
        links.some((href) => href !== null && href !== '#_top'),
        'A rendered On this page panel must link to a real section, not only Overview.',
      ).toBe(true);
    }

    await expectAccessible(page);
    expect(consoleErrors).toEqual([]);
  });
}

test('the navigation band is the same brand surface in light and dark themes', async ({ page }) => {
  await page.goto(base);
  await expect(page.locator('link[rel~="icon"]')).toHaveAttribute(
    'href',
    brandIconHref,
  );
  const title = page.locator('.site-title');
  await expect(title).toBeVisible();
  await expect(title).toContainText('Picogrid ECN SDK');

  const bands: Record<string, { artwork: string; foreground: string; surface: string }> = {};
  for (const theme of ['light', 'dark']) {
    await page.locator('html').evaluate((element, nextTheme) => {
      element.setAttribute('data-theme', nextTheme);
    }, theme);
    // The band is dark in either theme, so it carries the white wordmark rather
    // than the black-on-light one the page itself would use.
    const wordmark = await title.evaluate((element) => (
      getComputedStyle(element, '::before').backgroundImage
    ));
    expect(wordmark, `wordmark on the ${theme} page`).toContain('picogrid-wordmark-dark.png');

    const band = await page.locator('header.header').evaluate((element) => {
      const header = getComputedStyle(element);
      return {
        artwork: header.backgroundImage,
        foreground: header.color,
        surface: header.backgroundColor,
        titleColour: getComputedStyle(element.querySelector('.site-title')!).color,
      };
    });
    expect(band.artwork, `band artwork on the ${theme} page`).toContain('picogrid-nav-texture.png');
    expect(band.titleColour, `site title on the ${theme} page`).toBe(band.foreground);
    bands[theme] = { artwork: band.artwork, foreground: band.foreground, surface: band.surface };
  }
  expect(bands.light).toEqual(bands.dark);
});

const highlightingExamples = [
  {
    blockText: 'pip install picogrid-ecn-client==',
    language: 'bash',
    minimumColors: 3,
    route: 'getting-started/installation',
  },
  {
    blockText: 'picogrid-ecn clock check',
    language: 'bash',
    minimumColors: 3,
    route: 'how-to/check-clock',
  },
  {
    blockText: 'client.clock.measure',
    language: 'python',
    minimumColors: 5,
    route: 'how-to/check-clock',
  },
  {
    blockText: '# Audit the literal-local',
    language: 'python',
    minimumColors: 6,
    route: 'walkthroughs/task-handler-service',
  },
] as const;

test('authored and generated code has token diversity in both themes', async ({ page }) => {
  for (const example of highlightingExamples) {
    await page.goto(routeUrl(example.route));
    const block = page.locator(`pre[data-language="${example.language}"]`)
      .filter({ hasText: example.blockText });
    await expect(
      block,
      `${example.route} must render the ${example.language} block containing `
        + example.blockText,
    ).toHaveCount(1);
    const themeColors: Record<DocumentationTheme, string[]> = { dark: [], light: [] };

    for (const theme of documentationThemes) {
      await page.locator('html').evaluate((element, nextTheme) => {
        element.setAttribute('data-theme', nextTheme);
      }, theme);
      const colors = await block.evaluate((element) => (
        Array.from(element.querySelectorAll('span'))
          .filter((span) => !span.closest('.gutter') && !span.classList.contains('shell-prompt'))
          .map((span) => getComputedStyle(span).color)
      ));
      const distinctColors = [...new Set(colors)].sort();
      expect(
        distinctColors.length,
        `${example.route} ${example.language} ${theme} theme must render at least `
          + `${example.minimumColors} distinct computed token colors`,
      ).toBeGreaterThanOrEqual(example.minimumColors);
      themeColors[theme] = distinctColors;
    }

    expect(
      themeColors.dark,
      `${example.route} ${example.language} computed token colors must differ between dark `
        + 'and light themes',
    ).not.toEqual(themeColors.light);
  }

  const generatedRoute = 'reference/python/client/ecn-client';
  await page.goto(routeUrl(generatedRoute));
  const generatedBlocks = page.locator('pre[data-language="python"]');
  await expect(generatedBlocks).not.toHaveCount(0);
  const generatedThemeColors: Record<DocumentationTheme, string[]> = { dark: [], light: [] };

  for (const theme of documentationThemes) {
    await page.locator('html').evaluate((element, nextTheme) => {
      element.setAttribute('data-theme', nextTheme);
    }, theme);
    const blockColors = await generatedBlocks.evaluateAll((elements) => elements.map((element) => (
      Array.from(element.querySelectorAll('span'))
        .filter((span) => !span.closest('.gutter') && !span.classList.contains('shell-prompt'))
        .map((span) => getComputedStyle(span).color)
    )));
    const pageColors = [...new Set(blockColors.flat())].sort();
    expect(
      pageColors.length,
      `${generatedRoute} python ${theme} theme must render at least 5 distinct computed `
        + 'token colors across the generated page',
    ).toBeGreaterThanOrEqual(5);
    expect(
      blockColors.some((colors) => new Set(colors).size >= 4),
      `${generatedRoute} python ${theme} theme must render at least one block with 4 distinct `
        + 'computed token colors',
    ).toBe(true);
    generatedThemeColors[theme] = pageColors;
  }

  expect(
    generatedThemeColors.dark,
    `${generatedRoute} python computed token colors must differ between dark and light themes`,
  ).not.toEqual(generatedThemeColors.light);
});

test('bash and Python comments resolve separately from language tokens', async ({ page }) => {
  const bashRoute = 'getting-started/installation';
  await page.goto(routeUrl(bashRoute));
  const bashTokens = await page.locator('pre[data-language="bash"]')
    .filter({ hasText: 'python3.14 -m venv' })
    .filter({ hasText: 'pip install picogrid-ecn-client==' })
    .evaluate((element) => {
      const spans = Array.from(element.querySelectorAll('span'))
        .filter((span) => !span.closest('.gutter') && !span.classList.contains('shell-prompt'));
      const comment = spans.find((span) => span.textContent?.trimStart().startsWith('#'));
      const command = spans.find((span) => (
        span.textContent?.includes('python3') && !span.textContent.trimStart().startsWith('#')
      ));
      return {
        comment: comment
          ? { color: getComputedStyle(comment).color, text: comment.textContent }
          : null,
        command: command
          ? { color: getComputedStyle(command).color, text: command.textContent }
          : null,
      };
    });
  if (!bashTokens.comment) {
    throw new Error(`${bashRoute} bash block must contain a comment token starting with #`);
  }
  if (!bashTokens.command) {
    throw new Error(`${bashRoute} bash block must contain a python3 command token`);
  }
  expect(
    bashTokens.comment.color,
    `${bashRoute} bash comment and command tokens must have different computed colors`,
  ).not.toBe(bashTokens.command.color);

  const pythonRoute = 'walkthroughs/task-handler-service';
  await page.goto(routeUrl(pythonRoute));
  const pythonTokens = await page.locator('pre[data-language="python"]')
    .filter({ hasText: '# Audit the literal-local' })
    .evaluate((element) => {
      const spans = Array.from(element.querySelectorAll('span'));
      const comment = spans.find((span) => span.textContent?.trimStart().startsWith('#'));
      // Quotes are tokenized as their own punctuation spans, so the string
      // body is matched on its own.
      const quotedString = spans.find((span) => span.textContent === 'calculate');
      const keyword = spans.find((span) => span.textContent?.trim() === 'class');
      const token = (span: Element | undefined) => span
        ? { color: getComputedStyle(span).color, text: span.textContent }
        : null;
      return {
        comment: token(comment),
        keyword: token(keyword),
        quotedString: token(quotedString),
      };
    });
  if (!pythonTokens.comment) {
    throw new Error(`${pythonRoute} Python block must contain a comment token starting with #`);
  }
  if (!pythonTokens.quotedString) {
    throw new Error(`${pythonRoute} Python block must contain the string body calculate`);
  }
  if (!pythonTokens.keyword) {
    throw new Error(`${pythonRoute} Python block must contain the keyword class`);
  }
  expect(
    new Set([
      pythonTokens.comment.color,
      pythonTokens.quotedString.color,
      pythonTokens.keyword.color,
    ]).size,
    `${pythonRoute} Python comment, string and keyword tokens must each resolve to their own `
      + 'computed color',
  ).toBe(3);
});

test('highlighted shell blocks retain copy controls and mobile overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(routeUrl('getting-started/installation'));

  const bashBlock = page.locator('pre[data-language="bash"]')
    .filter({ hasText: 'python3.14 -m venv' })
    .filter({ hasText: 'pip install picogrid-ecn-client==' });
  await expect(bashBlock).toHaveCount(1);
  const frame = page.locator('figure.frame.is-terminal').filter({ has: bashBlock });
  const copyButton = frame.locator('button[title="Copy to clipboard"]');
  await expect(copyButton).toHaveCount(1);
  const copyCode = await copyButton.getAttribute('data-code');
  expect(
    copyCode?.trim(),
    'installation bash copy control must have non-empty data-code',
  ).toBeTruthy();
  expect(
    copyCode,
    'installation bash copy control must contain the authored virtual-environment command',
  ).toContain('python3.14 -m venv');

  const geometry = await page.locator('pre[data-language="bash"]').evaluateAll((elements) => {
    const longest = elements.reduce((current, candidate) => (
      candidate.scrollWidth - candidate.clientWidth > current.scrollWidth - current.clientWidth
        ? candidate
        : current
    ));
    const box = longest.getBoundingClientRect();
    longest.scrollLeft = longest.scrollWidth;
    return {
      clientWidth: longest.clientWidth,
      documentWidth: document.documentElement.scrollWidth,
      inlineEnd: box.right,
      inlineStart: box.left,
      overflowX: getComputedStyle(longest).overflowX,
      scrollWidth: longest.scrollWidth,
      scrollLeft: longest.scrollLeft,
      viewportWidth: window.innerWidth,
    };
  });
  expect(['auto', 'scroll']).toContain(geometry.overflowX);
  expect(geometry.scrollWidth).toBeGreaterThan(geometry.clientWidth);
  expect(geometry.scrollLeft).toBeGreaterThan(0);
  expect(geometry.inlineStart).toBeGreaterThanOrEqual(0);
  expect(geometry.inlineEnd).toBeLessThanOrEqual(geometry.viewportWidth + 1);
  expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewportWidth + 1);
});

test('dimmest highlighted tokens meet AA contrast in both themes', async ({ page }) => {
  const contrastExamples = [
    {
      blockText: 'python3.14 -m venv',
      language: 'bash',
      route: 'getting-started/installation',
    },
    {
      blockText: null,
      language: 'python',
      route: 'reference/python/client/ecn-client',
    },
  ] as const;

  for (const example of contrastExamples) {
    await page.goto(routeUrl(example.route));
    const languageBlocks = page.locator(`pre[data-language="${example.language}"]`);
    const blocks = example.blockText
      ? languageBlocks.filter({ hasText: example.blockText })
      : languageBlocks;
    await expect(blocks).not.toHaveCount(0);

    for (const theme of documentationThemes) {
      await page.locator('html').evaluate((element, nextTheme) => {
        element.setAttribute('data-theme', nextTheme);
      }, theme);
      const tokens = await blocks.evaluateAll((elements, context) => {
        const resolveOpaqueBackground = (element: Element): string => {
          let ancestor: Element | null = element;
          while (ancestor) {
            const background = getComputedStyle(ancestor).backgroundColor;
            const format = background.match(/^(rgb|rgba)\(.*\)$/);
            const channels = background.match(/[\d.]+/g)?.map(Number);
            const expectedChannels = format?.[1] === 'rgba' ? 4 : 3;
            if (
              !format
              || !channels
              || channels.length !== expectedChannels
              || channels.some((channel) => !Number.isFinite(channel))
            ) {
              throw new Error(
                `${context.route} ${context.language} ${context.theme} theme cannot interpret `
                  + `computed background color ${background}`,
              );
            }
            if ((channels[3] ?? 1) === 1) return background;
            ancestor = ancestor.parentElement;
          }
          throw new Error(
            `${context.route} ${context.language} ${context.theme} theme must resolve an opaque `
              + 'code background from the pre element or one of its ancestors',
          );
        };

        return elements.flatMap((element) => {
          const background = resolveOpaqueBackground(element);
          return Array.from(element.querySelectorAll('span'))
            .filter((span) => !span.closest('.gutter') && !span.classList.contains('shell-prompt'))
            .map((span) => ({
              background,
              color: getComputedStyle(span).color,
              text: span.textContent ?? '',
            }));
        });
      }, { language: example.language, route: example.route, theme });
      if (tokens.length === 0) {
        throw new Error(
          `${example.route} ${example.language} ${theme} theme must render highlighted tokens`,
        );
      }
      const dimmest = tokens.reduce((current, candidate) => (
        contrastRatio(candidate.color, candidate.background)
          < contrastRatio(current.color, current.background)
          ? candidate
          : current
      ));
      expect(
        contrastRatio(dimmest.color, dimmest.background),
        `${example.route} ${example.language} ${theme} theme dimmest token ${JSON.stringify(
          dimmest.text,
        )} must have at least 4.5:1 contrast against the computed code background`,
      ).toBeGreaterThanOrEqual(4.5);

      const comments = tokens.filter((token) => token.text.trimStart().startsWith('#'));
      if (comments.length > 0) {
        const dimmestComment = comments.reduce((current, candidate) => (
          contrastRatio(candidate.color, candidate.background)
            < contrastRatio(current.color, current.background)
            ? candidate
            : current
        ));
        expect(
          contrastRatio(dimmestComment.color, dimmestComment.background),
          `${example.route} ${example.language} ${theme} theme comment token ${JSON.stringify(
            dimmestComment.text,
          )} must have at least 4.5:1 contrast against the computed code background`,
        ).toBeGreaterThanOrEqual(4.5);
      }
    }
  }
});

test('the shell prompt is decorative and never reaches the clipboard', async ({
  context,
  page,
}) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.goto(routeUrl('getting-started/installation'));

  const frame = page.locator('figure.frame.is-terminal').filter({
    has: page.locator('pre[data-language="bash"]')
      .filter({ hasText: 'python3.14 -m venv' })
      .filter({ hasText: 'pip install picogrid-ecn-client==' }),
  });
  await expect(frame).toHaveCount(1);

  // Mirror the renderer's rule: every authored line carries a prompt element,
  // and the prompt is visible only on a line that starts a command.
  const codeLines = await frame.locator('.ec-line .code').allInnerTexts();
  const showsPrompt = (index: number): boolean => {
    const text = codeLines[index];
    if (text.trim().length === 0 || text.trimStart().startsWith('#')) return false;
    if (index === 0) return true;
    const previous = codeLines[index - 1];
    return previous.trimStart().startsWith('#') || !previous.trimEnd().endsWith('\\');
  };
  const prompts = frame.locator('.ec-line .gutter .shell-prompt');
  await expect(prompts).toHaveCount(codeLines.length);
  expect(codeLines.some((line, index) => showsPrompt(index))).toBe(true);
  for (const [index, prompt] of (await prompts.all()).entries()) {
    await expect(prompt).toHaveAttribute('aria-hidden', 'true');
    await expect(prompt).toHaveText(showsPrompt(index) ? '$' : '');
  }
  const decoration = await prompts.first().evaluate((element) => {
    const style = getComputedStyle(element);
    return { pointerEvents: style.pointerEvents, userSelect: style.userSelect };
  });
  expect(decoration.userSelect, 'the shell prompt must not be selectable').toBe('none');
  expect(decoration.pointerEvents, 'the shell prompt must not be interactive').toBe('none');

  await frame.locator('button[title="Copy to clipboard"]').click();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied, 'the copied command must be the authored text').toContain(
    'python3.14 -m venv .venv-ecn-sdk',
  );
  expect(copied, 'the copied command must not carry the decorative prompt').not.toMatch(
    /(?:^|\n)\s*\$\s/,
  );

  // A comment line carries an empty prompt element so the code column stays
  // aligned without reading as a command the user is meant to run.
  const verifyFrame = page.locator('figure.frame.is-terminal')
    .filter({ has: page.locator('pre').filter({ hasText: 'Or query the package metadata directly' }) });
  const verifyLines = await verifyFrame.locator('.ec-line .code').allInnerTexts();
  const commentIndex = verifyLines.findIndex((line) => line.trimStart().startsWith('#'));
  expect(commentIndex, 'the verification block must keep an authored comment line').toBeGreaterThan(-1);
  await expect(
    verifyFrame.locator('.ec-line').nth(commentIndex).locator('.gutter .shell-prompt'),
  ).toHaveText('');
});

test('the brand row names both Picogrid documentation sets', async ({ page }) => {
  await page.goto(routeUrl('concepts/tasks'));
  const sets = page.getByRole('navigation', { name: 'Documentation' });

  const guide = sets.getByRole('link', { name: 'ECN SDK' });
  await expect(guide).toBeVisible();
  await expect(guide).toHaveAttribute('href', base);
  await expect(guide).toHaveAttribute('aria-current', 'true');

  // The Legion target is a build input, so the test asks for the resolved one.
  const documentation = resolveLegionDocumentation();
  const legion = sets.getByRole('link', { name: documentation.label });
  await expect(legion).toBeVisible();
  await expect(legion).toHaveAttribute('href', documentation.href);
  await expect(legion).not.toHaveAttribute('aria-current', /.*/);
});

test('the version menu links the release to the source it was built from', async ({ page }) => {
  await page.goto(base);
  const trigger = versionTrigger(page);
  await trigger.click();
  await expect(trigger).toHaveAttribute('aria-expanded', 'true');

  // Provenance metadata names the exact private engineering commit. The
  // reader-facing link must instead name an exported public ref: the release
  // tag when injected, or public main for ordinary clean and dirty builds.
  const commitMeta = page.locator('meta[name="source-commit"]');
  const commit = (await commitMeta.count()) ? await commitMeta.getAttribute('content') : null;
  if (commit === null) {
    await expect(page.locator('meta[name="source-ref"]')).toHaveAttribute('content', 'main');
  } else {
    expect(commit).toMatch(/^[0-9a-f]{40}$/);
    await expect(page.locator('meta[name="source-ref"]')).toHaveAttribute('content', commit);
  }
  const publicReference = process.env.DOCS_GIT_TAG || 'main';
  const publicSourceKind = process.env.DOCS_GIT_TAG ? 'tag' : 'branch';
  const publicTitle = `Built from ${publicSourceKind} ${publicReference}`;

  const menu = page.getByRole('menu', { name: 'Version' });
  const release = menu.getByRole('menuitem').first();
  await expect(release).toHaveAttribute('aria-current', 'true');
  await expect(release).toHaveAttribute(
    'href',
    `https://github.com/picogrid/ecn-sdk-python/tree/${publicReference}`,
  );
  await expect(release).toHaveAttribute('title', publicTitle);
  await expect(release).toContainText(`${publicSourceKind} ${publicReference}`);
  const footerSource = page.locator('.documentation-source a');
  await expect(footerSource).toHaveAttribute(
    'href',
    `https://github.com/picogrid/ecn-sdk-python/tree/${publicReference}`,
  );
  await expect(footerSource).toHaveText(publicReference);
  await expect(footerSource).toHaveAttribute('title', publicTitle);
  // What changed between releases is asked of the same control.
  const panel = page.locator('header.header .version-panel');
  await expect(panel.getByRole('menuitem', { name: 'Changelog' })).toBeVisible();
  await expectAccessible(page);

  await page.keyboard.press('Escape');
  await expect(menu).toBeHidden();
  await expect(trigger).toBeFocused();

  await trigger.click();
  await page.locator('header.header .version-panel')
    .getByRole('menuitem', { name: 'Changelog' })
    .click();
  await expect(page).toHaveURL(
    new RegExp(`${base.replaceAll('/', '\\/')}changelog\\/$`),
  );
});

test('the colour scheme menu applies a choice and remembers it', async ({ page }) => {
  await page.goto(routeUrl('getting-started/installation'));
  const trigger = page.getByRole('button', { name: 'Color scheme' });
  await trigger.click();

  const menu = page.getByRole('menu', { name: 'Color Scheme' });
  await expect(menu.getByRole('menuitemradio', { name: 'System' }))
    .toHaveAttribute('aria-checked', 'true');
  await expectAccessible(page);

  await menu.getByRole('menuitemradio', { name: 'Light' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await expect(menu).toBeHidden();

  // The choice is the theme's own stored preference, so it survives navigation.
  await page.goto(routeUrl('concepts/tasks'));
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.getByRole('button', { name: 'Color scheme' }).click();
  await expect(
    page.getByRole('menu', { name: 'Color Scheme' }).getByRole('menuitemradio', { name: 'Light' }),
  ).toHaveAttribute('aria-checked', 'true');
});

test('an explicit colour scheme survives OS changes when storage is unavailable', async ({ page }) => {
  await page.addInitScript(() => {
    Storage.prototype.getItem = () => {
      throw new Error('storage unavailable');
    };
    Storage.prototype.setItem = () => {
      throw new Error('storage unavailable');
    };
  });
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.goto(base);

  await page.getByRole('button', { name: 'Color scheme' }).click();
  await page
    .getByRole('menu', { name: 'Color Scheme' })
    .getByRole('menuitemradio', { name: 'Light' })
    .click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');

  await page.emulateMedia({ colorScheme: 'light' });
  await page.emulateMedia({ colorScheme: 'dark' });
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
});

test('the version menu supports keyboard navigation', async ({ page }) => {
  await page.goto(base);
  const trigger = versionTrigger(page);
  await trigger.focus();
  await page.keyboard.press('Enter');

  const menu = page.getByRole('menu', { name: 'Version' });
  const current = menu.getByRole('menuitem').first();
  const changelog = menu.getByRole('menuitem', { name: 'Changelog' });
  await expect(menu).toBeVisible();
  await expect(current).toBeFocused();
  await expectAccessible(page);

  await page.keyboard.press('Home');
  await expect(current).toBeFocused();
  await page.keyboard.press('End');
  await expect(changelog).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(menu).toBeHidden();
  await expect(trigger).toBeFocused();
});

test('the version menu reaches Changelog from the keyboard', async ({ page }) => {
  await page.goto(base);
  const trigger = versionTrigger(page);
  await trigger.focus();
  await page.keyboard.press('Enter');

  const changelog = page
    .getByRole('menu', { name: 'Version' })
    .getByRole('menuitem', { name: 'Changelog' });
  await page.keyboard.press('ArrowDown');
  await expect(changelog).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(
    new RegExp(`${base.replaceAll('/', '\\/')}changelog\\/$`),
  );
});

test('the colour scheme menu supports roving keyboard navigation', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('starlight-theme', 'dark'));
  await page.goto(base);
  const trigger = page.getByRole('button', { name: 'Color scheme' });
  await trigger.focus();
  await page.keyboard.press('Space');

  const menu = page.getByRole('menu', { name: 'Color Scheme' });
  const system = menu.getByRole('menuitemradio', { name: 'System' });
  const light = menu.getByRole('menuitemradio', { name: 'Light' });
  const dark = menu.getByRole('menuitemradio', { name: 'Dark' });
  await expect(menu).toBeVisible();
  await expect(dark).toBeFocused();
  await expectAccessible(page);

  await page.keyboard.press('ArrowDown');
  await expect(system).toBeFocused();
  await page.keyboard.press('ArrowDown');
  await expect(light).toBeFocused();
  await page.keyboard.press('ArrowUp');
  await expect(system).toBeFocused();
  await page.keyboard.press('End');
  await expect(dark).toBeFocused();
  await page.keyboard.press('Home');
  await expect(system).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(menu).toBeHidden();
  await expect(trigger).toBeFocused();
});

// The band gives its rows to the page and to search below this width, and the
// flyout carries the guide, the version, and the documentation sets.
const flyoutViewport = { width: 390, height: 844 };

test('the band keeps one height per arrangement and its controls across the swap', async ({ page }) => {
  // The band carries Legion's two heights, one for each arrangement, so the
  // guide and the Legion documentation present the same chrome at the same
  // width. What a reader must not meet is a third height, or a height that
  // moves while the arrangement does not: the swap happens at the one width
  // where the sidebar becomes a drawer and the page is rearranging anyway.
  const heights = new Map<number, number>();
  // Either side of the width where the band and the sidebar change together.
  for (const width of [flyoutViewport.width, 799, 800, 1280]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto(routeUrl('reference/api'));

    heights.set(width, Math.round(
      await page.locator('header.header').evaluate((element) => (
        element.getBoundingClientRect().height
      )),
    ));
    // Whichever arrangement the band is in, its controls are all reachable.
    await expect(page.getByRole('button', { name: 'Color scheme' })).toBeVisible();
    await expect(page.locator('header.header').getByRole('button', { name: /Search/ })).toBeVisible();
  }

  expect(heights.get(flyoutViewport.width), 'the flyout band holds one height')
    .toBe(heights.get(799));
  expect(heights.get(800), 'the desktop band holds one height')
    .toBe(heights.get(1280));
  expect(heights.get(799), 'the two arrangements are drawn at different heights')
    .not.toBe(heights.get(800));
});

test('the band artwork fills the band without outgrowing its own detail', async ({ page }) => {
  await page.goto(base);
  // The bounds the artwork is drawn between are the artwork's own, so a
  // replacement asset is measured against itself rather than against numbers
  // left behind by the one it replaced.
  const artwork = await page.evaluate(async (assetBase) => {
    const image = new Image();
    image.src = `${assetBase}brand/picogrid-nav-texture.png`;
    await image.decode();
    return { width: image.naturalWidth, height: image.naturalHeight };
  }, base);

  for (const width of [flyoutViewport.width, 1280, 1920, 3440]) {
    await page.setViewportSize({ width, height: flyoutViewport.height });
    const band = await page.evaluate(() => {
      // The width the artwork is drawn at, measured as the band computes it:
      // a box given the same width against the same containing block.
      const probe = document.createElement('div');
      probe.style.cssText = 'position:fixed;inline-size:var(--pg-nav-texture-width);block-size:0';
      document.body.append(probe);
      const drawn = probe.getBoundingClientRect().width;
      probe.remove();
      return { drawn, height: document.querySelector('header.header')!.getBoundingClientRect().height };
    });

    // However wide the band, the artwork is still tall enough to fill it,
    // and never enlarged past the width its dot field reads as a texture at.
    expect((band.drawn * artwork.height) / artwork.width, `artwork height at ${width}px`)
      .toBeGreaterThanOrEqual(band.height);
    expect(band.drawn, `artwork width at ${width}px`)
      .toBeLessThanOrEqual(artwork.width * 1.5);
  }
});

// The band names a page as the sidebar names it, so the flyout finds the entry
// that is marked rather than a second name for the same page.
for (const [subject, route, pageName] of [
  ['guide page', routeUrl('reference/api'), 'Picogrid ECN SDK API'],
  ['landing page', base, 'Overview'],
]) {
  test(`the ${subject} flyout carries the band navigation on a phone`, async ({ page }) => {
    await page.setViewportSize(flyoutViewport);
    await page.goto(route);

    // The band's first row names the page being read and its second is search;
    // the guide itself waits in the flyout.
    await expect(page.locator('.current-page')).toHaveText(pageName);
    await expect(page.locator('header.header').getByRole('button', { name: /Search/ }))
      .toBeVisible();
    await expect(page.locator('.sidebar-tree')).toBeHidden();

    const menu = page.getByRole('button', { name: 'Menu' });
    await menu.click();
    await expect(menu).toHaveAttribute('aria-expanded', 'true');

    // The flyout opens on the guide, and carries what the band gave up below it.
    await expect(page.locator('.sidebar-tree')).toBeVisible();
    await expect(versionTrigger(page)).toBeVisible();
    await expect(page.getByRole('link', { name: 'Picogrid ECN SDK', exact: true }))
      .toBeVisible();

    const sets = page.getByRole('navigation', { name: 'Documentation' });
    const documentation = resolveLegionDocumentation();
    await expect(sets.getByRole('link', { name: documentation.label }))
      .toHaveAttribute('href', documentation.href);
    await expect(sets.getByRole('link', { name: 'ECN SDK' }))
      .toHaveAttribute('aria-current', 'true');

    // An open flyout owns the scroll; the page behind it stays where it was.
    await page.mouse.move(195, 600);
    await page.mouse.wheel(0, 600);
    expect(await page.evaluate(() => window.scrollY)).toBe(0);

    await expectAccessible(page);
  });
}

test('the flyout travels in from the start edge instead of appearing over the page', async ({
  page,
}) => {
  await page.setViewportSize(flyoutViewport);
  await page.goto(routeUrl('reference/api'));

  const pane = page.locator('.sidebar-pane');

  // Closed, the pane waits a full width off the edge it opens from, so none of
  // it is on the page.
  const closed = await pane.evaluate((element) => ({
    visibility: getComputedStyle(element).visibility,
    right: element.getBoundingClientRect().right,
  }));
  expect(closed.visibility).toBe('hidden');
  expect(closed.right).toBeLessThanOrEqual(0);

  await page.getByRole('button', { name: 'Menu' }).click();

  // It travels rather than arriving, and it is opaque for the whole of it: a
  // reader is never reading the page through the menu. The transition is short,
  // so poll for it: a single sample can land after a slow worker has finished
  // it and report no travel where there was no regression.
  await expect
    .poll(() => pane.evaluate((element) => element
      .getAnimations()
      .filter((animation): animation is CSSTransition => animation instanceof CSSTransition)
      .map((animation) => animation.transitionProperty)))
    .toContain('translate');
  expect(await pane.evaluate((element) => getComputedStyle(element).opacity)).toBe('1');

  await expect
    .poll(() => pane.evaluate((element) => Math.round(element.getBoundingClientRect().left)))
    .toBe(0);

  // Above the breakpoint the same pane is the sidebar, and it does not travel.
  await page.setViewportSize({ width: 1280, height: flyoutViewport.height });
  await page.goto(routeUrl('reference/api'));
  expect(await pane.evaluate((element) => getComputedStyle(element).translate)).toBe('none');
  await expect(pane).toBeVisible();
});
test('landing brand and read-only journey fit a mobile viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(base);

  await expect(page.getByRole('heading', { level: 1, name: 'Picogrid ECN SDK' })).toBeVisible();
  await expect(page.getByRole('main').getByRole('link', { name: 'Install the SDK', exact: true }))
    .toBeVisible();
  await expect(page.locator('.outcome')).toHaveCount(4);

  const viewport = await page.evaluate(() => ({
    documentWidth: document.documentElement.scrollWidth,
    viewportWidth: window.innerWidth,
  }));
  expect(viewport.documentWidth).toBeLessThanOrEqual(viewport.viewportWidth + 1);
});

test('the overview offers an outcome to start from before it explains itself', async ({ page }) => {
  await page.goto(base);

  // The outcomes stand between the one call to action and the prose.
  const cards = page.locator('.outcome');
  await expect(cards).toHaveCount(4);
  for (const [outcome, name, route] of [
    ['Observe tracks, detections, or locations', 'Observe ECN data', routeUrl('quickstarts/observe-data')],
    ['Connect a sensor', 'Build a sensor publisher', routeUrl('quickstarts/sensor-publisher')],
    ['Connect an effector', 'Build an effector task handler', routeUrl('quickstarts/effector-handler')],
    ['Run a tactical display', 'Run the operator view', routeUrl('quickstarts/operator-view')],
  ]) {
    const card = cards.filter({ hasText: outcome });
    await expect(card.getByRole('link', { name })).toHaveAttribute('href', route);
  }
  await expectAccessible(page);

  // The card is one target, so a click anywhere on it leads to the quickstart.
  await cards.filter({ hasText: 'Connect a sensor' }).click({ position: { x: 12, y: 12 } });
  await expect(page).toHaveURL(
    new RegExp(`${base.replaceAll('/', '\\/')}quickstarts\\/sensor-publisher\\/$`),
  );
});

test('the flyout tree reaches a sensor quickstart on a phone', async ({ page }) => {
  await page.setViewportSize(flyoutViewport);
  await page.goto(routeUrl('getting-started/configuration'));

  // The band names the page and the page says how it was reached; the guide
  // itself is behind the menu.
  await expect(page.locator('.current-page')).toHaveText('Configure a connection');
  const trail = page.getByRole('main').getByRole('navigation', { name: 'Breadcrumb' });
  await expect(trail.getByRole('link', { name: 'Overview' })).toBeVisible();
  await expect(trail.locator('[aria-current="page"]')).toHaveText('Configure a connection');

  await page.getByRole('button', { name: 'Menu' }).click();
  const tree = page.locator('.sidebar-tree');
  await expect(tree.getByRole('link', { name: 'Configure a connection' }))
    .toHaveAttribute('aria-current', 'page');
  await expectAccessible(page);

  await tree.getByRole('link', { name: 'Build a sensor publisher' }).click();
  await expect(page).toHaveURL(
    new RegExp(`${base.replaceAll('/', '\\/')}quickstarts\\/sensor-publisher\\/$`),
  );
  await expect(page.getByRole('heading', { level: 1, name: 'Build a sensor publisher' }))
    .toBeVisible();
});

test('the trail on a wide viewport leads back through the guide', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(base);
  await expect(
    page.getByRole('main').getByRole('navigation', { name: 'Breadcrumb' }),
  ).toHaveCount(0);

  await page.goto(routeUrl('getting-started/configuration'));

  // The trail belongs to the page, so it is in the same place at every width.
  const trail = page.getByRole('main').getByRole('navigation', { name: 'Breadcrumb' });
  await expect(trail).toContainText('Install and authenticate');
  await expect(trail.locator('[aria-current="page"]')).toHaveText('Configure a connection');

  await trail.getByRole('link', { name: 'Overview' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Picogrid ECN SDK' })).toBeVisible();
});

test('a page on a phone opens with its trail rather than an outline', async ({ page }) => {
  await page.setViewportSize(flyoutViewport);
  // A page whose outline the theme would otherwise collapse into the panel.
  await page.goto(routeUrl('getting-started/preflight'));

  await expect(page.getByRole('group', { name: /on this page/i })).toHaveCount(0);
  await expect(page.locator('#starlight__mobile-toc')).toHaveCount(0);

  // What the panel occupied is now the page: the trail, then the title.
  const trail = page.getByRole('main').getByRole('navigation', { name: 'Breadcrumb' });
  const heading = page.getByRole('heading', { level: 1 });
  await expect(trail).toBeVisible();
  await expect(trail.locator('[aria-current="page"]')).toHaveText('Run read-only preflight');
  const [trailBox, headingBox] = await Promise.all([trail.boundingBox(), heading.boundingBox()]);
  expect(trailBox && headingBox && trailBox.y < headingBox.y).toBe(true);

  // The outline the panel summarised is still a column of its own further up.
  await page.setViewportSize({ width: 1280, height: 900 });
  await expect(page.locator('starlight-toc')).toBeVisible();
});

test('keyboard-only navigation reaches the installation journey', async ({ page }) => {
  await page.goto(base);
  // The overview is a page of the guide, so the sidebar names this route as
  // well; the journey under test is the one the overview itself offers.
  const install = page
    .getByRole('main')
    .getByRole('link', { name: 'Install the SDK', exact: true });
  await tabTo(page, install, 80);
  await expect(install).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(install).not.toBeFocused();
  await page.keyboard.press('Tab');
  await expect(install).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(
    new RegExp(`${base.replaceAll('/', '\\/')}getting-started\\/installation\\/$`),
  );
});

test('the guide ships one mono face from its own origin and reads in the system face', async ({
  page,
}) => {
  const origins = new Set<string>();
  page.on('request', (request) => origins.add(new URL(request.url()).origin));

  await page.goto(routeUrl('reference/api'));
  await page.evaluate(async () => {
    await document.fonts.ready;
  });

  // Nothing is fetched from a font host: the face is served with the guide.
  const site = new URL(page.url()).origin;
  expect([...origins]).toEqual([site]);
  const faces = await page.evaluate(() =>
    [...document.fonts].filter((face) => face.status === 'loaded').map((face) => face.family),
  );
  expect(faces).toContain('Chivo Mono Variable');

  const expectSystemFace = async (selectors: { prose: string; navigation: string }) => {
    const families = await page.evaluate(({ prose, navigation }) => {
      const first = (selector: string) => {
        const element = document.querySelector(selector);
        if (!element) throw new Error(`expected typography fixture is missing: ${selector}`);
        return getComputedStyle(element).fontFamily.split(',')[0]!.trim().replace(/^"|"$/g, '');
      };
      return {
        prose: first(prose),
        navigation: first(navigation),
      };
    }, selectors);

    expect(families.prose).toBe('ui-sans-serif');
    expect(families.navigation).toBe('ui-sans-serif');
  };

  await expectSystemFace({
    prose: '.sl-markdown-content p',
    navigation: '.sidebar-content a',
  });
  const codeFamily = await page.locator('.sl-markdown-content code').first().evaluate((element) =>
    getComputedStyle(element).fontFamily.split(',')[0]!.trim().replace(/^"|"$/g, ''),
  );
  expect(codeFamily).toBe('Chivo Mono Variable');

  await page.goto(`${base}does-not-exist`);
  await expectSystemFace({ prose: '.summary', navigation: 'nav a' });

  await page.evaluate(async () => {
    await document.fonts.ready;
  });
  const standaloneMonoFamilies = await page.locator('.brand span, .eyebrow, h1').evaluateAll(
    (elements) => elements.map((element) =>
      getComputedStyle(element).fontFamily.split(',')[0]!.trim().replace(/^"|"$/g, '')
    ),
  );
  expect(standaloneMonoFamilies).toEqual([
    'Chivo Mono Variable',
    'Chivo Mono Variable',
    'Chivo Mono Variable',
  ]);
  const standaloneFaces = await page.evaluate(() =>
    [...document.fonts].filter((face) => face.status === 'loaded').map((face) => face.family),
  );
  expect(standaloneFaces).toContain('Chivo Mono Variable');
});

test('reduced-motion preference suppresses documentation animation and transitions', async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await page.goto(base);
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

test('public navigation presents product journeys without maintainer routes', async ({ page }) => {
  await page.goto(`${base}getting-started/configuration/`);
  const navigation = page.getByRole('navigation', { name: 'Main' });

  for (const name of [
    'Build a sensor publisher',
    'Build an effector task handler',
    'Run the operator view',
    'MQTT topics and delivery',
  ]) {
    await expect(navigation.getByRole('link', { name })).toBeVisible();
  }

  await expect(
    navigation.getByRole('link', { name: 'Picogrid ECN SDK guide' }),
  ).toHaveCount(0);
});

test('local search reaches broker authorization guidance', async ({ page }) => {
  await page.goto(base);
  await page.getByRole('button', { name: 'Search' }).first().click();
  const search = page.getByRole('dialog', { name: 'Search' });
  await expect(search).toBeVisible();
  await search.locator('input').fill('broker ACLs');
  const result = search.getByRole('link', { name: /Broker ACLs/i }).first();
  await expect(result).toBeVisible();
  await result.click();
  await expect(page).toHaveURL(
    new RegExp(`${base.replaceAll('/', '\\/')}concepts\\/acls\\/$`),
  );
});

test('branded 404 offers safe recovery routes', async ({ page }) => {
  const response = await page.goto(`${base}does-not-exist`);
  expect(response?.status()).toBe(404);
  await expect(page.getByRole('heading', { level: 1, name: 'Page not found' })).toBeVisible();
  await expect(page.locator('link[rel~="icon"]')).toHaveAttribute(
    'href',
    brandIconHref,
  );
  await expect(page.getByRole('link', { name: /Picogrid ECN SDK/i })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Return to the guide' })).toHaveAttribute('href', base);
  await expect(page.getByRole('link', { name: 'Observe ECN data' })).toHaveAttribute(
    'href',
    `${base}quickstarts/observe-data/`,
  );
  await expectAccessible(page);
});

test('dark-selected pages and primary action produce readable light print output', async ({ page }) => {
  await page.goto(`${base}reference/configuration/`);
  await page.locator('html').evaluate((element) => element.setAttribute('data-theme', 'dark'));
  await page.emulateMedia({ media: 'print' });

  const styles = await page.evaluate(() => {
    const table = document.querySelector('table');
    const sidebar = document.querySelector('nav.sidebar');
    if (!table || !sidebar) throw new Error('expected print fixtures are missing');
    return {
      background: getComputedStyle(document.body).backgroundColor,
      color: getComputedStyle(document.querySelector('main')!).color,
      sidebarDisplay: getComputedStyle(sidebar).display,
      tableDisplay: getComputedStyle(table).display,
      tableOverflow: getComputedStyle(table).overflowX,
    };
  });

  expect(styles).toEqual({
    background: 'rgb(255, 255, 255)',
    color: 'rgb(0, 0, 0)',
    sidebarDisplay: 'none',
    tableDisplay: 'table',
    tableOverflow: 'visible',
  });

  // Default print settings suppress CSS background graphics. The brand mark
  // must therefore remain foreground content with a rendered box.
  await page.addStyleTag({
    content: '@media print { *, *::before, *::after { background-image: none !important; } }',
  });
  const printWordmark = page.locator(
    `.site-title img[src="${base}brand/picogrid-wordmark-light.png"]`,
  );
  await expect(printWordmark).toBeVisible();
  const printWordmarkBox = await printWordmark.boundingBox();
  expect(printWordmarkBox?.width).toBeGreaterThan(0);
  expect(printWordmarkBox?.height).toBeGreaterThan(0);

  await page.goto(base);
  await page.locator('html').evaluate((element) => element.setAttribute('data-theme', 'dark'));
  const primaryActionLink = page.locator('.sl-link-button.primary');
  await primaryActionLink.hover();
  const primaryAction = await primaryActionLink.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      backgroundColor: style.backgroundColor,
      borderColor: style.borderColor,
      color: style.color,
    };
  });

  expect(primaryAction).toEqual({
    backgroundColor: 'rgb(255, 255, 255)',
    borderColor: 'rgb(0, 0, 0)',
    color: 'rgb(0, 0, 0)',
  });
});

const tableExamples = [
  { label: 'exception reference', route: `${base}reference/exceptions/` },
  { label: 'runnable examples', route: `${base}shipped-tooling/` },
  { label: 'configuration reference', route: `${base}reference/configuration/` },
  { label: 'generated location fields', route: `${base}reference/python/locations/location/` },
];

test('local search reaches the generated task acknowledgement page', async ({ page }) => {
  await page.goto(base);
  await page.getByRole('button', { name: 'Search' }).first().click();
  const search = page.getByRole('dialog', { name: 'Search' });
  await expect(search).toBeVisible();
  await search.locator('input').fill('TaskAcknowledgement');
  const result = search.getByRole('link', { name: /TaskAcknowledgement/i }).first();
  await expect(result).toBeVisible();
  await result.click();
  await expect(page).toHaveURL(
    new RegExp(`${base.replaceAll('/', '\\/')}reference\\/python\\/tasks\\/task-acknowledgement\\/$`),
  );
});

test('generated client page anchors deep-link to member sections', async ({ page }) => {
  await page.goto(`${base}reference/python/client/ecn-client/#preflight`);
  const heading = page.locator('h2#preflight');
  await expect(heading).toBeVisible();
  await expect(page).toHaveURL(/#preflight$/);
  const geometry = await heading.evaluate((element) => {
    const box = element.getBoundingClientRect();
    return {
      blockEnd: box.bottom,
      blockStart: box.top,
      viewportHeight: window.innerHeight,
    };
  });
  expect(geometry.blockEnd).toBeGreaterThan(0);
  expect(geometry.blockStart).toBeLessThan(geometry.viewportHeight);
  await expect(heading).toContainText('preflight');
});

test('generated signatures stay inside the mobile content column', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${base}reference/python/tasks/tasks/`);
  const signatures = page.locator('.sl-markdown-content pre');
  await expect(signatures.first()).toBeVisible();
  const geometry = await signatures.evaluateAll((elements) => {
    const widest = elements.reduce((current, candidate) =>
      candidate.scrollWidth > current.scrollWidth ? candidate : current,
    );
    const box = widest.getBoundingClientRect();
    return {
      inlineEnd: box.right,
      inlineStart: box.left,
      scrollWidth: widest.scrollWidth,
      viewportWidth: window.innerWidth,
    };
  });
  expect(geometry.scrollWidth).toBeGreaterThan(0);
  expect(geometry.inlineStart).toBeGreaterThanOrEqual(0);
  expect(geometry.inlineEnd).toBeLessThanOrEqual(geometry.viewportWidth + 1);
});

for (const example of tableExamples) {
  test(`${example.label} table fills the desktop content column`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(example.route);

    const content = page.locator('.sl-markdown-content');
    const table = content.locator('table').first();
    await expect(table).toBeVisible();
    const contentWidth = await content.evaluate(
      (element) => element.getBoundingClientRect().width,
    );
    const geometry = await table.evaluate((element) => {
      const tableBox = element.getBoundingClientRect();
      const lastCellBox = element.querySelector('tbody tr:first-child td:last-child')
        ?.getBoundingClientRect();
      return {
        emptyInlineSpace: lastCellBox ? tableBox.right - lastCellBox.right : Number.POSITIVE_INFINITY,
        tableClientWidth: element.clientWidth,
        tableScrollWidth: element.scrollWidth,
        tableWidth: tableBox.width,
      };
    });

    expect(geometry.tableWidth).toBeGreaterThanOrEqual(contentWidth - 1);
    expect(geometry.emptyInlineSpace).toBeLessThanOrEqual(2);
    expect(geometry.tableScrollWidth).toBeLessThanOrEqual(geometry.tableClientWidth + 1);
  });

  test(`${example.label} table scrolls inside the mobile content column`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(example.route);

    const table = page.locator('.sl-markdown-content table').first();
    await expect(table).toBeVisible();
    const geometry = await table.evaluate((element) => {
      const box = element.getBoundingClientRect();
      return {
        clientWidth: element.clientWidth,
        documentWidth: document.documentElement.scrollWidth,
        inlineEnd: box.right,
        inlineStart: box.left,
        overflowX: getComputedStyle(element).overflowX,
        scrollWidth: element.scrollWidth,
        viewportWidth: window.innerWidth,
      };
    });

    expect(geometry.overflowX).toBe('auto');
    expect(geometry.scrollWidth).toBeGreaterThan(geometry.clientWidth);
    expect(geometry.inlineStart).toBeGreaterThanOrEqual(0);
    expect(geometry.inlineEnd).toBeLessThanOrEqual(geometry.viewportWidth + 1);
    expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewportWidth + 1);

    const scrollLeft = await table.evaluate((element) => {
      element.scrollLeft = element.scrollWidth;
      return element.scrollLeft;
    });
    expect(scrollLeft).toBeGreaterThan(0);
  });
}

const apiManifest = JSON.parse(
  readFileSync(new URL('../../../scripts/public-api-manifest.json', import.meta.url), 'utf8'),
) as {
  groups: { id: string; title: string; route: string }[];
  symbols: { group: string; route: string }[];
  testing_symbols: { group: string; route: string }[];
};

function pythonReferenceNav(page: Page): Locator {
  // `:scope >` keeps this on the Python group itself: the enclosing
  // `API reference` element also contains that summary as a descendant.
  return page.locator('#starlight__sidebar details').filter({
    has: page.locator(':scope > summary span.large', { hasText: /^Python$/ }),
  });
}

test('python reference navigation is flat and free of duplicate overviews', async ({ page }) => {
  await page.goto(routeUrl('reference/python/entities'));
  const nav = pythonReferenceNav(page);
  await expect(nav).toHaveCount(1);

  // Two meaningful levels below the API reference root: the Python group, then
  // one entry per generated group. A nested <details> would be a third.
  await expect(nav.locator('details')).toHaveCount(0);

  // Exactly one Overview, for the reference index. The duplication this
  // replaces was an Overview under Python *and* one under all eleven groups.
  const overview = nav.getByText('Overview', { exact: true });
  await expect(overview).toHaveCount(1);
  await expect(nav.getByRole('link', { name: 'Overview', exact: true })).toHaveAttribute(
    'href',
    routeUrl('reference/python'),
  );

  const hrefs = await nav.locator('a').evaluateAll((links) =>
    links.map((link) => new URL((link as HTMLAnchorElement).href).pathname),
  );
  expect(hrefs).toEqual([
    routeUrl('reference/python'),
    ...apiManifest.groups.map((group) => routeUrl(group.route)),
  ]);
});

test('every generated group heading links to its own index page', async ({ page }) => {
  await page.goto(routeUrl('reference/python/entities'));
  const nav = pythonReferenceNav(page);
  for (const group of apiManifest.groups) {
    await expect(nav.getByRole('link', { name: group.title, exact: true })).toHaveAttribute(
      'href',
      routeUrl(group.route),
    );
  }
});

test('leaf symbol pages stay out of the sidebar but remain reachable', async ({ page }) => {
  await page.goto(routeUrl('reference/python/entities'));
  const nav = pythonReferenceNav(page);
  const groupRoutes = new Set(apiManifest.groups.map((group) => group.route));
  const leafRoutes = [...apiManifest.symbols, ...apiManifest.testing_symbols]
    .map((symbol) => symbol.route)
    .filter((route) => !groupRoutes.has(route));

  await expect(nav.locator(`a[href="${routeUrl(leafRoutes[0])}"]`)).toHaveCount(0);

  // The group index is the inventory for its leaves, so each one is one click away.
  const entityLeaves = leafRoutes.filter((route) => route.startsWith('reference/python/entities/'));
  for (const route of entityLeaves) {
    await expect(page.locator(`main a[href="${routeUrl(route)}"]`).first()).toBeVisible();
  }
});

test('selecting a group navigates to its index and marks the entry current', async ({ page }) => {
  await page.goto(routeUrl('reference/python/client'));
  const nav = pythonReferenceNav(page);
  await nav.getByRole('link', { name: 'Locations', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${routeUrl('reference/python/locations')}$`));
  await expect(
    pythonReferenceNav(page).getByRole('link', { name: 'Locations', exact: true }),
  ).toHaveAttribute('aria-current', 'page');
});

test('python reference groups are reachable from the mobile menu', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(routeUrl('reference/python/entities'));
  await page.getByRole('button', { name: 'Menu' }).click();
  const nav = pythonReferenceNav(page);
  await expect(nav.getByRole('link', { name: 'Tasks', exact: true })).toBeVisible();
  await expect(nav.locator('details')).toHaveCount(0);
  await expectAccessible(page);
});
