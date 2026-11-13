import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, it } from "vitest";

// Vitest hands `?raw` CSS imports back empty, so the file is read directly.
const css = readFileSync(resolve(import.meta.dirname, "catalog.css"), "utf8");

it("shows a collapsed ancestor of the current place, by weight and not by colour alone", () => {
  const rule = /\[data-current-ancestor\][^{]*{([^}]*)}/.exec(css);

  expect(rule?.[1]).toMatch(/font-weight:\s*500/);
});
