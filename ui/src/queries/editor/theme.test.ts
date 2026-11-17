import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, it } from "vitest";

// The theme's source, read as text like the stylesheets in screen-css.test.ts.
const theme = readFileSync(resolve(import.meta.dirname, "theme.ts"), "utf8");

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

it("replaces the lint marks' data: images, which img-src refuses", () => {
  expect(theme).toMatch(/"\.cm-lintRange-error":\s*{[^}]*backgroundImage:\s*"none"/);
  expect(theme).toMatch(/"\.cm-lint-marker-error":\s*{[^}]*content:\s*"none"/);
  expect(theme).not.toContain("url(");
});
