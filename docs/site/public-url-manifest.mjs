// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/** Public pathnames under /ecn-sdk/ that must exist in the built site. */
import { publicGuideRoutes } from './public-routes.mjs';
import { documentationBasePath } from './site-config.mjs';

export function publicPathnameForRoute(route) {
  if (!route) return `${documentationBasePath}/`;
  return `${documentationBasePath}/${route}/`;
}

export const publicGuidePathnames = publicGuideRoutes.map(publicPathnameForRoute);

/**
 * High-signal static assets and recovery surface used by bookmarks, OG, and
 * browser chrome. Keep this list intentionally small; full asset coverage is
 * already exercised by `check-built-site.mjs`.
 */
export const publicStaticPathnames = [
  `${documentationBasePath}/brand/picogrid-app-icon-192.png`,
  `${documentationBasePath}/brand/ecn-client-og.png`,
  `${documentationBasePath}/404.html`,
];

/**
 * Representative product journeys called out explicitly so the manifest still
 * asserts a few well-known URLs even if the full route list drifts temporarily.
 */
export const knownJourneyPathnames = [
  `${documentationBasePath}/`,
  `${documentationBasePath}/getting-started/installation/`,
  `${documentationBasePath}/quickstarts/observe-data/`,
  `${documentationBasePath}/reference/api/`,
];

/** Full ordered unique list of public pathnames under `/ecn-sdk/`. */
export const publicUrlPathnames = [
  ...new Set([
    ...publicGuidePathnames,
    ...knownJourneyPathnames,
    ...publicStaticPathnames,
  ]),
];

// HTTP gate for this list: site/url-compatibility.mjs (A7). Offline disk gate:
// site/check-deploy-contract.mjs (A3). Intentional removals are declared in
// publicRedirects inside url-compatibility.mjs — never silent 404s.
