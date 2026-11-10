/**
 * The Still Water palettes as the tests read them from `src/styles/tokens.css`, and the WCAG 2
 * contrast arithmetic the token, glyph and colour-vision tests share.
 *
 * @packageDocumentation
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/** Custom property names mapped to their values, as one rule declares them. */
export type Palette = Record<string, string>;

/** A colour as linear-light red, green and blue, each between 0 and 1. */
export type LinearRgb = readonly [number, number, number];

// Vitest hands `?raw` CSS imports back empty, so the file is read directly.
/** The text of the token file. */
export const TOKENS_CSS: string = readFileSync(
  resolve(import.meta.dirname, "../styles/tokens.css"),
  "utf8",
);

/**
 * Returns the declarations of the first rule whose selector line matches.
 *
 * @param css - The stylesheet text.
 * @param selector - The selector exactly as written, without the brace.
 * @returns The text between the rule's braces.
 */
export function block(css: string, selector: string): string {
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
export function properties(body: string): Palette {
  const result: Palette = {};
  for (const match of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    result[match[1] ?? ""] = (match[2] ?? "").trim();
  }
  return result;
}

/** The light palette (`:root`). */
export const LIGHT: Palette = properties(block(TOKENS_CSS, ":root"));
/** The dark palette (`:root[data-theme="dark"]`). */
export const DARK: Palette = properties(block(TOKENS_CSS, ':root[data-theme="dark"]'));
/** Both palettes by name. */
export const PALETTES: Record<string, Palette> = { light: LIGHT, dark: DARK };

/**
 * Converts a `#RRGGBB` colour to linear-light RGB (the sRGB transfer function undone).
 *
 * @param hex - The colour.
 * @returns Its three linear channels.
 */
export function linear(hex: string): LinearRgb {
  const [r = 0, g = 0, b = 0] = [1, 3, 5].map((at) => {
    const channel = parseInt(hex.slice(at, at + 2), 16) / 255;
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return [r, g, b];
}

/**
 * The relative luminance of a linear-light colour (WCAG 2).
 *
 * @param rgb - The colour's linear channels.
 * @returns The luminance between 0 and 1.
 */
export function luminanceOf(rgb: LinearRgb): number {
  const [r, g, b] = rgb;
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/**
 * The relative luminance of a `#RRGGBB` colour (WCAG 2).
 *
 * @param hex - The colour.
 * @returns The luminance between 0 and 1.
 */
export function luminance(hex: string): number {
  return luminanceOf(linear(hex));
}

/**
 * The WCAG 2 contrast ratio of two luminances.
 *
 * @param first - One luminance.
 * @param second - The other.
 * @returns The ratio, 1 to 21.
 */
export function ratio(first: number, second: number): number {
  return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05);
}

/**
 * The WCAG 2 contrast ratio of two token colours.
 *
 * @param palette - The palette to read from.
 * @param foreground - The foreground token name.
 * @param background - The background token name.
 * @returns The ratio, 1 to 21.
 */
export function contrast(palette: Palette, foreground: string, background: string): number {
  return ratio(luminance(palette[foreground] ?? ""), luminance(palette[background] ?? ""));
}
