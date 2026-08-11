// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import { readFileSync } from 'node:fs';

const apiManifest = JSON.parse(
  readFileSync(new URL('../../scripts/public-api-manifest.json', import.meta.url), 'utf8'),
);

// The reference index and one entry per group are the navigable surface. Leaf
// symbol pages are reached from those group indexes, search, cross-links, and
// their stable routes, so they are deliberately absent from the sidebar.
export const pythonSidebarRoutes = [
  'reference/python',
  ...apiManifest.groups.map((group) => group.route),
];

export const pythonLeafRoutes = [...new Set(
  [...apiManifest.symbols, ...apiManifest.testing_symbols].map((symbol) => symbol.route),
)].filter((route) => !pythonSidebarRoutes.includes(route));

export const pythonReferenceRoutes = [...pythonSidebarRoutes, ...pythonLeafRoutes];

export const publicGuideRoutes = [
  '',
  'changelog',
  'compatibility/limitations',
  'concepts/acls',
  'concepts/ecns',
  'concepts/entities',
  'concepts/lifecycle',
  'concepts/locations',
  'concepts/mesh-routing',
  'concepts/mqtt-wire',
  'concepts/security',
  'concepts/tasks',
  'concepts/uuids',
  'deployment-support',
  'getting-started/authentication',
  'getting-started/configuration',
  'getting-started/installation',
  'getting-started/mock-setup',
  'getting-started/preflight',
  'how-to/check-clock',
  'how-to/cleanup',
  'how-to/dispatch-local-tasks',
  'how-to/dispatch-mesh-tasks',
  'how-to/observe-location',
  'how-to/observe-mesh-data',
  'how-to/pli-entities',
  'how-to/protobuf-decode',
  'how-to/receive-local-tasks',
  'how-to/tracks-detections',
  'how-to/troubleshooting',
  'integrations/effectors',
  'integrations/sensors',
  'operator/application',
  'operator/workflows',
  'quickstarts/effector-handler',
  'quickstarts/observe-data',
  'quickstarts/operator-view',
  'quickstarts/sensor-publisher',
  'reference/api',
  'reference/configuration',
  'reference/exceptions',
  'reference/licensing',
  'reference/wire-formats',
  ...pythonReferenceRoutes,
  'security/credentials',
  'shipped-tooling',
  'walkthroughs/tactical-live-map',
  'walkthroughs/task-handler-service',
  'walkthroughs/track-viewer',
];

export const maintainerOnlyDocumentationRoutes = [
  'readme',
  'reference/evidence-status',
  'reference/original-ecn-integration-parity',
];
