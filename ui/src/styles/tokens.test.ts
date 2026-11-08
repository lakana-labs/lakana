import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

// Vitest hands `?raw` CSS imports back empty, so the file is read directly.
const tokens = readFileSync(resolve(import.meta.dirname, "tokens.css"), "utf8");

type Palette = Record<string, string>;

/**
 * Returns the declarations of the first rule whose selector line matches.
 *
 * @param css - The stylesheet text.
 * @param selector - The selector exactly as written, without the brace.
 * @returns The text between the rule's braces.
 */
function block(css: string, selector: string): string {
  const start = css.indexOf(`${selector} {`);
  if (start === -1) {
    throw new Error(`no rule for ${selector}`);
  }
  const open = css.indexOf("{", start);
  let depth = 0;
  for (let index = open; index < css.length; index += 1) {
    if (css[index] === "{") depth += 1;
    if (css[index] === "}") depth -= 1;
    if (depth === 0) return css.slice(open + 1, index);
  }
  throw new Error(`unclosed rule for ${selector}`);
}

/**
 * Reads the custom properties of a rule body.
 *
 * @param body - The text between a rule's braces.
 * @returns Each custom property name mapped to its value.
 */
function properties(body: string): Palette {
  const result: Palette = {};
  for (const match of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    result[match[1] ?? ""] = (match[2] ?? "").trim();
  }
  return result;
}

const LIGHT = properties(block(tokens, ":root"));
const DARK = properties(block(tokens, ':root[data-theme="dark"]'));
const PALETTES: Record<string, Palette> = { light: LIGHT, dark: DARK };

/**
 * The relative luminance of a `#RRGGBB` colour (WCAG 2).
 *
 * @param hex - The colour.
 * @returns The luminance between 0 and 1.
 */
function luminance(hex: string): number {
  const [r = 0, g = 0, b = 0] = [1, 3, 5].map((at) => {
    const channel = parseInt(hex.slice(at, at + 2), 16) / 255;
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/**
 * The WCAG 2 contrast ratio of two token colours.
 *
 * @param palette - The palette to read from.
 * @param foreground - The foreground token name.
 * @param background - The background token name.
 * @returns The ratio, 1 to 21.
 */
function contrast(palette: Palette, foreground: string, background: string): number {
  const first = luminance(palette[foreground] ?? "");
  const second = luminance(palette[background] ?? "");
  return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05);
}

const modes = Object.keys(PALETTES);

describe("tokens", () => {
  it.each(modes)("body text reaches AAA on ground and surface (%s)", (mode) => {
    const palette = PALETTES[mode] ?? {};
    for (const ground of ["--color-ground", "--color-surface"]) {
      expect(contrast(palette, "--color-text", ground)).toBeGreaterThanOrEqual(7);
    }
  });

  it.each(modes)("muted, faint, accent and status text reach AA (%s)", (mode) => {
    const palette = PALETTES[mode] ?? {};
    for (const text of [
      "--color-text-muted",
      "--color-text-faint",
      "--color-accent",
      "--color-ok",
      "--color-warn",
      "--color-bad",
    ]) {
      for (const ground of ["--color-ground", "--color-surface"]) {
        expect(contrast(palette, text, ground), `${text} on ${ground}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it.each(modes)("control edges reach 3:1 on the surface (%s)", (mode) => {
    expect(
      contrast(PALETTES[mode] ?? {}, "--color-edge", "--color-surface"),
    ).toBeGreaterThanOrEqual(3);
  });

  it.each(modes)("glyph knock-outs reach 3:1 on every filled glyph (%s)", (mode) => {
    const palette = PALETTES[mode] ?? {};
    for (const fill of ["--color-ok", "--color-bad", "--color-warn", "--color-accent"]) {
      expect(contrast(palette, "--color-knockout", fill), fill).toBeGreaterThanOrEqual(3);
    }
  });

  it.each(modes)("status words reach AA on their own backgrounds (%s)", (mode) => {
    const palette = PALETTES[mode] ?? {};
    const pairs = [
      ["--color-ok", "--color-ok-bg"],
      ["--color-warn", "--color-warn-bg"],
      ["--color-bad", "--color-bad-bg"],
      ["--color-accent", "--color-accent-bg"],
      ["--color-on-accent", "--color-accent"],
    ] as const;
    for (const [foreground, background] of pairs) {
      expect(contrast(palette, foreground, background), foreground).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("defines the dark palette under both the media query and data-theme", () => {
    const media = block(tokens, "@media (prefers-color-scheme: dark)");
    const guarded = properties(block(media, ':root:not([data-theme="light"])'));
    expect(guarded).toEqual(DARK);
    expect(Object.keys(DARK).length).toBeGreaterThan(15);
  });

  it("has no font size below 12 px", () => {
    const sizes = [...tokens.matchAll(/(?:--type-[\w-]+-size|font-size)\s*:\s*([\d.]+)px/g)];
    expect(sizes.length).toBeGreaterThanOrEqual(7);
    for (const size of sizes) {
      expect(Number(size[1])).toBeGreaterThanOrEqual(12);
    }
  });

  it("turns every duration off under reduced motion", () => {
    const body = properties(block(tokens, ":root"));
    const durations = Object.keys(body).filter((name) => /^--(motion-|pulse)/.test(name));
    expect(durations.length).toBeGreaterThanOrEqual(3);
    const zeroed = properties(
      block(block(tokens, "@media (prefers-reduced-motion: reduce)"), ":root"),
    );
    for (const name of durations) {
      expect(zeroed[name], name).toBe("0ms");
    }
  });
});
