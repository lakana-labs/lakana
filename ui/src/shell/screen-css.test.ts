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
