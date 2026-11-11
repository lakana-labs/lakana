# How Lakana looks and why

This page explains the look of Lakana's web interface and the reasons behind it. It is
for anyone who changes a screen, and for anyone who wants to know why the interface is so
quiet. The rules themselves live in the code: the token file `ui/src/styles/tokens.css` and
its tests, and the project skill `.claude/skills/lakana-ui/SKILL.md`, which every pull request
that changes a screen is checked against.

**Short answer.** The interface should feel calm and familiar after ten minutes: few
colours, roomy rows, one way of doing each thing, and nothing that moves unless you or a
run caused it. Colour is kept for state, and state never depends on colour alone.

## Still Water

The direction is called Still Water, after the lakes and lagoons a lakana (an outrigger
canoe) travels on:

- a cool, pale ground and ink-navy text, never pure white or pure black;
- one lake-blue accent, for what you can act on: links, the current place, the primary button;
- hairlines instead of boxes and shadows (in dark, a floating surface is lighter instead of
  shadowed);
- colour kept for state: green for succeeded, red for failed, amber for a warning;
- light and dark as equals: each is designed and tested, neither is derived from the other;
- IBM Plex Sans for text and IBM Plex Mono for code and data, served by Lakana itself, so the
  interface makes no request to another site.

The amber of the logo's sail stays in the logo. Inside the interface amber means "warning",
and a brand colour that looks like a warning would teach people to ignore warnings.

## The tokens

Every colour, size, space and duration comes from one reviewed file, `ui/src/styles/tokens.css`.
A screen takes its colours, type sizes, spacing, radii and durations from it; only
hairline borders, layout bounds and a few small optical adjustments are written as plain values.
If no token fits, the token file changes in its own reviewed step. Its tests check the contrast of every pair that is used together.

**Colour.** Ground, surface, sunk and float are the four surfaces; text, muted and faint
the three text levels; line and edge the hairlines and control borders; accent with its hover
and background; ok, warn and bad with their backgrounds. Body text reaches 7:1 (WCAG AAA)
on the ground and on surfaces, the other text levels and the state colours 4.5:1 (AA), and
control edges 3:1.

**Type scale.** Seven steps, none below 12 px:

| Step | Size and line | Weight | Used for |
|---|---|---|---|
| Title | 20 / 28 px | 500 | the page heading |
| Section | 15 / 22 px | 500 | a section heading, the product name |
| Body | 14 / 22 px | 400 | running text |
| Dense | 13 / 18 px | 400 | secondary lines |
| Data | 12.5 / 18 px | 400 | numbers and values in tables |
| Code | 12.5 / 20 px | 400 | code, in Plex Mono |
| Label | 12 / 16 px | 500 | field labels, column heads |

**Spacing.** Eight steps: 4, 8, 12, 16, 24, 32, 48 and 64 px. Corners are 4 px on controls
and 6 px on panels; only a person is round: their picture, or their initials in the accent on
its pale ground, in a circle beside their name.

**Motion.** Two durations, 120 ms for a hover and 200 ms for a panel or a change of theme,
with one easing curve, and a slow 2.4 s pulse for a running glyph. All of them become 0 under
the system setting "reduce motion".

## The seven status glyphs

Every state has its own shape, so that colour is never the only signal:

| State | Shape |
|---|---|
| Succeeded | a filled circle with a tick |
| Failed | a filled diamond with a cross |
| Warning | a filled triangle with an exclamation mark |
| Running | a ring, half filled, pulsing slowly |
| Queued | an empty ring |
| Upstream failed | a ring with a dash |
| Cancelled | a ring with a slash |

A glyph always sits beside its word (Succeeded, Failed, ...), except where it stands alone
and carries the word as its accessible name. The glyphs are tested at 12 px, the smallest
size in use, in light and dark and in greyscale. In the system's high-contrast mode (forced
colours) they take the system's text colour.

**Colour blindness.** The glyphs' colours are checked through simulations of protanopia,
deuteranopia and tritanopia (the matrices of Machado, Oliveira and Fernandes, 2009, at full
severity) and of achromatopsia (luminance only), in light and dark. The result:

