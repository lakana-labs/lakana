import { readdirSync, readFileSync } from "node:fs";
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

/** The source folder, whose stylesheets the held-control test reads. */
const SRC = resolve(import.meta.dirname, "..");

/** A held control's attribute in any of its forms: `[aria-disabled]`, `="true"`, `='true'`, `=true`. */
const HELD = /\[\s*aria-disabled\s*(?:=\s*(["']?)true\1\s*)?\]/;

/**
 * Every rule of the interface's stylesheets for a held control: one marked `aria-disabled`, which
 * (unlike a disabled one, `[data-disabled]` with the `disabled` attribute) keeps the focus, so its
 * ring is seen.
 *
 * @param css - A stylesheet's text, by default every one under `src/`.
 * @returns Each rule's file, selector and declarations.
 */
function heldRules(css?: string): { file: string; selector: string; body: string }[] {
  const sheets =
    css === undefined
      ? readdirSync(SRC, { recursive: true, encoding: "utf8" })
          .filter((name) => name.endsWith(".css"))
          .map((file) => ({ file, text: readFileSync(resolve(SRC, file), "utf8") }))
      : [{ file: "given", text: css }];
  return sheets.flatMap(({ file, text }) =>
    [...text.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/([^{}]+)\{([^{}]*)\}/g)]
      .map((match) => ({ file, selector: (match[1] ?? "").trim(), body: match[2] ?? "" }))
      .filter((rule) => HELD.test(rule.selector)),
  );
}

/**
 * What a held rule must not do: dim (an opacity, a filter) or restyle the focus ring (an outline
 * of its own), each of which would take the ring below the accent's contrast.
 *
 * @param body - The rule's declarations.
 * @returns The properties it sets that it must not.
 */
function heldFaults(body: string): string[] {
  return [...body.matchAll(/(?<![-\w])(opacity|filter|outline(?:-[\w-]+)?)\s*:/g)].map(
    (match) => match[1] ?? "",
  );
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

  it("finds the rules of held controls in every form of the attribute", () => {
    expect(heldRules().map((rule) => rule.file)).toContain("jobs/jobs.css");
    const forms = [
      '.a[aria-disabled="true"] { color: red; }',
      ".a[aria-disabled='true'] { color: red; }",
      ".a[aria-disabled=true] { color: red; }",
      ".a[aria-disabled] { color: red; }",
    ];
    for (const css of forms) {
      expect(heldRules(css), css).toHaveLength(1);
    }
    expect(heldRules(".a[data-disabled] { opacity: 0.6; }")).toEqual([]);
  });

  it("names a held rule's opacity, filter or outline as a fault", () => {
    expect(heldFaults("opacity: var(--x); filter: opacity(0.6); outline-color: red;")).toEqual([
      "opacity",
      "filter",
      "outline-color",
    ]);
    expect(heldFaults("border-color: var(--color-line); color: var(--color-text-faint);")).toEqual(
      [],
    );
  });

  it.each(modes)(
    "a held control is never dimmed, its ring the accent at 3:1 and its label at 4.5:1 (%s)",
    (mode) => {
      const palette = PALETTES[mode] ?? {};
      for (const ground of ["--color-ground", "--color-surface"]) {
        expect(
          contrast(palette, "--color-accent", ground),
          `ring on ${ground}`,
        ).toBeGreaterThanOrEqual(3);
      }
      for (const { file, selector, body } of heldRules()) {
        const where = `${file}: ${selector}`;
        expect(heldFaults(body), where).toEqual([]);
        const label = /(?<![-\w])color\s*:\s*var\((--[\w-]+)\)/.exec(body)?.[1] ?? "--color-text";
        expect(
          contrast(palette, label, "--color-surface"),
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
