import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render } from "@testing-library/react";
import { createElement } from "react";
import { describe, expect, it } from "vitest";

import { MACHADO_2009, simulate, type Vision, VISIONS } from "../test/colour-vision";
import { luminanceOf, PALETTES, ratio } from "../test/palette";
import { StatusGlyph } from "./StatusGlyph";
import { STATUS_KINDS, type StatusKind } from "./status";

// The glyphs' colour checks through simulated colour vision (docs/site/explanation/
// how-lakana-looks.md, "The seven status glyphs"). The matrices and their source are in
// src/test/colour-vision.ts. A glyph never relies on colour: each has its own silhouette, and
// these tests prove that the silhouette stays visible (3:1, WCAG 1.4.11) for every vision.

// Vitest hands `?raw` CSS imports back empty, so the file is read directly.
const statusCss = readFileSync(resolve(import.meta.dirname, "status.css"), "utf8");

/** The surfaces a glyph is drawn on. */
const BACKGROUNDS = ["--color-ground", "--color-surface", "--color-sunk", "--color-float"];

/** The glyphs drawn filled, with a knock-out mark inside. */
const KNOCKED_OUT: readonly StatusKind[] = ["succeeded", "failed", "warning"];

/**
 * The colour token of each glyph, as status.css assigns it.
 *
 * @returns Each kind mapped to its token name.
 */
function glyphTokens(): Record<StatusKind, string> {
  const found: Partial<Record<StatusKind, string>> = {};
  for (const rule of statusCss.matchAll(/([^{}]+)\{\s*color:\s*var\((--[\w-]+)\);\s*\}/g)) {
    for (const selector of (rule[1] ?? "").split(",")) {
      const kind = selector.trim().replace(/^\.status-glyph-/, "") as StatusKind;
      if (STATUS_KINDS.includes(kind)) {
        found[kind] = rule[2] ?? "";
      }
    }
  }
  return found as Record<StatusKind, string>;
}

const TOKENS = glyphTokens();
const CASES = Object.keys(PALETTES).flatMap((mode) =>
  VISIONS.map((vision) => [mode, vision] as const),
);

/**
 * The contrast of two token colours as someone with the given vision sees them.
 *
 * @param mode - `light` or `dark`.
 * @param vision - The kind of colour vision.
 * @param first - One token name.
 * @param second - The other.
 * @returns The WCAG 2 contrast ratio of the simulated colours.
 */
function seen(mode: string, vision: Vision, first: string, second: string): number {
  const palette = PALETTES[mode] ?? {};
  return ratio(
    luminanceOf(simulate(palette[first] ?? "", vision)),
    luminanceOf(simulate(palette[second] ?? "", vision)),
  );
}

/**
 * The silhouette of a glyph: its markup, which carries no colour (only `currentColor`).
 *
 * @param kind - The glyph.
 * @returns Its inner SVG markup.
 */
function silhouette(kind: StatusKind): string {
  const { container, unmount } = render(createElement(StatusGlyph, { kind }));
  const markup = container.querySelector("svg")?.innerHTML ?? "";
  unmount();
  return markup;
}

describe("the status glyphs through simulated colour vision", () => {
  it("reads every glyph's colour from status.css", () => {
    expect(Object.keys(TOKENS).sort()).toEqual([...STATUS_KINDS].sort());
  });

  it.each(Object.entries(MACHADO_2009))("keeps white white under the %s matrix", (_, matrix) => {
    for (const row of matrix) {
      expect(row.reduce((sum, value) => sum + value, 0)).toBeCloseTo(1, 5);
    }
  });

  it("changes the colours it simulates, but never the luminance of a grey", () => {
    expect(simulate("#b0362c", "deuteranopia")).not.toEqual(simulate("#b0362c", "normal"));
    for (const vision of VISIONS) {
      const [r, g, b] = simulate("#777777", vision);
      expect(r, vision).toBeCloseTo(g, 2);
      expect(g, vision).toBeCloseTo(b, 2);
    }
  });

  it.each(CASES)("each glyph stays at 3:1 on every surface (%s, %s)", (mode, vision) => {
    for (const kind of STATUS_KINDS) {
      for (const background of BACKGROUNDS) {
        expect(
          seen(mode, vision, TOKENS[kind], background),
          `${kind} on ${background}`,
        ).toBeGreaterThanOrEqual(3);
      }
    }
  });

  it.each(CASES)("each knock-out mark stays at 3:1 on its glyph (%s, %s)", (mode, vision) => {
    for (const kind of KNOCKED_OUT) {
      expect(seen(mode, vision, "--color-knockout", TOKENS[kind]), kind).toBeGreaterThanOrEqual(3);
    }
  });

  it("tells every pair of glyphs apart by silhouette, so no pair depends on colour", () => {
    const shapes = new Map(STATUS_KINDS.map((kind) => [kind, silhouette(kind)]));
    const pairs = STATUS_KINDS.flatMap((first, index) =>
      STATUS_KINDS.slice(index + 1).map((second) => [first, second] as const),
    );
    expect(pairs).toHaveLength(21);
    for (const [first, second] of pairs) {
      expect(shapes.get(first), `${first} and ${second}`).not.toBe(shapes.get(second));
    }
  });
});
