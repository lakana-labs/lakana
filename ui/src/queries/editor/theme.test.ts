import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, it } from "vitest";

// The theme's source, read as text like the stylesheets in screen-css.test.ts.
const theme = readFileSync(resolve(import.meta.dirname, "theme.ts"), "utf8");
// @codemirror/lint as installed: its base theme draws marks with data: images.
const lint = readFileSync(
  resolve(import.meta.dirname, "../../../node_modules/@codemirror/lint/dist/index.js"),
  "utf8",
);

/**
 * The selectors of a theme source whose rule sets a property to a value.
 *
 * @param source - The source.
 * @param rule - The property and value, such as `content: "none"`.
 * @returns Each selector of those rules, one per comma-separated part.
 */
function selectorsSetting(source: string, rule: RegExp): string[] {
  return [...source.matchAll(/"([^"]+)":\s*{([^{}]*)}/g)]
    .filter((match) => rule.test(match[2] ?? ""))
    .flatMap((match) => (match[1] ?? "").split(",").map((part) => part.trim()));
}

it("uses only var(--…) colours and the --type-code step", () => {
  expect(theme).not.toMatch(/#[0-9a-f]{3,8}\b/i);
  expect(theme).not.toMatch(/\b(rgba?|hsla?)\(/i);
  expect(theme).toContain("var(--type-code-size)");
  expect(theme).toContain("var(--type-code-line)");
  expect(theme).toContain("var(--font-mono)");
  const fontSizes = [...theme.matchAll(/fontSize:\s*"([^"]*)"/g)].map((match) => match[1]);
  expect(fontSizes).not.toEqual([]);
  for (const size of fontSizes) {
    expect(size).toMatch(/^var\(--type-/);
  }
});

it("replaces every data: image of the lint base theme, which img-src refuses", () => {
  // The lint module's helpers svg() and underline() build url('data:image/svg+xml,...').
  expect(lint).toContain("return `url('data:image/svg+xml,");
  const ranges = selectorsSetting(lint, /backgroundImage:\s*\/\*@__PURE__\*\/(svg|underline)\(/);
  const markers = selectorsSetting(lint, /content:\s*\/\*@__PURE__\*\/(svg|underline)\(/);
  const replaced = [ranges, markers].flat();
  // Any other property drawn with an image would need a replacement of its own.
  expect(selectorsSetting(lint, /(svg|underline)\(|url\(/).sort()).toEqual(replaced.sort());
  expect(ranges).toContain(".cm-lintRange-error");
  expect(markers).toContain(".cm-lint-marker-error");

  const noImage = selectorsSetting(theme, /backgroundImage:\s*"none"/);
  const noContent = selectorsSetting(theme, /content:\s*"none"/);
  for (const selector of ranges) {
    expect(noImage, selector).toContain(selector);
  }
  for (const selector of markers) {
    expect(noContent, selector).toContain(selector);
  }
  expect(theme).not.toContain("url(");
});
