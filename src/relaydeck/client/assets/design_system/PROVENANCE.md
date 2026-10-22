# design_system assets: provenance and license

Bundled assets for the design system. No file here
is installed on any host; The application wires them through the actual web/Android
packaging configuration. No network font dependency at runtime.

## Fonts (SIL Open Font License 1.1)

- `fonts/BricolageGrotesque-{Regular,Medium,SemiBold,Bold,ExtraBold}.ttf`:
  Bricolage Grotesque v9, latin subset, static weights 400/500/600/700/800.
  Copyright 2022 The Bricolage Grotesque Project Authors
  (https://github.com/ateliertriay/bricolage).
  Source binaries: google-webfonts-helper builds of the upstream release,
  fetched from the upstream release. License: `OFL-BricolageGrotesque.txt` (upstream
  OFL.txt, same authors).
- `fonts/JetBrainsMono-{Regular,SemiBold,Bold}.ttf`: JetBrains Mono v24,
  latin subset, static weights 400/600/700. Copyright 2020 The JetBrains
  Mono Project Authors (https://github.com/JetBrains/JetBrainsMono).
  Source binaries: google-webfonts-helper builds of the upstream release,
  fetched from the upstream release. License: `OFL-JetBrainsMono.txt` (upstream OFL.txt).
- `fonts/NotoSansSymbols2-Regular.ttf`: Noto Sans Symbols 2, subset to
  the symbol ranges agents emit.
  Copyright 2022 The Noto Project Authors
  (https://github.com/notofonts/symbols). Source binary: upstream
  `google/fonts` repo
  (`ofl/notosanssymbols2/NotoSansSymbols2-Regular.ttf`), fetched
  from upstream (full file 1233128 bytes, SHA256
  `7d5fb73b7ca67a6798101741f5d280a3d016a56a197afcd4199dbb57b4b82a21`).
  Subset command (fontTools via uvx, not a project dependency):
  `uvx --from fonttools pyftsubset NotoSansSymbols2-Regular.ttf
  --output-file=NotoSansSymbols2-Regular.ttf
  --unicodes="U+2190-21FF,U+2300-23FF,U+2460-24FF,U+2500-25FF,U+2600-27BF,U+2800-28FF,U+2B00-2BFF,U+1F300-1F5FF"
  --layout-features='' --name-IDs='*'`.
  Subset: 429992 bytes, 1179 glyphs, SHA256
  `50ad862c9fee9a057e48a8750e45cf8c7eeda378c822befa9af0af97b3825f8c`.
  Verified: family name still `Noto Sans Symbols 2`; every in-font
  glyph of each requested range retained (2460-24FF contributes none —
  absent upstream); U+2733 U+25D0-U+25D3 U+26A0 U+23FA and full
  U+2800-U+28FF present. License: `OFL-NotoSansSymbols2.txt`
  (upstream OFL.txt, unchanged). Single weight-400 face registered as
  the `Noto Sans Symbols 2` family; UI text and the terminal list it
  as a fallback, never as primary.
- `fonts/NotoSansSymbols-Regular.ttf`: the published, unmodified **full**
  Regular TTF from Noto Sans Symbols v2.003 (not a subset or variable font).
  Copyright 2022 The Noto Project Authors (https://github.com/notofonts/symbols).
  Official release archive:
  https://github.com/notofonts/symbols/releases/download/NotoSansSymbols-v2.003/NotoSansSymbols-v2.003.zip
  (SHA-256 `0c113cdcf6c31d050b80dac39fba2d804a6985281012e76e9220c0a00da007f3`).
  Archive member: `NotoSansSymbols/full/ttf/NotoSansSymbols-Regular.ttf`.
  264032 bytes; SHA-256
  `0088617baec0e8ac47e022cc1f38695f772301c9ef6d1f24a785abbef1e05d79`.
  Embedded version: `Version 2.003; ttfautohint (v1.8.4.7-5d5b)`.
  The archive's unchanged `OFL.txt` ships as `OFL-NotoSansSymbols.txt`
  (SHA-256 `b118dd41337806a5d4797052c77caf3bd096aed783e5eb21b4d11154351e1ac0`).
  Registered as `Noto Sans Symbols`, after Symbols 2 in both fallback chains.

### Web engine fallback notices

`fonts/web-fallback-notices.json` contains the full, unmodified copyright/OFL
notices for all 145 families in Flutter 3.44.8's fallback list, including all
five CJK families. Each record includes its upstream URL and SHA-256 of the
UTF-8 notice. Sources are `ofl/<family>/OFL.txt` at the official Google Fonts
repository commit `47d54ca1ebb14fd864695f1d39dc734f39017c33`:
https://github.com/google/fonts/tree/47d54ca1ebb14fd864695f1d39dc734f39017c33/ofl

`scripts/web_fonts.py` verifies those hashes and requires a notice for every
vendored family before installing fonts. It appends the selected families'
full notices to the web build's existing `assets/NOTICES` and verifies their
presence, preserving Flutter's original notices. No licence network access
is needed during builds. Binaries are fetched from the engine's exact
versioned `https://fonts.gstatic.com/s/<family>/v<N>/<file>` URLs, validated,
and served from the build's own `assets/fonts/`; they are not committed here.

Flet 1.0.3 `page.fonts` maps one file per family name (verified in the
pinned `flet` Dart client: one `FontLoader` asset per family, no weight
descriptors), so each weight registers under its own family alias — see
`components/design_system/fonts.py`. This is a loader limitation workaround,
not a design change: every alias renders the matching static face.

## Decorative assets

- `dots/dot_tile.png`: 18x18 tile, transparent ground with one #241D48
  dot (1.2px radius, centered), generated locally with PIL (4x
  supersample, Lanczos downscale) from the dot spec
  (1.2px dots, 18px grid, #241D48 on #130F26). Rendered via
  `ft.DecorationImage(repeat=ImageRepeat.REPEAT)` over the page-ground
  container; no CSS, no network.
