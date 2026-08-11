// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import { defineConfig } from 'astro/config';
import { unified } from '@astrojs/markdown-remark';
import starlight from '@astrojs/starlight';
import { copyFileSync, existsSync, readFileSync, statSync } from 'node:fs';
import { dirname, extname, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { shellPrompt } from './site/expressive-code-shell-prompt.mjs';
import { resolveVersionControl } from './site/version-control.mjs';

import {
  documentationBase,
  documentationBasePath,
  documentationCanonicalBase,
  documentationSite,
  documentationWorkspaceRoot,
  repositoryRoot,
  siteContentRoot,
} from './site/site-config.mjs';

const repository = 'https://github.com/picogrid/ecn-sdk-python';
const documentationRoot = documentationWorkspaceRoot;
const operatorScreenshots = new Map([
  [resolve(documentationRoot, '../operator-app/docs/operator-mock.png'), 'operator-mock.png'],
  [resolve(documentationRoot, '../operator-app/docs/operator-mock-light.png'), 'operator-mock-light.png'],
  [resolve(documentationRoot, '../operator-app/docs/operator-mock-mobile-dark.png'), 'operator-mock-mobile-dark.png'],
  [resolve(documentationRoot, '../operator-app/docs/operator-mock-mobile-light.png'), 'operator-mock-mobile-light.png'],
]);
const base = documentationBase;
const brandIcon = `${base}brand/picogrid-app-icon-192.png`;
const openGraphImage = `${documentationCanonicalBase}brand/ecn-client-og.png`;
const versionControl = resolveVersionControl({ repository });
const documentationVersion = versionControl.version;
const apiManifest = JSON.parse(
  readFileSync(new URL('../scripts/public-api-manifest.json', import.meta.url), 'utf8'),
);
// The manifest is the single reviewed source of navigation, so the sidebar is
// derived from it rather than hand-maintained. Each group links straight to its
// generated index, which lists that group's symbols: leaf pages stay reachable
// through those indexes, search, cross-links, and their stable routes without
// being permanently expanded here.
const pythonReferenceSidebar = {
  label: 'Python',
  collapsed: true,
  items: [
    // One index entry, not one per group: the twelve `Overview` nodes this
    // replaces were the duplication, and the generated index is the only page
    // here that no group link reaches.
    { label: 'Overview', slug: 'reference/python' },
    ...apiManifest.groups.map((group) => ({ label: group.title, slug: group.route })),
  ],
};

function within(directory, target) {
  const path = relative(directory, target);
  return path === '' || (!path.startsWith(`..${sep}`) && path !== '..');
}

function splitTarget(value) {
  const boundary = value.search(/[?#]/);
  return boundary === -1
    ? [value, '']
    : [value.slice(0, boundary), value.slice(boundary)];
}

function documentationRoute(target) {
  let path = relative(documentationRoot, target).split(sep).join('/');
  path = path.slice(0, -extname(path).length);
  if (path === 'index') return base;
  return `${base}${path.split('/').map(encodeURIComponent).join('/')}/`;
}

/**
 * Make source-relative Markdown links usable in the published static site.
 * Documentation pages become site routes; links to shipped source files point
 * to the corresponding public repository file instead of a nonexistent route.
 */
function rewriteSourceLinks() {
  return (tree, file) => {
    if (!file.path) return;
    const sourceDirectory = dirname(resolve(String(file.path)));

    function visit(node) {
      if ((node.type === 'link' || node.type === 'image') && typeof node.url === 'string') {
        const [pathname, suffix] = splitTarget(node.url);
        if (!pathname) {
          // fragment-only / empty
        } else if (
          pathname.startsWith('/')
          && !pathname.startsWith('//')
          && !pathname.startsWith(base)
          && pathname !== documentationBasePath
          && !pathname.startsWith(`${documentationBasePath}/`)
        ) {
          // Site-absolute paths without the mount (e.g. /concepts/x/) → under base.
          node.url = `${base}${pathname.slice(1)}${suffix}`;
        } else if (
          !pathname.startsWith('/')
          && !/^(?:[a-z]+:|\/\/)/i.test(pathname)
        ) {
          let decodedPath;
          try {
            decodedPath = decodeURIComponent(pathname);
          } catch {
            decodedPath = pathname;
          }
          const target = resolve(sourceDirectory, decodedPath);
          if (!existsSync(target) || !statSync(target).isFile()) {
            throw new Error(`broken source-relative documentation link: ${pathname}`);
          }
          if (node.type === 'image' && operatorScreenshots.has(target)) {
            node.url = `${base}${operatorScreenshots.get(target)}${suffix}`;
          } else if (
            within(documentationRoot, target)
            && ['.md', '.mdx'].includes(extname(target))
          ) {
            node.url = `${documentationRoute(target)}${suffix}`;
          } else if (within(repositoryRoot, target)) {
            const sourcePath = relative(repositoryRoot, target).split(sep).join('/');
            node.url = `${repository}/blob/main/${sourcePath
              .split('/')
              .map(encodeURIComponent)
              .join('/')}${suffix}`;
          }
        }
      }
      if (Array.isArray(node.children)) node.children.forEach(visit);
    }

    visit(tree);
  };
}

function isHostname(value) {
  const segments = (value.startsWith('.') ? value.slice(1) : value).split('.');
  return segments.every((segment) => (
    segment !== ''
    && !segment.startsWith('-')
    && !segment.endsWith('-')
    && /^[A-Za-z0-9-]+$/.test(segment)
  ));
}

/**
 * Hosts the development server answers to, beyond the local ones it always
 * allows. The server refuses an unrecognised `Host` header so that a page open
 * elsewhere cannot read a local preview, so previewing through a tunnel names
 * that tunnel for the run: `DOCS_DEV_ALLOWED_HOSTS=tunnel.example npx astro dev`.
 * An ephemeral preview host belongs to whoever is previewing rather than to this
 * repository, and nothing here reaches a build.
 */
function developmentPreviewHosts(environment = process.env) {
  const hosts = (environment.DOCS_DEV_ALLOWED_HOSTS ?? '')
    .split(',')
    .map((host) => host.trim())
    .filter(Boolean);
  for (const host of hosts) {
    if (!isHostname(host)) {
      throw new Error(`development preview host is not an ordinary hostname: ${host}`);
    }
  }
  return hosts;
}

function includeOperatorScreenshots() {
  return {
    name: 'picogrid-operator-screenshots',
    hooks: {
      'astro:build:done': ({ dir }) => {
        for (const [source, output] of operatorScreenshots) {
          if (!existsSync(source) || !statSync(source).isFile()) {
            throw new Error(`operator publication screenshot is missing: ${output}`);
          }
          copyFileSync(source, fileURLToPath(new URL(output, dir)));
        }
      },
    },
  };
}

export default defineConfig({
  site: documentationSite,
  base,
  publicDir: './site/public',
  outDir: siteContentRoot,
  markdown: {
    processor: unified({ remarkPlugins: [rewriteSourceLinks] }),
  },
  integrations: [
    starlight({
      title: 'Picogrid ECN SDK',
      description: 'Connect sensors, effectors, and operator tools to authorized ECN data and tasking.',
      disable404Route: true,
      // Starlight applies `base` to this itself, so it is authored unmounted.
      favicon: '/brand/picogrid-app-icon-192.png',
      // The mono face is served from this site rather than requested from a
      // font host, and it is listed before the theme so the theme's own rules
      // are still the last word. See NOTICE.md for its licence.
      customCss: ['@fontsource-variable/chivo-mono/wght.css', './src/styles/picogrid.css'],
      expressiveCode: {
        plugins: [shellPrompt()],
        // The only gutter on this site is the decorative shell prompt, which
        // reads as a terminal, not as a line-number column.
        styleOverrides: { gutterBorderWidth: '0' },
      },
      components: {
        Footer: './src/components/DocumentationFooter.astro',
        Header: './src/components/Header.astro',
        Hero: './src/components/Hero.astro',
        MobileTableOfContents: './src/components/MobileTableOfContents.astro',
        PageFrame: './src/components/PageFrame.astro',
        PageTitle: './src/components/PageTitle.astro',
        Sidebar: './src/components/Sidebar.astro',
        SiteTitle: './src/components/SiteTitle.astro',
        ThemeSelect: './src/components/ThemeSelect.astro',
      },
      routeMiddleware: './src/route-data.mjs',
      editLink: {
        // Starlight resolves a page's edit URL against the Astro project root,
        // which is this workspace rather than the repository root, so the base
        // carries the workspace directory back.
        baseUrl: `${repository}/edit/main/${relative(repositoryRoot, documentationRoot).split(sep).join('/')}/`,
      },
      head: [
        {
          // The docs mount is deployment configuration; stylesheets stay
          // mount-neutral and consume these base-derived asset locations.
          tag: 'style',
          content: `:root{--pg-wordmark-dark:url('${base}brand/picogrid-wordmark-dark.png');--pg-nav-texture:url('${base}brand/picogrid-nav-texture.png')}`,
        },
        {
          tag: 'meta',
          attrs: { name: 'version', content: documentationVersion },
        },
        // Bind every published page to the exact source it was built from, so a
        // reader can check the guide against the repository without trusting CI.
        {
          tag: 'meta',
          attrs: { name: 'source-ref', content: versionControl.reference },
        },
        ...(versionControl.commit
          ? [{
            tag: 'meta',
            attrs: { name: 'source-commit', content: versionControl.commit },
          }]
          : []),
        {
          tag: 'meta',
          attrs: { name: 'theme-color', content: '#181818' },
        },
        {
          tag: 'link',
          attrs: {
            rel: 'apple-touch-icon',
            sizes: '192x192',
            href: brandIcon,
          },
        },
        {
          tag: 'meta',
          attrs: { property: 'og:image', content: openGraphImage },
        },
        {
          tag: 'meta',
          attrs: { name: 'twitter:image', content: openGraphImage },
        },
        {
          tag: 'script',
          content: `addEventListener('DOMContentLoaded', () => {
            for (const menu of document.querySelectorAll('starlight-menu-button')) {
              const button = menu.querySelector('button[aria-expanded]');
              if (!button) continue;
              const synchronize = () => button.setAttribute(
                'aria-expanded',
                menu.getAttribute('aria-expanded') === 'true' ? 'true' : 'false',
              );
              new MutationObserver(synchronize).observe(menu, {
                attributes: true,
                attributeFilter: ['aria-expanded'],
              });
              synchronize();
            }
          });`,
        },
      ],
      lastUpdated: true,
      markdown: {
        processedDirs: [documentationRoot],
      },
      // What changed between releases is asked of the version menu, which
      // carries the changelog; the band's icons are the source itself.
      social: [{ icon: 'github', label: 'Source repository', href: repository }],
      sidebar: [
        { label: 'Overview', slug: 'index' },
        {
          label: 'Install and authenticate',
          items: [
            'getting-started/installation',
            'getting-started/configuration',
            'getting-started/authentication',
            'getting-started/mock-setup',
            'getting-started/preflight',
            'how-to/check-clock',
          ],
        },
        {
          label: 'Quickstarts',
          items: [
            'quickstarts/observe-data',
            'quickstarts/sensor-publisher',
            'quickstarts/effector-handler',
            'quickstarts/operator-view',
          ],
        },
        {
          label: 'Core concepts',
          items: [
            'concepts/ecns',
            'concepts/entities',
            'concepts/locations',
            'concepts/tasks',
            'concepts/mesh-routing',
            'concepts/acls',
            'concepts/mqtt-wire',
            'concepts/uuids',
            'concepts/lifecycle',
          ],
        },
        {
          label: 'Sensor integration',
          items: [
            'integrations/sensors',
            'how-to/tracks-detections',
            'how-to/pli-entities',
            'how-to/observe-location',
            'how-to/observe-mesh-data',
          ],
        },
        {
          label: 'Effector integration',
          items: [
            'integrations/effectors',
            'how-to/receive-local-tasks',
            'how-to/dispatch-local-tasks',
            'how-to/dispatch-mesh-tasks',
          ],
        },
        {
          label: 'Operator workflows',
          items: [
            'operator/workflows',
            'operator/application',
            'walkthroughs/track-viewer',
            'walkthroughs/task-handler-service',
            'walkthroughs/tactical-live-map',
          ],
        },
        {
          label: 'Security and credentials',
          items: [
            'security/credentials',
            'concepts/security',
          ],
        },
        {
          label: 'API reference',
          items: [
            'reference/api',
            'reference/licensing',
            pythonReferenceSidebar,
            'reference/configuration',
            'reference/exceptions',
            'reference/wire-formats',
            'how-to/protobuf-decode',
          ],
        },
        {
          label: 'Compatibility and limitations',
          slug: 'compatibility/limitations',
        },
        {
          label: 'Deployment and support',
          items: [
            'deployment-support',
            'how-to/troubleshooting',
            'how-to/cleanup',
            'shipped-tooling',
          ],
        },
        { label: 'Changelog', slug: 'changelog' },
      ],
    }),
    includeOperatorScreenshots(),
  ],
  vite: {
    server: {
      allowedHosts: developmentPreviewHosts(),
    },
  },
});
