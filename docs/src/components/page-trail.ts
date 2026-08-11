// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * Where a page sits in the guide, read out of the theme's own sidebar.
 *
 * Two places ask this question and must agree: the band, which names the page
 * being read, and the trail on the page, which names the path that reached it.
 * Both are answered from the sidebar the theme computes for the route, so
 * neither can drift from the navigation a reader sees beside them.
 */

export interface SidebarLink {
  type: 'link';
  label: string;
  href: string;
  isCurrent: boolean;
}

export interface SidebarGroup {
  type: 'group';
  label: string;
  entries: SidebarEntry[];
}

export type SidebarEntry = SidebarLink | SidebarGroup;

export interface Crumb {
  label: string;
  href?: string;
}

/** The headings standing above the page being read, and then the page itself. */
function walk(entries: SidebarEntry[], above: Crumb[]): Crumb[] | null {
  for (const entry of entries) {
    if (entry.type === 'link') {
      if (entry.isCurrent) return [...above, { label: entry.label, href: entry.href }];
      continue;
    }
    const found = walk(entry.entries, [...above, { label: entry.label }]);
    if (found) return found;
  }
  return null;
}

/**
 * The path to the page being read, from the front page of the guide down. Only
 * the front page and the page itself are pages; the headings between them are
 * groupings the sidebar makes, so they are named without being offered.
 */
export function pageTrail(
  sidebar: readonly SidebarEntry[],
  frontHref: string,
  fallbackLabel: string,
): Crumb[] {
  const entries = sidebar as SidebarEntry[];
  const path = walk(entries, []) ?? [{ label: fallbackLabel }];
  const front = entries.find(
    (entry): entry is SidebarLink => entry.type === 'link' && entry.href === frontHref,
  );
  return front && path[0]?.href !== front.href
    ? [{ label: front.label, href: front.href }, ...path]
    : path;
}
