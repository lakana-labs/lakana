import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, it } from "vitest";

// Vitest hands `?raw` CSS imports back empty, so the file is read directly.
const css = readFileSync(resolve(import.meta.dirname, "table.css"), "utf8");

it("lets every tab of a table's page take the full width: no reading width of its own", () => {
  expect(css).not.toMatch(/--table-reading-width|1040px/);
  expect(css).not.toMatch(/\.page\s*\{[^}]*max-width/);
});
