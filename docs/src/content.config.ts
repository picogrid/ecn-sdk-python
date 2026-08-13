// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import { defineCollection } from 'astro:content';
import { glob } from 'astro/loaders';
import { docsSchema } from '@astrojs/starlight/schema';

export const collections = {
  docs: defineCollection({
    loader: glob({
      base: '.',
      // Allowlist content roots so workspace tooling and node_modules are never traversed.
      // The site checker discovers documentation independently and reports uncovered sources.
      pattern: [
        '{index,changelog,deployment-support,shipped-tooling}.{md,mdx}',
        '{compatibility,concepts,getting-started,how-to,integrations,operator,quickstarts,reference,security,walkthroughs}/**/*.{md,mdx}',
      ],
    }),
    schema: docsSchema(),
  }),
};
