import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";

const ROOT = resolve(import.meta.dirname, "..");
const PACKAGE = JSON.parse(readFileSync(resolve(ROOT, "package.json"), "utf8")) as {
  dependencies: Record<string, string>;
};
// The fonts reach the build as CSS assets, not as bundled code; their licence ships beside
// the notices (public/licenses/IBM-Plex-OFL-1.1.txt).
const BUNDLED = Object.keys(PACKAGE.dependencies).filter((name) => !name.startsWith("@ibm/plex"));

test("the build carries the licence notice of every bundled package", async ({
  request,
  browserName,
}) => {
  test.skip(browserName !== "chromium", "the build is the same for every browser");
  const response = await request.get("/licenses/THIRD-PARTY-NOTICES.md");
  expect(response.status()).toBe(200);
  const notices = await response.text();

  for (const name of BUNDLED) {
    expect(notices, `${name} is bundled but not named`).toMatch(
      new RegExp(`^## ${name.replace(/[/.]/g, "\\$&")} - `, "m"),
    );
  }
  // Every entry carries its licence text, not only its name: the notice is the condition.
  const entries = notices.split(/^## /m).slice(1);
  expect(entries.length).toBeGreaterThanOrEqual(BUNDLED.length);
  for (const entry of entries) {
    expect(entry.split("\n").slice(1).join("\n").trim(), entry.split("\n")[0]).not.toBe("");
  }
});
