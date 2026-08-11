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
endorsement. The `brand/*` outputs inventoried in the provenance and transformation
tables below are distributed in `docs/site/public/brand/*`, with mirrored copies in
`operator-app/frontend/public/brand/*`.

## Third-party notices

The verified candidate environment resolved the following direct runtime dependency
versions from the declarations in `pyproject.toml`. The exact resolved versions are
recorded in `uv.lock`:

| Dependency | Declared range | Verified candidate version | License |
| --- | --- | --- | --- |
| `aiomqtt` | `==2.5.1` | 2.5.1 | BSD-3-Clause |
| `paho-mqtt` | `==2.1.0` | 2.1.0 | EPL-2.0 OR BSD-3-Clause |
| `protobuf` | `>=7.35.1,<8` | 7.35.1 | BSD-3-Clause |
| `pydantic` (with `pydantic-core`) | `>=2.11,<3` | 2.13.4 | MIT |

These dependencies are not vendored into the wheel. Another installation may resolve
a different version within a declared range. Its authoritative dependency inventory
is the installed distribution metadata, which `make verify-release` regenerates at
`reports/generated/dependency-licenses.json`; that inventory is not a legal approval.

## Third-party software

The separately installable operator application embeds Leaflet 1.9.4 from the
integrity-pinned npm package recorded in `operator-app/package-lock.json`. Leaflet is
licensed under the BSD 2-Clause License. Its complete copyright notice, conditions,
and disclaimer are shipped in `operator-app/THIRD_PARTY_LICENSES.md` and in the
operator wheel's `.dist-info/licenses/` directory. This third-party notice does not
change the repository's unresolved Picogrid software-license review.

## Picogrid brand review

Picogrid authorized reuse of the minimum static tokens and brand assets needed for
this public documentation site and operator example. The review was pinned to the
read-only `picogrid/web` repository at commit
`711e3a3f7d9c5ed233425a6c218929e14697d80e`; untracked worktree files were not read
or used. The reviewed inputs were:

| Source path | Git blob | SHA-256 of file content | Use |
| --- | --- | --- | --- |
| `.agents/skills/pico-brand/SKILL.md` | `e05dbb8c5a4f1ecb1846c3cb04ac885e133b0df6` | `6d449eec008fda415da49ac1a24155cd5550d345103c59b0b5b746aee6e7f338` | Public color, typography, accessibility, and asset rules |
| `.agents/skills/pico-brand-mobile/SKILL.md` | `4e39c68ff96b14251d9d84d1d639f49ec7d42c36` | `226d806f5a045195d2340a7ff193708041d9a563eec794d493f357020ca4c87b` | Responsive layout and touch-target rules |
| `.agents/skills/pico-brand-terminals/SKILL.md` | `02d269287d459caa1dc4034d0dc50485faf9429a` | `dc618d3f14077927db59cf56a5e282f78088c7e9d761f3e2247d3d374a6e80e8` | Dense, dark-first operator hierarchy |
| `packages/ui/src/styles.css` | `1a8ac07984baa86557484706ee0a46544e702f4b` | `5db11f354fffde0d3b12cb5946eeb833f9532561e2df6a18c2594d3de2b52384` | Static palette, spacing, radius, semantic status, and mono-font tokens |
| `apps/orion/public/assets/general/pg-logo.png` | `3885e140f0b0b00aae293627b3610ad15283646a` (Git LFS object) | `7ab80a61a87cc134421ae42f5101f5df07a71424c9b20f29431c348a7f0eb6ca` | Authorized Picogrid wordmark |

The application mark is not sourced from `picogrid/web`. It is the current public
Picogrid mark, reviewed in the read-only `picogrid/picogrid` repository at commit
`741a3cfa03643863f9feba95fcc5b54d3b7ac558`:

| Source path | Git blob | SHA-256 of file content | Use |
| --- | --- | --- | --- |
| `apps/web/public/favicon.svg` | `bba3e34afdbcbc7217f0e9626bfc678541677f73` | `667954c19b483e17cba600c3e7ed437353f1364525daa85bb591a60fab9835ed` | Current public Picogrid application mark; the exact mark the public Picogrid marketing site publishes as its favicon |

`docs/src/styles/picogrid.css` and `operator-app/frontend/src/styles.css` independently
adapt those static values into semantic light/dark CSS variables. No component,
React source, story, application source, package metadata, or runtime dependency was
copied from `picogrid/web`.

## Static asset transformations

The following transforms were performed locally without a network or generative
asset service. Identical files copied into the docs and standalone operator public
directories intentionally have identical hashes.

| Public output | Transformation | SHA-256 |
| --- | --- | --- |
| `brand/picogrid-wordmark-dark.png` | Exact authorized `1001x94` wordmark bytes; white artwork for a dark surface | `7ab80a61a87cc134421ae42f5101f5df07a71424c9b20f29431c348a7f0eb6ca` |
| `brand/picogrid-wordmark-light.png` | `1001x94` wordmark RGB recolored to Picogrid black `#181818`; source alpha preserved | `bbda6e31860087383538a510b92e7341d25483184bfc4633ea8878867a61f4ea` |
| `brand/picogrid-app-icon-512.png` | Tracked `32x32` vector application mark rasterized to `512x512` with librsvg 2.61.3 (`rsvg-convert -w 512 -h 512`), then re-encoded to metadata-free 8-bit RGB with ImageMagick 7.1.2-13 | `d95b6fbbead6dce849b26c76cc816a6510de0725b5ad0fd72cd614565bcf0540` |
| `brand/picogrid-app-icon-192.png` | Same vector mark, rasterization, and encoding at `192x192` | `9383f03ccc433051752679fd59ca7cd6fbdec4ea330b233123111aa1a1c7595b` |
| `docs/site/public/brand/ecn-client-og.svg` | Independently composed outcome-led `1200x630` Picogrid ECN SDK product card using the authorized wordmark and static tokens | `44ac27e1d3be03f8edb13eb9fbb0b61908476f0b646c07316393dd5280dc0c35` |
| `brand/ecn-client-og.png` | SVG product card rasterized to `1200x630` with librsvg | `f685266adb685d59eb00d9bec7a2c75c0eaf14c71aac02161131c9d6087436aa` |
| `docs/site/public/brand/picogrid-nav-texture.png` | Exact authorized Picogrid cover artwork bytes; supplied `2256x382` 8-bit RGBA PNG (color type 6), with `pHYs`, `sRGB`, and `gAMA` ancillary chunks preserved | `86a1f57335a3784013137ba024ec49d38c404fa56ca3050d79ae8ec3a20e4a8c` |

