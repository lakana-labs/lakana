import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  block,
  contrast,
  DARK,
  LIGHT,
  luminance,
  over,
  PALETTES,
  ratio,
  properties,
  TOKENS_CSS as tokens,
} from "../test/palette";

const modes = Object.keys(PALETTES);

/** The source folder, whose stylesheets the held-control test reads. */
const SRC = resolve(import.meta.dirname, "..");

/**
 * Every rule of the interface's stylesheets for a held control: one marked
 * `aria-disabled="true"`, which (unlike a disabled one) keeps the focus, so its ring is seen.
 *
 * @returns Each rule's file, selector and declarations.
 */
function heldRules(): { file: string; selector: string; body: string }[] {
  const files = readdirSync(SRC, { recursive: true, encoding: "utf8" }).filter((name) =>
    name.endsWith(".css"),
  );
  return files.flatMap((file) =>
    [
      ...readFileSync(resolve(SRC, file), "utf8")
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .matchAll(/([^{}]+)\{([^{}]*)\}/g),
    ]
      .map((match) => ({ file, selector: (match[1] ?? "").trim(), body: match[2] ?? "" }))
      .filter((rule) => rule.selector.includes('[aria-disabled="true"]')),
  );
}

/**
 * The contrast of a colour seen through an opacity against the background it is drawn on.
 *
 * @param palette - The palette.
 * @param foreground - The colour's token.
 * @param background - The background's token.
 * @param opacity - The element's opacity, 0 to 1.
 * @returns The ratio, 1 to 21.
 */
function seenContrast(
  palette: Record<string, string>,
  foreground: string,
  background: string,
  opacity: number,
): number {
  const ground = palette[background] ?? "";
  const seen = over(palette[foreground] ?? "", ground, opacity);
  return ratio(luminance(seen), luminance(ground));
}

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

  it("finds the rules of held controls", () => {
    expect(heldRules().map((rule) => rule.file)).toContain("jobs/jobs.css");
  });

  it.each(modes)(
    "a held control keeps its focus ring at 3:1 and its label at 4.5:1, never dimmed (%s)",
    (mode) => {
      const palette = PALETTES[mode] ?? {};
      for (const { file, selector, body } of heldRules()) {
        const opacity = Number(/(?<![-\w])opacity\s*:\s*([\d.]+)/.exec(body)?.[1] ?? "1");
        const label = /(?<![-\w])color\s*:\s*var\((--[\w-]+)\)/.exec(body)?.[1] ?? "--color-text";
        const where = `${file}: ${selector}`;
        for (const ground of ["--color-ground", "--color-surface"]) {
          expect(
            seenContrast(palette, "--color-accent", ground, opacity),
            `${where}: ring on ${ground}`,
          ).toBeGreaterThanOrEqual(3);
        }
        expect(
          seenContrast(palette, label, "--color-surface", opacity),
          `${where}: label`,
        ).toBeGreaterThanOrEqual(4.5);
      }
    },
  );

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

  it("has one density, compact, and no density switch", () => {
    const body = properties(block(tokens, ":root"));
    expect(body["--row-height"]).toBe("32px");
    expect(body["--grid-row-height"]).toBe("24px");
    expect(body["--cell-line"]).toBe("var(--type-dense-line)");
    expect(tokens).not.toMatch(/data-density/);
  });

  it("gives controls one height of at least the 24 px target size, 8 px apart", () => {
    const body = properties(block(tokens, ":root"));
    expect(Number.parseInt(body["--control-height"] ?? "", 10)).toBeGreaterThanOrEqual(24);
    expect(body["--control-gap"]).toBe("var(--space-2)");
  });

  it("keeps the layout on the 4/8 px grid: gutter, panel gap, row and control heights", () => {
    const body = properties(block(tokens, ":root"));
    for (const name of ["--row-height", "--grid-row-height", "--control-height"]) {
      expect(Number.parseInt(body[name] ?? "", 10) % 4, name).toBe(0);
    }
    for (const name of ["--page-gutter", "--panel-gap", "--control-gap"]) {
      expect(body[name], name).toMatch(/^var\(--space-\d\)$/);
    }
  });
});
