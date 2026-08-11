// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/** CLI: crawl ReadMe for /ecn-sdk links and validate them on a candidate base. */
import {
  runReadmeEcnCrawler,
} from './readme-ecn-crawler.mjs';
import { documentationOrigin } from './site-config.mjs';

function readFlag(name) {
  const prefix = `--${name}=`;
  const hit = process.argv.slice(2).find((argument) => argument.startsWith(prefix));
  return hit ? hit.slice(prefix.length) : undefined;
}

function hasFlag(name) {
  return process.argv.slice(2).includes(`--${name}`);
}

const candidateBaseUrl =
  readFlag('base-url')
  ?? process.env.PREVIEW_BASE_URL
  ?? process.env.CANDIDATE_BASE_URL;

const sourceBaseUrl =
  readFlag('source-base-url')
  ?? process.env.README_SOURCE_BASE_URL
  ?? documentationOrigin;

const maxPagesValue = readFlag('max-pages') ?? process.env.README_CRAWL_MAX_PAGES;
const requireEcnLinks = hasFlag('require-ecn-links')
  || process.env.README_REQUIRE_ECN_LINKS === '1';

const crawlerOptions = {
  sourceBaseUrl,
  candidateBaseUrl,
  requireEcnLinks,
};
if (maxPagesValue !== undefined) {
  crawlerOptions.maxPages = Number(maxPagesValue);
}

let result;
try {
  result = await runReadmeEcnCrawler(crawlerOptions);
} catch (error) {
  process.stderr.write(`readme → ecn crawl could not run: ${error.message}\n`);
  process.exitCode = 1;
}

if (result) {
  const {
    failures,
    warnings,
    pagesVisited,
    ecnLinkCount,
    checked,
    ok,
  } = result;

  for (const warning of warnings) {
    process.stderr.write(`warning: ${warning}\n`);
  }

  if (!ok) {
    for (const failure of failures) {
      process.stderr.write(`${failure}\n`);
    }
    process.stderr.write(
      `readme → ecn crawl failed: ${failures.length} problem(s); `
      + `${pagesVisited.length} page(s) crawled, ${ecnLinkCount} ECN path(s)\n`,
    );
    process.exitCode = 1;
  } else {
    process.stdout.write(
      `readme → ecn crawl passed: ${pagesVisited.length} ReadMe page(s), `
      + `${ecnLinkCount} ECN path(s), ${checked.length} candidate request(s)`
      + (ecnLinkCount === 0 ? ' (no /ecn-sdk cross-links found)' : '')
      + '\n',
    );
  }
}