- every glyph keeps at least 3:1 against every surface it is drawn on (WCAG 1.4.11, non-text
  contrast) under every simulation, also while the running glyph pulses. At rest the lowest is
  4.17:1, the failed glyph in light under deuteranopia; at the faintest point of its pulse
  (opacity 0.75) the running glyph measures at least 3.15:1, in light under protanopia on the
  sunk surface;
- every knock-out mark (the tick, the cross, the exclamation mark) keeps at least 3:1 against
  its glyph; the lowest is 5.34:1, the failed glyph in light under deuteranopia;
- the colours alone do **not** tell the states apart: succeeded, failed and warning differ in
  lightness by a ratio of 1.0 to 1.45 under the simulations, and the three ring states share one
  colour. That is by design: every pair of the seven glyphs differs in shape, and the test
  checks all 21 pairs.

The check runs with the interface's unit tests (`ui/src/status/colour-vision.test.ts`), so a
changed colour that breaks it fails before review. The browser tests also save the glyphs as
each simulation shows them, for a look by eye.

## The mark

The Lakana mark is the logo's outrigger canoe drawn flat in one colour: the sail, and the hull
with its outrigger boom and float. The waves and the circle of the logo are left out, because
they turn into noise at 16 px. It is drawn on a 16 px grid so that its edges fall on whole
pixels at 16 and 32 px, and a browser test checks that the sail, the hull and the float stay
apart at 16 px. It is the favicon and sits beside the product name in the sidebar, in the text
colour. The source is `ui/src/shell/lakana-mark.svg`; it is covered by the same terms as the
logo (see `docs/legal/TRADEMARK.md` in the repository).

## Density

Roomy is the default: 44 px rows, few columns, and the kind and last change of an object as a
quiet second line under its name. Compact, with 36 px rows, is a personal setting in the menu
under your name. Roomy is the default because the interface is for people who do not live in
it all day; someone who does can choose compact for themselves.

## One pattern for every area

Every area has the same four parts in the same places:

1. **Place:** the sidebar on the left, in words, with the current area marked by a bar and a
   heavier weight, not by colour alone.
2. **Find:** one search field and a few filters above the list; Ctrl K (⌘ K on a Mac) searches
   everywhere.
3. **List:** one table, the name first, a state as glyph and word, numbers aligned right.
4. **Act:** at most one primary action, top right.

Every object has the same detail page: its name, one row of tabs, Overview first. There are
at most two levels, the area and then the object with its tabs. Once you know one area, you
know them all.

**Rows open their object.** Every row of every list opens its object, and something without a
detail page is not shown as a row. You can see this before you point at it: the name is in the
accent colour and a quiet chevron (›) ends the row; on hover the row is highlighted, the name
underlined and the chevron turns to the accent. The name is a real link, so the keyboard and
screen readers reach it; the whole row is clickable too.

**Breadcrumbs.** Every detail page shows where it sits above its heading, for example
Access › People › test1. Every part but the last is a link. A list or a tab of an area has no
breadcrumb, because the sidebar already says where you are.

## What we avoid

The interface fails, even if every feature works, when it:

- looks like a generic generated interface;
- feels slow or does not respond;
- has menus with many levels;
- makes things hard to find;
- asks for workflows that are too complex;
- looks full or dense.

The project skill lists the patterns that make an interface look generated, such as purple
and blue gradients, frosted glass, emoji as icons, and shadowed rounded cards for everything.

## Influences

Three bodies of work shaped how we think about this interface. None of their authors was
involved in Lakana, and none of them endorses it.

- **Kenya Hara**, the Japanese graphic designer, who writes in *Designing Design* and *White*
  about emptiness as something that invites the person to bring their own meaning, rather than
  as a style. What we take from it: leave space empty instead of filling it with summaries
  and tiles.
- **Arata Isozaki**, the Japanese architect, who curated the exhibition *Ma: Space-Time in
  Japan* (Paris, 1978) about *ma*, the interval between things. What we take from it: the
  space between things is designed, so spacing is a token of its own, not what is left over.
- **The design system of Japan's Digital Agency**
  ([design.digital.go.jp](https://design.digital.go.jp/)), a public-sector design system with
  accessibility as a stated aim. What we take from it: shared tokens and components, the same
  way everywhere, with accessibility checked rather than assumed.
