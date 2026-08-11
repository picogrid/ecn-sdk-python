// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import { copyFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

import {
  assetsOutputRoot,
  documentationBase,
  documentationBasePath,
  documentationCanonicalBase,
  documentationWorkspaceRoot,
} from './site-config.mjs';

const assetsRoot = assetsOutputRoot;
const mountDir = documentationBasePath.replace(/^\/+/, '');

const templatePath = resolve(documentationWorkspaceRoot, 'site/root-redirect.html');
const rootIndexPath = resolve(assetsRoot, 'index.html');
const nested404Path = resolve(assetsRoot, mountDir, '404.html');
const root404Path = resolve(assetsRoot, '404.html');

const template = await readFile(templatePath, 'utf8');
const html = template
  .replaceAll('__CANONICAL__', documentationCanonicalBase)
  .replaceAll('__MOUNT__', documentationBase);

await mkdir(assetsRoot, { recursive: true });
await writeFile(rootIndexPath, html, 'utf8');

// GitHub Pages only serves a custom 404 from the site root, not under /ecn-sdk/.
await copyFile(nested404Path, root404Path);
