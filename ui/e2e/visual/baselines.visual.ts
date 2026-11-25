import { readdirSync } from "node:fs";
import { join } from "node:path";

import { expect, test } from "@playwright/test";

import { DESKTOP_PROJECTS, PHONE_PROJECTS, SHOTS } from "./shots";

// Every picture under screenshots/ belongs to a shot of SHOTS in a project that takes it, so a
// renamed or removed shot cannot leave its old baselines behind (docs/agent/testing.md, "Visual
// regression"). A missing picture already fails its own comparison.

/** Where the baselines are, by project. */
const SCREENSHOTS = join(import.meta.dirname, "screenshots");

/**
 * The pictures each project should have.
 *
 * @returns The expected paths, `<project>/<shot>.png`.
 */
function expected(): Set<string> {
  const paths = new Set<string>();
  for (const shot of SHOTS) {
    if (shot.phoneOnly !== true) {
      for (const project of DESKTOP_PROJECTS) {
        paths.add(`${project}/${shot.name}.png`);
      }
    }
    if (shot.phone === true || shot.phoneOnly === true) {
      for (const project of PHONE_PROJECTS) {
        paths.add(`${project}/${shot.name}.png`);
      }
    }
  }
  return paths;
}

test("every baseline belongs to a shot", () => {
  const want = expected();
  const orphans = readdirSync(SCREENSHOTS, { recursive: true, encoding: "utf8" })
    .map((path) => path.split("\\").join("/"))
    .filter((path) => path.endsWith(".png") && !want.has(path));
  expect(orphans, "delete these files, or add their shots back to SHOTS").toEqual([]);
});
