---
name: lakana-ui
description: Still Water, the look of Lakana's web interface, as rules for building or reviewing a screen in ui/. Use before writing or changing anything under ui/src that a person sees (a screen, component, stylesheet, string or glyph), and when reviewing a pull request that does; every screen pull request is checked against this skill.
---

# Lakana UI: Still Water

The reasons behind every rule here are on the contributor page
`docs/site/explanation/how-lakana-looks.md`; read it once. This skill is the checklist.
Code rules for `ui/` (CSP, i18n, parity, breadcrumb component, licences) are in
`docs/agent/conventions.md`, "The `ui/` project"; its gates in `docs/agent/testing.md`.

## Tokens

- Every colour, size, space, radius and duration is a `var(--…)` from
  `ui/src/styles/tokens.css`. No literal colour, px size or duration in a component stylesheet.
  No token fits: change the token file in its own reviewed step, with its contrast test.
- Colours: surfaces `ground`, `surface`, `sunk`, `float`; text `text`, `text-muted`,
  `text-faint`; `line` and `edge`; one `accent` (links, current place, the primary button);
  `ok`, `warn`, `bad` only for state. The logo's sail amber never enters the interface.
- Type: the seven `--type-*` steps (title, section, body, dense, data, code, label), never
  below 12 px. IBM Plex Sans for text, Plex Mono for code and data.
- Spacing `--space-1`…`--space-8` (4–64 px); radius 4 px controls, 6 px panels.
- Motion: `--motion-fast` (hover), `--motion-panel` (panels, the theme fade), `--ease`. Nothing
  moves unless the user or a run caused it; everything stops under reduced motion.
- Light and dark are equals: check both, every time.

## Components and patterns

- **One pattern per area:** sidebar in words (place) → one search field and few filters (find)
  → one table: name first, state as glyph + word, numbers right-aligned (list) → at most one
  primary action, top right (act). Detail page: name, one row of tabs, Overview first. At most
  two levels.
- **Rows open their object:** the name is a real link in the accent colour, a quiet `›` ends the
  row, the whole row is clickable. Something without a detail page is not a list row.
- **Breadcrumbs on every detail page:** pass `trail` to `Page`; never a breadcrumb on an area's
  own list or tab.
- **State** uses `StatusGlyph`/`StatusLabel` (shape + word, never colour alone). A new state
  needs a new silhouette and passes `ui/src/status/colour-vision.test.ts`.
- **Density:** design for the roomy 44 px row; check compact (36 px) too.
- **Refusals and errors name the cause and the next step**, in the user's words, next to the
  field or action they belong to. Empty, loading, error and permission-denied states are designed.
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
- glassmorphism: frosted, translucent or blurred panels;
- emoji as icons;
- shadowed rounded cards for everything, KPI tiles, dashboards of boxes;
- centred hero layouts, big welcome banners;
- Inter (or any face but Plex) as the body face;
- placeholder data like "John Doe", "Jane Smith", "Acme", "Lorem ipsum", "foo/bar"; use
  realistic Lakana data (tables such as `gold.station_ranking`, real job and run names);
- a second accent colour, colour as the only signal, a spinner without words after 1 s;
- hover-only controls, menus more than one level deep, a second primary action.

## Reviewing a screen pull request

Check it against each section above and say which rules it meets and which it breaks, with the
file and line. Look at its light and dark screenshots at roomy and compact.
