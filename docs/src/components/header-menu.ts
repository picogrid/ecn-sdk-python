// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * Behaviour shared by the header's menus.
 *
 * The version menu and the colour-scheme menu are each a button that reveals a
 * panel of menu items, so they close on Escape, on a click outside, and when
 * focus leaves the menu entirely. Keeping one implementation means the controls
 * cannot drift apart in how they respond to a keyboard.
 *
 * Both panels declare `role="menu"`, which is a promise to a screen reader that
 * the arrow keys move between the items and that Tab leaves rather than steps
 * through them. That promise is kept here rather than in either component: the
 * menu is one tab stop, opening it puts focus on the item that is already
 * marked, and the arrows, Home, and End move within it.
 */
const itemSelector = '[role="menuitem"], [role="menuitemcheckbox"], [role="menuitemradio"]';

let panelSequence = 0;

export function attachHeaderMenu(host: HTMLElement): { close: () => void } {
  const noop = { close: () => {} };
  const trigger = host.querySelector('button[aria-expanded]');
  const panel = host.querySelector<HTMLElement>('[data-menu-panel]');
  if (!(trigger instanceof HTMLButtonElement) || !panel) return noop;

  // The trigger names the panel it reveals, so the two are one control rather
  // than a button and an unrelated region that happens to appear beneath it.
  if (!panel.id) {
    panelSequence += 1;
    panel.id = `${host.localName}-panel-${panelSequence}`;
  }
  trigger.setAttribute('aria-controls', panel.id);

  const items = (): HTMLElement[] => [...panel.querySelectorAll<HTMLElement>(itemSelector)];

  /**
   * Move focus to one item and make it the menu's only tab stop, so the menu
   * is entered and left once rather than once per item.
   */
  const focusItem = (index: number): void => {
    const all = items();
    if (all.length === 0) return;
    const target = all[((index % all.length) + all.length) % all.length]!;
    for (const item of all) item.tabIndex = item === target ? 0 : -1;
    target.focus();
  };

  /** The item a reader is already on: the checked scheme, or the release in use. */
  const markedIndex = (): number => {
    const all = items();
    const marked = all.findIndex(
      (item) => item.getAttribute('aria-checked') === 'true' || item.hasAttribute('aria-current'),
    );
    return marked === -1 ? 0 : marked;
  };

  const focusedIndex = (): number => items().findIndex((item) => item === document.activeElement);

  const setExpanded = (expanded: boolean): void => {
    trigger.setAttribute('aria-expanded', String(expanded));
    panel.hidden = !expanded;
    if (!expanded) for (const item of items()) item.tabIndex = -1;
  };

  const open = (focus: 'marked' | 'last' | 'none'): void => {
    setExpanded(true);
    if (focus === 'marked') focusItem(markedIndex());
    else if (focus === 'last') focusItem(items().length - 1);
  };

  const close = ({ restoreFocus }: { restoreFocus: boolean }): void => {
    if (trigger.getAttribute('aria-expanded') !== 'true') return;
    setExpanded(false);
    if (restoreFocus) trigger.focus();
  };

  trigger.addEventListener('click', () => {
    if (trigger.getAttribute('aria-expanded') === 'true') close({ restoreFocus: false });
    else open('marked');
  });

  trigger.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      open('marked');
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      open('last');
    } else if (event.key === 'Tab' && trigger.getAttribute('aria-expanded') === 'true') {
      close({ restoreFocus: false });
    }
  });

  panel.addEventListener('keydown', (event) => {
    switch (event.key) {
      case ' ': {
        const target = event.target;
        if (target instanceof HTMLAnchorElement && target.matches(itemSelector)) {
          event.preventDefault();
          if (!event.repeat) target.click();
        }
        break;
      }
      case 'ArrowDown':
        event.preventDefault();
        focusItem(focusedIndex() + 1);
        break;
      case 'ArrowUp':
        event.preventDefault();
        focusItem(focusedIndex() - 1);
        break;
      case 'Home':
        event.preventDefault();
        focusItem(0);
        break;
      case 'End':
        event.preventDefault();
        focusItem(items().length - 1);
        break;
      case 'Tab':
        // Tab leaves the whole menu, so it closes and hands the trigger back
        // for the browser's own move to the next control on the band.
        close({ restoreFocus: true });
        break;
      default:
        break;
    }
  });

  host.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && trigger.getAttribute('aria-expanded') === 'true') {
      event.preventDefault();
      close({ restoreFocus: true });
    }
  });

  host.addEventListener('focusout', (event) => {
    const next = event.relatedTarget;
    if (next instanceof Node && host.contains(next)) return;
    close({ restoreFocus: false });
  });

  document.addEventListener('click', (event) => {
    const target = event.target;
    if (target instanceof Node && host.contains(target)) return;
    close({ restoreFocus: false });
  });

  setExpanded(false);
  return { close: () => close({ restoreFocus: true }) };
}