The documentation site uses `brand/picogrid-app-icon-192.png` as its favicon. It keeps
the published composition exactly, a white triangular mark on a solid black `#000000`
field, so the documentation and operator identity match the current public Picogrid
mark and stay legible against both light and dark browser chrome. Neither the cropped
wordmark letter nor the superseded internal application mark is retained in the
release source, and no Orion-specific product mark is redistributed.

`brand/picogrid-nav-texture.png` is authorized Picogrid cover artwork supplied for
this public site so that the guide's navigation band matches the published Legion
API documentation. It is decorative background artwork carrying no text, mark, or
operational content, and no information is conveyed by it alone.

Unlike every other brand input above, it is not traceable to a path and blob in a
pinned public repository. It was supplied directly by Picogrid rather than taken
from the reviewed `picogrid/web` revision, and no byte-identical file is tracked
there, so a recipient cannot re-derive it from a published source. What a recipient
can check is the artwork they actually received: it is redistributed byte for byte,
unmodified, as 335,984 bytes of 8-bit RGBA PNG (color type 6) measuring `2256x382`,
with its `pHYs`, `sRGB`, and `gAMA` ancillary chunks preserved, hashing to the
SHA-256 recorded above. The departure from pinned-source provenance is deliberate
and recorded in the engineering decision record under D030 rather than left as a gap.

The colour-scheme control's device-default glyph in
`docs/src/components/SchemeGlyph.astro` is not Picogrid line art. Its five path
elements are the `sun-moon` icon from the public Lucide icon set, byte-identical
to the tracked source below, which is what the Legion API documentation's own
theme control draws, so both sites offer the choice under the same mark. The
light and dark glyphs beside it are the documentation theme's own icons, used
under that theme's license.

| Source | Git blob | SHA-256 of file content | Use |
| --- | --- | --- | --- |
| `lucide-icons/lucide@62527757e2607ca3e73eec1e4f24e78cf60eb993`, `icons/sun-moon.svg` | `5465d9f814eebea0b2a7f1f08a29ad5b51812974` | `d1b183b301763d4674e784fab326cf26c3e6dea7192a0b1e1af3709c8cae73db` | Device-default colour-scheme glyph |

| Public output | Transformation | SHA-256 |
| --- | --- | --- |
| `docs/src/components/SchemeGlyph.astro` | The five `path` elements copied unchanged onto the same `0 0 24 24` grid, with the wrapper attributes re-authored so the glyph inherits size and colour from the control | `21289f46bdc06cebb5f0ab7eb274fcbc4c26be469f24b09ab9ad9b39894e38a3` |

Lucide is published under the ISC license, which requires its copyright notice
and permission notice to appear in all copies. The `LICENSE` at the pinned
revision above (Git blob `718bb3f0e44153809972abed31839375804bf652`) states, for
every icon not derived from the Feather project, which `sun-moon` is not:

```text
ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

That requirement follows the glyph into every copy, including the deployed
documentation, which renders the path data but carries none of this file. The
upstream text is therefore redistributed verbatim with the built guide at
`/licenses/lucide-ISC.txt`, byte-identical to the `LICENSE` blob named above,
exactly as the font license is carried at `/fonts/chivo-mono-OFL.txt`.

The operator map markers are independently authored, generic lettered geometric
symbols. They communicate public entity category, affiliation, freshness, and
selection without importing military-standard, restricted, or proprietary tactical
symbology. No Orion favicon, product-specific icon, UI asset, map tile, operational address,
credential, private npm package, or internal infrastructure detail is included.

The finished documentation site and operator frontend depend only on their committed
public npm dependencies and lockfiles. No remote font or brand request is made: the
page is served entirely from its own origin, which the browser suite asserts by
requiring every request a published page makes to be same-origin.

## Fonts

Running text on both the documentation site and the operator frontend is set in the
reader's own system face. No webfont is served for it, and none is named ahead of the
system stack.

The documentation site serves one webfont, for monospace, because that face also sets
its headings and site title rather than only its code:

| Font | Package | Version | License | Served from |
| --- | --- | --- | --- | --- |
| Chivo Mono | `@fontsource-variable/chivo-mono` | `5.3.0` | SIL Open Font License 1.1 | This site's own origin |

Chivo Mono is published by Omnibus-Type under the SIL Open Font License 1.1.
The upstream copyright and license text is redistributed with the built guide at
`/fonts/chivo-mono-OFL.txt`. Only the upright weight axis is included. The face is
loaded from the site's own build output, split by Unicode range, so a reader fetches
only the subset their page needs and no request
reaches a font host. The system monospace stack remains behind it for the moment
before it loads and for any reader who blocks it.
