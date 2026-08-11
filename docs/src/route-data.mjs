// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * Starlight route middleware: keep authored frontmatter mount-neutral.
 *
 * Hero action links are authored as site-absolute routes without the
 * documentation mount (for example `/quickstarts/observe-data/`). The mount is
 * deployment configuration (Astro `base`), so it is applied here at build time
 * instead of being hardcoded in content.
 */
import { defineRouteMiddleware } from '@astrojs/starlight/route-data';

const base = import.meta.env.BASE_URL.endsWith('/')
  ? import.meta.env.BASE_URL
  : `${import.meta.env.BASE_URL}/`;

export const onRequest = defineRouteMiddleware((context) => {
  const { hero } = context.locals.starlightRoute.entry.data;
  for (const action of hero?.actions ?? []) {
    if (
      action.link?.startsWith('/')
      && !action.link.startsWith('//')
      && !action.link.startsWith(base)
    ) {
      action.link = `${base}${action.link.slice(1)}`;
    }
  }
});
