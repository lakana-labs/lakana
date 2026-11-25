import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { ALL_SHIPPED, areasFor, visibleAreas } from "../shell/areas";
import { TOUR_GUIDES, tourSteps, tourTarget } from "./steps";

/** The documentation site's sources, which the site serves at directory URLs. */
const SITE = resolve(import.meta.dirname, "../../../docs/site");

/**
 * The source page the site serves at a path, as `scripts/tests/test_user_text_urls.py` maps it.
 *
 * @param path - The path after the site's base, such as `how-to/find-a-table/`.
 * @returns The page's file.
 */
function pageOf(path: string): string {
  const stripped = path.replace(/\/$/, "");
  const directory = resolve(SITE, stripped);
  return existsSync(resolve(directory, "index.md"))
    ? resolve(directory, "index.md")
    : `${directory}.md`;
}

/**
 * The anchors of a page's headings, as the site builds them.
 *
 * @param file - The page's file.
 * @returns Each heading lower-cased, without punctuation, spaces turned into hyphens.
 */
function anchors(file: string): Set<string> {
  const found = new Set<string>();
  for (const line of readFileSync(file, "utf8").split("\n")) {
    const heading = /^#{1,6}\s+(.*?)\s*#*$/.exec(line);
    if (heading?.[1] !== undefined) {
      const text = heading[1].replaceAll("`", "").replaceAll("*", "").toLowerCase();
      found.add(text.replace(/[^\w\s-]/g, "").replace(/\s/g, "-"));
    }
  }
  return found;
}

describe("tourSteps", () => {
  it("follows the sidebar, then Search and Help, with no step of its own for Access", () => {
    const steps = tourSteps(visibleAreas(ALL_SHIPPED));
    expect(steps).not.toContain("access");
    expect(steps).toEqual([
      "home",
      "catalog",
      "queries",
      "jobs",
      "connections",
      "settings",
      "search",
      "help",
    ]);
  });

  it("leaves out the areas a person cannot use", () => {
    expect(tourSteps(visibleAreas(areasFor(true)))).toEqual([
      "home",
      "queries",
      "jobs",
      "connections",
      "settings",
      "search",
      "help",
    ]);
  });

  it("finds each step's place by its data-tour attribute", () => {
    expect(tourTarget("catalog")).toBe('[data-tour="catalog"]');
  });
});

describe("TOUR_GUIDES", () => {
  it("gives every step but Help a guide; Help keeps the further reading", () => {
    const steps = tourSteps(visibleAreas(ALL_SHIPPED));
    expect(Object.keys(TOUR_GUIDES).sort()).toEqual(steps.filter((step) => step !== "help").sort());
  });

  it.each(Object.entries(TOUR_GUIDES))(
    "links %s to a page and a heading of the documentation site that exist",
    (_step, path) => {
      const [page = "", anchor] = path.split("#");
      expect(page, "the site uses directory URLs").toMatch(/^(tutorials|how-to)\/[a-z0-9-]+\/$/);
      const file = pageOf(page);
      expect(existsSync(file), `${file} exists`).toBe(true);
      if (anchor !== undefined) {
        expect(anchors(file), `a heading of ${file}`).toContain(anchor);
      }
    },
  );

  it("reads the anchors as the site builds them", () => {
    const found = anchors(pageOf("how-to/find-a-run/"));
    expect(found).toContain("find-what-failed-in-every-environment");
    expect(found).not.toContain("no-such-heading");
    expect(pageOf("how-to/")).toBe(resolve(SITE, "how-to/index.md"));
  });
});
