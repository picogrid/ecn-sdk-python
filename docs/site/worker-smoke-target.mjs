// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * Resolve which origin the production deploy should smoke.
 *
 * The answer is already in `wrangler.jsonc`, so it is derived rather than
 * carried in a separate switch an operator has to keep in step with a cutover:
 *
 * - `workers_dev` enabled: the Worker still owns a bootstrap origin, which is
 *   what a deploy should verify while the canonical host may still be served by
 *   another documentation product.
 * - `workers_dev` disabled: the canonical host routes are the only way in, so
 *   that is what a deploy must verify.
 *
 * An unresolved target fails closed. Smoking the wrong origin reports success
 * for a Worker nobody is reaching.
 */
import { readFile } from 'node:fs/promises';

import { parseJsonc } from './jsonc.mjs';
import { documentationOrigin } from './site-config.mjs';

/** Wrangler announces the deployed triggers, then indents each one beneath it. */
const TRIGGER_HEADING = /^Deployed\s+\S+\s+triggers\b/mu;
const INDENTED_ENTRY = /^\s+\S/u;

/**
 * Wrangler prints unrelated links — its telemetry notice, documentation links —
 * so a candidate must satisfy two independent conditions: it appears in the
 * indented trigger listing, and its first hostname label is the Worker name.
 * Position alone would accept an unrelated host that happens to be listed; the
 * label alone would accept a same-label link printed anywhere in the output,
 * including after the listing ends. Naming the bootstrap suffix here is
 * deliberately avoided: it is not an approved publication hostname, so it
 * cannot appear in this file.
 */
export function bootstrapOriginFrom(deployLog, workerName) {
  const heading = deployLog.search(TRIGGER_HEADING);
  if (heading === -1) {
    throw new Error('Wrangler output did not announce any deployed triggers');
  }
  // The listing is the contiguous indented block; the first line at column zero
  // ends it, so later output such as `Current Version ID:` is not scanned.
  const listing = [];
  for (const line of deployLog.slice(heading).split('\n').slice(1)) {
    if (!INDENTED_ENTRY.test(line)) break;
    listing.push(line);
  }
  const matches = listing.join('\n').match(/https:\/\/[^\s"'`<>)\]]+/gu) ?? [];
  const origins = [...new Set(
    matches
      .map((candidate) => {
        try {
          return new URL(candidate);
        } catch {
          return null;
        }
      })
      .filter((url) => url?.hostname.split('.')[0] === workerName)
      .map((url) => url.origin),
  )];
  if (origins.length !== 1) {
    throw new Error(
      `expected exactly one deployed ${workerName} origin in the Wrangler output, found ${origins.length}`,
    );
  }
  return origins[0];
}

export function smokeTarget({ wrangler, deployLog }) {
  if (wrangler?.workers_dev === true) {
    return bootstrapOriginFrom(deployLog, wrangler.name);
  }
  return documentationOrigin;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const [logPath] = process.argv.slice(2);
  const wrangler = parseJsonc(await readFile(new URL('../wrangler.jsonc', import.meta.url), 'utf8'));
  const deployLog = logPath ? await readFile(logPath, 'utf8') : '';
  process.stdout.write(smokeTarget({ wrangler, deployLog }));
}
