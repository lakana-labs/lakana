import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, it } from "vitest";

const ROOT = resolve(import.meta.dirname, "../..");

it("the Plex licence ships verbatim", () => {
  const shipped = readFileSync(resolve(ROOT, "public/licenses/IBM-Plex-OFL-1.1.txt"));
  const original = readFileSync(resolve(ROOT, "node_modules/@ibm/plex-sans/LICENSE.txt"));

  expect(shipped.equals(original)).toBe(true);
});
