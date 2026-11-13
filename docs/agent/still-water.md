# Lakana UI: Still Water

This is the Still Water checklist for the web interface in `ui/`: the rules to follow when you
build or change anything under `ui/src` that a person sees (a screen, component, stylesheet,
string or glyph). Every screen pull request is checked against it. The reasons behind every
rule are on the contributor page `docs/site/explanation/how-lakana-looks.md`; read that once.
Code rules for `ui/` (CSP, i18n, parity, breadcrumb component, licences) are in
`docs/agent/conventions.md`, "The `ui/` project"; its gates in `docs/agent/testing.md`.

## Tokens

From `ui/src/styles/tokens.css`, always as `var(--…)`:

- **Colours.** Surfaces `ground`, `surface`, `sunk`, `float`; text `text`, `text-muted`,
  `text-faint`; `line` and `edge`; one `accent` (links, the current place, the primary button,
  focus, the running glyph) with `accent-hover` and `accent-bg`; `ok`, `warn`, `bad` and their
  backgrounds only for state, warnings and errors; `knockout`, `on-accent`, `scrim`. Literal
  only: `transparent`, `currentColor`, and system colours (`Canvas`, `CanvasText`, ...) inside
  `@media (forced-colors: active)`. The logo's sail amber never enters the interface.
- **Type.** Font size and line height from the seven `--type-*` steps (title, section, body,
  dense, data, code, label), never below 12 px; `--font-sans` (IBM Plex Sans) and `--font-mono`.
  A literal `font-weight` may be 400 or 500, the two weights bundled.
- **Spacing.** Padding, margin and gaps between elements from `--space-1`…`--space-8`
  (4–64 px). Row height `--row-height` (44 px roomy, 36 px compact).
- **Radii** `--radius-control` (4 px), `--radius-panel` (6 px) and `--radius-round` (a person's
  circle: picture or initials); **shadow** only
  `--shadow-float`, only on floating surfaces (menus, the palette, dialogs).
- **Motion.** `--motion-fast` (hover), `--motion-panel` (panels, the theme fade), `--ease`,
  `--pulse`. The only literal duration is `0ms` under reduced motion. Nothing moves unless the
  user or a run caused it; everything stops under reduced motion.
- **Focus** `--focus-ring` with `--focus-offset`.

Literal values the merged stylesheets use, and which stay allowed:

- border widths: 1 px hairlines and control edges, 2–3 px indicator bars (tab underline,
  current-area bar, notice bar);
- layout bounds: `min-height`/`max-height`, `width`/`max-width` (also inside `min(…px, 100%)`),
  `flex-basis`, and the `max-width: 639.98px` breakpoint; a control's 32 px minimum height
  (touch targets stay ≥ 24 px);
- icon and glyph sizes (16 px, 12 px smallest) and small optical nudges (`vertical-align`,
  a negative `outline-offset`, the 2 px gap between sidebar links);
- `z-index`, the visually-hidden pattern (1 px), and opacity for disabled controls and the
  running pulse (whose lowest point is tested at 3:1 in `colour-vision.test.ts`).

Anything else that needs a new value: add a token in its own reviewed step, with its test.
Light and dark are equals: check both, every time.

## Components and patterns

- **One pattern per area:** sidebar in words (place) → one search field and few filters (find)
  → one table: name first, state as glyph + word, numbers right-aligned (list) → at most one
  primary action, top right (act). Detail page: name, one row of tabs, Overview first. At most
  two levels.
- **Rows open their object:** the name is a real link in the accent colour, a quiet `›` ends the
  row, the whole row is clickable. Something without a detail page is not a list row.
- **Lists keep at least 24 px between columns** (`--space-3` on each side of a cell); numbers
  right-aligned with tabular figures (`.number`).
- **Breadcrumbs on every detail page:** pass `trail` to `Page`; never a breadcrumb on an area's
  own list or tab.
- **State** uses `StatusGlyph`/`StatusLabel` (shape + word, never colour alone). A new state
  needs a new silhouette and passes `ui/src/status/colour-vision.test.ts`.
- **Density:** design for the roomy 44 px row; check compact (36 px) too.
- **Refusals and errors name the cause and the next step**, in the user's words, next to the
  field they belong to, or above the form when they concern the whole step. Every view designs
  its empty, loading, partial, error, offline or stale, and permission-denied states.
- Every string through i18next (`ui/src/i18n/en.json`); sentence case; the same term everywhere.

## Accessibility (WCAG 2.2 AA)

- Keyboard first: every action reachable, focus visible (`--focus-ring`), no traps.
- React Aria for interactive patterns; a native element when it does the job.
- Contrast: body text 7:1, other text 4.5:1, glyphs, edges and focus 3:1.
- A decorative SVG is `aria-hidden`; one that stands alone has `role="img"` and a name.
- Works at 320 px wide (reflow), in forced colours, with reduced motion.
- The axe smoke test passes; screenshots in light and dark for review.

## Forbidden

A screen with any of these is sent back:

- purple-blue (or any) gradients;
- glassmorphism: frosted, translucent or blurred panels (the `--color-scrim` behind a modal is
  not a panel);
- emoji as icons;
- shadowed rounded cards for everything, KPI tiles, dashboards of boxes (a shadow belongs only
  to a floating surface);
- centred hero layouts, big welcome banners;
- Inter (or any face but Plex) as the body face;
- placeholder data like "John Doe", "Jane Smith", "Acme", "Lorem ipsum", "foo/bar"; use
  realistic Lakana data (tables such as `gold.station_ranking`, real job and run names);
- a second accent colour, colour as the only signal, a spinner without words after 1 s;
- hover-only controls, menus more than one level deep, a second primary action.

## Reviewing a screen pull request

Check it against each section above and say which rules it meets and which it breaks, with the
file and line. Look at its light and dark screenshots at roomy and compact.
