// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * Page behaviour that is stated visually and, without this, stated no other way.
 *
 * Two things on a documentation page are announced to nobody. Expressive Code
 * turns a code block that overflows into a focusable `region` and gives it no
 * name, so a reader who tabs into one is told only that they are in a region;
 * and Pagefind writes how many results a search found into the dialog without
 * marking it as something that changed, so a reader typing a query hears
 * nothing at all.
 */

/**
 * The name every code block on the page would carry, decided once.
 *
 * Which blocks overflow depends on the viewport, so the role comes and goes as
 * a reader resizes. Deciding the names up front, over every block rather than
 * over the ones currently marked, keeps a block's name the same each time it
 * reappears instead of renumbering the page around the reader.
 */
function codeRegionNames(blocks: HTMLElement[]): Map<HTMLElement, string> {
  const anonymous = new Set(['', 'text', 'plaintext', 'plain']);
  const counts = new Map<string, number>();
  const names = new Map<HTMLElement, string>();

  for (const block of blocks) {
    const title = block.parentElement?.querySelector('figcaption')?.textContent?.trim();
    const language = block.dataset.language ?? '';
    const base = title || (anonymous.has(language) ? 'Code' : `${language} code`);
    const occurrence = (counts.get(base) ?? 0) + 1;
    counts.set(base, occurrence);
    // A landmark list is only worth reading if two entries in it can be told
    // apart, so a repeated name is numbered by where it falls on the page.
    names.set(block, occurrence === 1 ? base : `${base} ${occurrence}`);
  }
  return names;
}

function nameCodeRegions(): void {
  const blocks = [
    ...document.querySelectorAll<HTMLElement>('.sl-markdown-content pre[data-language]'),
  ];
  const names = codeRegionNames(blocks);

  for (const block of blocks) {
    const name = names.get(block)!;
    // The name belongs to the region Expressive Code makes of the block, not to
    // the plain `pre` it is the rest of the time, which may not carry a label.
    const synchronize = () => {
      if (block.getAttribute('role') === 'region') block.setAttribute('aria-label', name);
      else block.removeAttribute('aria-label');
    };
    new MutationObserver(synchronize).observe(block, {
      attributes: true,
      attributeFilter: ['role'],
    });
    synchronize();
  }
}

function announceSearchResults(): void {
  const search = document.getElementById('starlight__search');
  if (!search) return;

  // The count is announced from beside the results rather than by marking
  // Pagefind's own line, which is replaced wholesale on every keystroke: a live
  // region that is removed and rebuilt is not one a screen reader is watching.
  const status = document.createElement('p');
  status.className = 'sr-only';
  status.setAttribute('role', 'status');
  search.after(status);

  let announced = '';
  const report = () => {
    const message = search.querySelector('.pagefind-ui__message')?.textContent?.trim() ?? '';
    if (message === announced) return;
    announced = message;
    status.textContent = message;
  };
  new MutationObserver(report).observe(search, {
    characterData: true,
    childList: true,
    subtree: true,
  });
  report();
}

nameCodeRegions();
announceSearchResults();
