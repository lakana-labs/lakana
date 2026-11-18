import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, it } from "vitest";

// Vitest hands `?raw` CSS imports back empty, so the file is read directly.
const css = readFileSync(resolve(import.meta.dirname, "jobs.css"), "utf8");

/**
 * The declarations of the first rule whose selector list holds a selector.
 *
 * @param selector - One selector of the rule, as written.
 * @returns The rule's declarations, or an empty string when there is none.
 */
function declarations(selector: string): string {
  for (const [, selectors = "", body = ""] of css.matchAll(/([^{}]+){([^}]*)}/g)) {
    const listed = selectors
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .split(",")
      .map((part) => part.trim());
    if (listed.includes(selector)) {
      return body;
    }
  }
  return "";
}

it.each([
  ".jobs-failure a",
  ".jobs-waiting a",
  ".jobs-note a",
  ".jobs-fact a",
  ".jobs-section .empty a",
])("colours %s with the accent, not the browser's blue", (selector) => {
  expect(declarations(selector)).toMatch(/color:\s*var\(--color-accent\)/);
  expect(declarations(`${selector}:hover`)).toMatch(/color:\s*var\(--color-accent-hover\)/);
  expect(declarations(selector)).not.toMatch(/text-decoration/);
});

it("marks the selected run's row by a tint and a heavier name, not by colour alone", () => {
  expect(declarations('.jobs-runs-scroll tr:has(a.row-name[aria-current="true"])')).toMatch(
    /background:\s*var\(--color-accent-bg\)/,
  );
  expect(declarations('.jobs-runs-scroll a.row-name[aria-current="true"]')).toMatch(
    /font-weight:\s*500/,
  );
});

it("keeps a row's controls and its failure's code above the row's link", () => {
  expect(declarations(".jobs-list .jobs-code")).toMatch(/position:\s*relative/);
  expect(declarations(".jobs-list .jobs-control")).toMatch(/position:\s*relative/);
});

it("keeps the names in the Jobs list's tables whole", () => {
  for (const scroll of ["jobs-running-scroll", "jobs-failed-scroll", "jobs-all-scroll"]) {
    expect(declarations(`.list-scroll.${scroll} .row-name`)).toMatch(/overflow-wrap:\s*normal/);
  }
});
