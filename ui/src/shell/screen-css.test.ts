import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, it } from "vitest";

// Vitest hands `?raw` CSS imports back empty, so the files are read directly.
const screen = readFileSync(resolve(import.meta.dirname, "screen.css"), "utf8");
const tokens = readFileSync(resolve(import.meta.dirname, "../styles/tokens.css"), "utf8");

/**
 * The pixels of a spacing token.
 *
 * @param name - The token, such as `--space-3`.
 * @returns Its size in pixels.
 */
function space(name: string): number {
  const match = new RegExp(`${name}:\\s*(\\d+)px`).exec(tokens);
  return Number(match?.[1]);
}

/**
 * The left and right padding of a rule's `padding` shorthand, in pixels.
 *
 * @param selector - The rule's selector, such as `.list th`.
 * @returns The left and right padding.
 */
function sides(selector: string): { left: number; right: number } {
  const rule = new RegExp(`^${selector.replace(".", "\\.")}\\s*{([^}]*)}`, "m").exec(screen);
  const padding = /padding:\s*([^;]+);/.exec(rule?.[1] ?? "")?.[1] ?? "";
  const values = [...padding.matchAll(/var\((--space-\d+)\)/g)].map((match) =>
    space(String(match[1])),
  );
  const right = values[1] ?? values[0] ?? 0;
  return { left: values[3] ?? right, right };
}

it("keeps at least 24 px between the columns of every list", () => {
  for (const selector of [".list th", ".list td"]) {
    const { left, right } = sides(selector);

    expect(left + right).toBeGreaterThanOrEqual(24);
  }
});

it("right-aligns numbers with tabular figures", () => {
  expect(screen).toMatch(
    /\.list \.number\s*{[^}]*text-align:\s*right;[^}]*font-variant-numeric:\s*tabular-nums/,
  );
});

it("lifts what carries a title above the row's link only in the lists that forward its click", () => {
  expect(screen).toMatch(
    /\.catalog-list \[title\],\s*\.expiring-view \[title\]\s*{[^}]*position:\s*relative/,
  );
  expect(screen).not.toMatch(/\.list \[title\]/);
});

it("lays the row's overlay only from a link: a name without a page covers nothing", () => {
  expect(screen).toMatch(/a\.row-name::after\s*{[^}]*position:\s*absolute/);
  expect(screen).not.toMatch(/(^|[^a])\.row-name::after/m);
});

it("lays a horizontal radio group out in a row that wraps, with its label and texts above", () => {
  expect(screen).toMatch(/\.field\[data-orientation="horizontal"\]\s*{[^}]*flex-flow:\s*row wrap;/);
  expect(screen).toMatch(
    /\.field\[data-orientation="horizontal"\] > :not\(\.radio-field\)\s*{[^}]*flex-basis:\s*100%;/,
  );
});
