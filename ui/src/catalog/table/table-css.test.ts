import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, it } from "vitest";

// Vitest hands `?raw` CSS imports back empty, so the file is read directly.
const css = readFileSync(resolve(import.meta.dirname, "table.css"), "utf8");

it("defines the table page's reading width once and uses it by name", () => {
  expect(css.match(/1040px/g)).toHaveLength(1);
  expect(css.match(/var\(--table-reading-width\)/g)?.length).toBeGreaterThanOrEqual(2);
});
