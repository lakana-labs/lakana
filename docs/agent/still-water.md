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
  (4–64 px), on one 4/8 px grid. One density, compact: a list's row `--row-height` (32 px), a
  data grid's row `--grid-row-height` (24 px), a list cell's line `--cell-line` (the dense line).
  There is no density setting.
- **Layout.** `--control-height` (32 px) for every control, `--control-gap` (8 px) between
  adjacent controls, `--page-gutter` (24 px) around the working area, `--panel-gap` (16 px)
  between panels, `--reading-width` (720 px) for running text only, `--name-width` (360 px)
  where a name in a list is cut.
- **Flat panels.** A block among several on a page is `.flat-panel` (`ui/src/shell/screen.css`):
  `--color-surface`, a `--color-line` hairline, `--radius-panel`, `--space-4` padding, no
  shadow; its heading is the section step (`.panel-title`), its own actions at its top right
  (`.panel-head`); panels stand `--panel-gap` apart (`.panel-stack`). Reuse the classes, never a
  copy of them.
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
- layout bounds: `min-height`/`max-height`, `min-width` (a table inside its own scrolling region), `width`/`max-width` (also inside `min(…px, 100%)`),
  `flex-basis`, and the `max-width: 639.98px` breakpoint; a pill's 20 px minimum height
  (a control's height is `--control-height`; touch targets stay ≥ 24 px);
- icon and glyph sizes (16 px, 12 px smallest) and small optical nudges (`vertical-align`,
  a negative `outline-offset`, the 2 px gap between sidebar links);
- `z-index`, the visually-hidden pattern (1 px), and opacity for disabled controls and the
  running pulse (whose lowest point is tested at 3:1 in `colour-vision.test.ts`).

Anything else that needs a new value: add a token in its own reviewed step, with its test.
Light and dark are equals: check both, every time. The documentation site wears the same colour
tokens (`docs/site/assets/stylesheets/still-water.css`): a changed colour in `tokens.css` is
copied into both of its schemes in the same change, or `scripts/tests/test_docs_look.py` fails
(`docs/agent/testing.md`, "The documentation site").

## Components and patterns

- **One pattern per area:** sidebar in words (place) → one search field and few filters (find)
  → one table: name first, state as glyph + word, numbers right-aligned (list) → at most one
  primary action, top right (act). Detail page: name, one row of tabs, Overview first. At most
  two levels.
- **Rows open their object:** the name is a real link in the accent colour, a quiet `›` ends the
  row, the whole row is clickable. Something without a detail page is not a list row. Where a
  list of facts names something with no page (a dropped table, an orphan, an upload, a version),
  the name is plain text in the text colour (`span.row-name`), with no click layer over the row:
  only a link (`a.row-name`) takes the accent and covers the row.
- **Lists keep at least 24 px between columns** (`--space-3` on each side of a cell); numbers
  right-aligned with tabular figures (`.number`).
- **Lists span the full width** of their place, one hairline between rows, no stripes; column
  heads are small muted labels (the label step) on one line, a row high. A name never wraps:
  `.list .row-name` (and `.truncate` elsewhere) stays on one line and ends with an ellipsis past
  `--name-width`; `TruncationTip` (`ui/src/shell/TruncationTip.tsx`, mounted once in the shell)
  shows it whole on hover and on focus. Never add a tooltip of your own for a cut name.
- **Breadcrumbs on every detail page:** pass `trail` to `Page`; never a breadcrumb on an area's
  own list or tab.
- **State** uses `StatusGlyph`/`StatusLabel` (shape + word, never colour alone). `StatusLabel` is
  a small pill (`.pill`): the 12 px glyph and the word in the label step, sentence case, on the
  state's tint (`.pill-ok`, `-bad`, `-warn`, `-accent`; a hairline for states without a colour).
  A new state needs a new silhouette and passes `ui/src/status/colour-vision.test.ts`, which also
  checks each glyph at 3:1 on its pill's tint.
- **Refusals and errors name the cause and the next step**, in the user's words, next to the
  field they belong to, or above the form when they concern the whole step. Every view designs
  its empty, loading, partial, error, offline or stale, and permission-denied states.
- Every string through i18next (`ui/src/i18n/en.json`); sentence case; the same term everywhere.

## Layout and rhythm

Every screen, every time (the end-to-end test `ui/e2e/layout.spec.ts` measures most of them):

- **Full width.** A screen and its tables take the full width of the working area; only running
  text (a paragraph) keeps `--reading-width`. No horizontal page scroll from 1280 px on, and no
  list that scrolls sideways there; at 320 px a table scrolls inside its own region.
- **Title left, primary action right**, on the title's line (`Page`'s `action`, `.page-head`).
- **One height for a row of controls**: buttons, fields, pickers and search are
  `--control-height`; adjacent controls stand `--control-gap` apart (`.button-row`; a toolbar is
  `.toolbar`, its search on the left and the rest in `.toolbar-end` on the right). Never let two
  buttons touch.
- **Left edges line up** from the title to the toolbar to the table; nothing is indented by a
  one-off margin.
- **Labels and values share a baseline**: facts are `.fact-list` (a `dl`, names muted, one baseline),
  or a flex row with `align-items: baseline`.
- **State as small pills** (`StatusLabel`); colour only for state, links and the one primary
  button.
- **Panels** are `.flat-panel` with a `.panel-head` (title, own actions at the right), `--panel-gap`
  apart. A dashboard panel (`.dashboard-panel`, for Home) has a title and a muted subtitle
  (`.panel-subtitle`, "Recently started · top 5"), at most five rows with their state on the
  right and a link on at its foot (`.panel-foot`).
- **Tokens, not pixels.** A new spacing or size need is a token or a shared class in
  `ui/src/shell/screen.css`, never a one-off value in an area's stylesheet.

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
file and line. Look at its light and dark screenshots at 1280 and 1440 px.
