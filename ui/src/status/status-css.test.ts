import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, it } from "vitest";

// Vitest hands `?raw` CSS imports back empty, so the file is read directly.
const css = readFileSync(resolve(import.meta.dirname, "status.css"), "utf8");

it("the pulse stops under reduced motion", () => {
  const reduced = css.slice(css.indexOf("@media (prefers-reduced-motion: reduce)"));

  expect(reduced).toContain(".status-glyph-pulse");
  expect(reduced).toMatch(/animation:\s*none/);
});

it("forced colours use system colours", () => {
  const forced = css.slice(css.indexOf("@media (forced-colors: active)"));

  expect(forced).toContain("CanvasText");
  expect(forced).toContain("Canvas;");
});
