# Lead Hub — build plan

## Palette (all text pairs verified ≥ 4.5:1, WCAG AA)

| Token            | Dark       | Light      | Use |
|------------------|------------|------------|-----|
| `--bg`           | `#17140F`  | `#F4EEE2`  | page (warm charcoal / warm cream) |
| `--surface`      | `#1F1B15`  | `#FBF8F2`  | cards, inputs |
| `--surface-2`    | `#29241C`  | `#ECE4D4`  | hover, header strip |
| `--text`         | `#EFE6D6`  | `#1E1A14`  | body text (14.8:1 / 15.0:1) |
| `--muted`        | `#A89D8A`  | `#5C5345`  | meta text (6.9:1 / 6.5:1) |
| `--gold`         | `#C8A765`  | `#7A5A1C`  | accent TEXT: scores, numbers, active tab (8.0:1 / 5.5:1) |
| `--gold-deco`    | `#C8A765`  | `#B08D4F`  | hairlines/ornament only, never text (light = 2.7:1) |
| `--on-gold`      | `#17140F`  | `#FBF8F2`  | text on gold buttons (8.0:1 / 6.0:1) |
| `--line`         | `#3A3329`  | `#DDD3C1`  | decorative dividers |
| `--line-strong`  | `#7D705C`  | `#8F826C`  | form-control borders (≥ 3:1, WCAG 1.4.11) |

Status hues (each also carries a text label + glyph, never colour alone):
new `#C8A765/#7A5A1C`, contacted `#9FB3C2/#35566E`, replied `#BFC78E/#55602A`,
client `#86C59E/#2C6A45`, rejected `#A0968A/#625A50`.

Light mode is a separately tuned set (gold darkened to antique brass for text), not an inversion.

## Type
- **Display:** Cormorant Garamond 600 (OFL) — wordmark, section titles, stat numbers (lining tabular figures). Has Cyrillic.
- **UI / content:** IBM Plex Sans 400/500/600 (OFL) — lead text, business rows, controls. Has Cyrillic, very legible at small sizes.
- Self-hosted woff2 (latin + cyrillic subsets) from Fontsource packages.

## System
- One radius: `3px` (buttons, inputs, cards, badges). Hairline 1px borders, no shadows-as-decoration, no gradients-as-decoration.
- Weights: 400 body, 500 labels, 600 emphasis; display 600 only.

## Layout & 3D placement
```
┌───────────────────────────────────────────────┐
│ HEADER STRIP (≈200px desktop / ≈170px mobile) │
│  Lead Hub wordmark        [ 3D canvas: right  │
│  4 stat numbers (real)      55% of strip only]│
├───────────────────────────────────────────────┤
│ [Заказы] [Бизнесы рядом]   tabs (ARIA tablist) │
│ filters · list (no canvas behind it, ever)    │
└───────────────────────────────────────────────┘
```
- 3D = ~56 gold points + nearest-neighbour edges (LineSegments), slow rotation, `pointer-events:none`, `aria-hidden`.
- Confined to the header strip; on phones it fades under the text with a mask and lower opacity.
- Header renders a static inline-SVG constellation first; three.js is dynamically imported in `requestIdleCallback` *after* the lists render, then swaps in. Reduced motion or no WebGL → the SVG simply stays.
- DPR capped at 1.5, loop stopped when tab hidden or header scrolled out of view.

## Backend
`leadhub/` package: `config`, `db` (raw sqlite3, WAL), `scoring`, `osm`, `csv_export`, `auth` (HMAC session cookie), `notify` (Bot API), `monitors/` (telegram, reddit, rss, runner), `main` (FastAPI). Monitors run as a separate process (`python -m leadhub.monitor`) because Telethon needs an interactive first login.
