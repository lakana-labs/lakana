import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  block,
  contrast,
  DARK,
  LIGHT,
  luminance,
  PALETTES,
  properties,
  TOKENS_CSS as tokens,
} from "../test/palette";

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
      ["--color-on-accent", "--color-accent-hover"],
    ] as const;
    for (const [foreground, background] of pairs) {
      expect(contrast(palette, foreground, background), foreground).toBeGreaterThanOrEqual(4.5);
    }
  });

  it.each(modes)("a hovered primary button differs from its rest state (%s)", (mode) => {
    const palette = PALETTES[mode] ?? {};
    expect(palette["--color-accent-hover"]).toMatch(/^#[0-9a-f]{6}$/);
    expect(palette["--color-accent-hover"]).not.toBe(palette["--color-accent"]);
    expect(contrast(palette, "--color-accent-hover", "--color-surface")).toBeGreaterThanOrEqual(3);
  });

  it("hovers the primary button with the token, never a filter", () => {
    const signIn = readFileSync(resolve(import.meta.dirname, "../sign-in/sign-in.css"), "utf8");
    const hovered = block(signIn, ".button-primary[data-hovered]");
    expect(hovered).toContain("var(--color-accent-hover)");
    expect(signIn).not.toMatch(/filter\s*:/);
  });

  it("keeps the light ground and surfaces soft, pale and cool", () => {
    // Not glaring (no pure white, a few steps below it), still pale, and blue-leaning rather
    // than grey-beige or cream: blue ≥ green ≥ red.
    for (const name of ["--color-ground", "--color-surface", "--color-sunk", "--color-float"]) {
      const hex = LIGHT[name] ?? "";
      expect(luminance(hex), name).toBeLessThanOrEqual(0.92);
      expect(luminance(hex), name).toBeGreaterThanOrEqual(0.75);
      const [red = 0, green = 0, blue = 0] = [1, 3, 5].map((at) =>
        parseInt(hex.slice(at, at + 2), 16),
      );
      expect(blue, name).toBeGreaterThanOrEqual(green);
      expect(green, name).toBeGreaterThanOrEqual(red);
    }
    expect(luminance(LIGHT["--color-ground"] ?? "")).toBeLessThan(
      luminance(LIGHT["--color-surface"] ?? ""),
    );
  });

  it("keeps the dark palette as reviewed", () => {
    expect([DARK["--color-ground"], DARK["--color-surface"], DARK["--color-text"]]).toEqual([
      "#0f171c",
      "#151f26",
      "#e3eaee",
    ]);
  });

  it("defines the dark palette under both the media query and data-theme", () => {
    const media = block(tokens, "@media (prefers-color-scheme: dark)");
    const guarded = properties(block(media, ':root:not([data-theme="light"])'));
    expect(guarded).toEqual(DARK);
    expect(Object.keys(DARK).length).toBeGreaterThan(15);
  });

  it.each(modes)("defines the scrim behind a modal (%s)", (mode) => {
    expect(PALETTES[mode]?.["--color-scrim"]).toMatch(/^rgba\(/);
  });

  it("has no font size below 12 px", () => {
    const sizes = [...tokens.matchAll(/(?:--type-[\w-]+-size|font-size)\s*:\s*([\d.]+)px/g)];
    expect(sizes.length).toBeGreaterThanOrEqual(7);
    for (const size of sizes) {
      expect(Number(size[1])).toBeGreaterThanOrEqual(12);
    }
  });

  it("keeps three radii: controls, panels and a person's circle", () => {
    const body = properties(block(tokens, ":root"));
    const radii = Object.fromEntries(
      Object.entries(body).filter(([name]) => name.startsWith("--radius-")),
    );

    expect(radii).toEqual({
      "--radius-control": "4px",
      "--radius-panel": "6px",
      "--radius-round": "50%",
    });
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
