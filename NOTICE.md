# Notices and public brand provenance

The Picogrid ECN SDK is distributed under the Mozilla Public License 2.0. The
complete, unmodified license text is in [LICENSE](LICENSE).

MPL-2.0 reciprocity is file-level: modifications to covered files stay under
MPL-2.0, while a larger work that combines this SDK with separate proprietary files
may be distributed under its own terms.

The brand asset files ship in this repository and are covered by MPL-2.0 like every
other file; their copyright is granted under §2.1. MPL-2.0 §2.3 grants no trademark
rights. "Picogrid", "ECN", the Picogrid wordmark, and the Picogrid application mark
remain Picogrid trademarks. The license permits copying, modifying, and
redistributing the asset files, but does not permit using Picogrid's names or marks
as trademarks, for example to brand a fork or derived product or to imply
endorsement.

## Runtime dependencies

The SDK declares its direct runtime dependencies in `pyproject.toml`; the exact
resolved dependency set is recorded in `uv.lock`. These dependencies are not
vendored into the client wheel. The release verifier records the installed
candidate's dependency and license inventory in
`reports/generated/dependency-licenses.json`.

## Bundled third-party software

The separately installable operator application embeds Leaflet 1.9.4 from the
integrity-pinned npm package recorded in `operator-app/package-lock.json`. Leaflet is
licensed under the BSD 2-Clause License. Its complete copyright notice, conditions,
and disclaimer are shipped in `operator-app/THIRD_PARTY_LICENSES.md` and in the
operator wheel's `.dist-info/licenses/` directory.

The documentation's device-default colour-scheme glyph uses the `sun-moon` icon from
`lucide-icons/lucide@62527757e2607ca3e73eec1e4f24e78cf60eb993`,
`icons/sun-moon.svg`. Lucide is licensed under the ISC License. The required
copyright and permission notice is redistributed with the built guide at
`/licenses/lucide-ISC.txt`.

The documentation site serves Chivo Mono from
`@fontsource-variable/chivo-mono@5.3.0`. Chivo Mono is published by Omnibus-Type
under the SIL Open Font License 1.1. The upstream license text is redistributed with
the built guide at `/fonts/chivo-mono-OFL.txt`.

## Picogrid brand provenance

Picogrid authorized the minimum static tokens and brand assets used by this public
documentation site and operator example. The copied inputs were reviewed from these
pinned public sources:

| Source | Paths used |
| --- | --- |
| `picogrid/web@711e3a3f7d9c5ed233425a6c218929e14697d80e` | `.agents/skills/pico-brand/SKILL.md`; `.agents/skills/pico-brand-mobile/SKILL.md`; `.agents/skills/pico-brand-terminals/SKILL.md`; `packages/ui/src/styles.css`; `apps/orion/public/assets/general/pg-logo.png` |
| `picogrid/picogrid@741a3cfa03643863f9feba95fcc5b54d3b7ac558` | `apps/web/public/favicon.svg` |

The stylesheets adapt static values from those sources; no private component,
application source, story, package metadata, or runtime dependency was copied.
Output hashes and dimensions are enforced by `scripts/release-policy.json`.

`brand/picogrid-nav-texture.png` is the sole unpinned brand input. Picogrid supplied
it directly, so no source path or Git blob exists. It is redistributed byte for byte
as decorative artwork; the private engineering decision log records its admission
as a named provenance exception.

The operator map markers are independently authored generic geometric symbols. No
Orion favicon, product-specific icon, proprietary tactical symbology, map tile,
operational address, credential, private npm package, or internal infrastructure
detail is included.
