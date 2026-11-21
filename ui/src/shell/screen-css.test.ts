import { readdirSync, readFileSync } from "node:fs";
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

it("keeps a row's button label on one line, whatever the cell's overflow-wrap", () => {
  expect(screen).toMatch(
    /\.list \.button\s*{[^}]*white-space:\s*nowrap;[^}]*overflow-wrap:\s*normal/,
  );
});

it("draws only a link's row name in the accent; a name without a page in the text colour", () => {
  expect(screen).toMatch(/(^|\n)\.row-name\s*{[^}]*color:\s*var\(--color-text\);/);
  expect(screen).toMatch(/a\.row-name\s*{[^}]*color:\s*var\(--color-accent\);/);
  expect(screen).not.toMatch(/(^|[^a])\.row-name\s*{[^}]*--color-accent/);
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

it("sets controls side by side --control-gap apart, in a button row and a toolbar", () => {
  expect(screen).toMatch(/\.button-row,\s*\.toolbar\s*{[^}]*gap:\s*var\(--control-gap\)/);
  expect(screen).toMatch(/\.toolbar-end\s*{[^}]*gap:\s*var\(--control-gap\)/);
});

it("centres the primary action on the title's line", () => {
  expect(screen).toMatch(/\.page-head\s*{[^}]*align-items:\s*center;/);
});

it("keeps a list's names on one line, cut with an ellipsis at --name-width", () => {
  expect(screen).toMatch(
    /\.list \.row-name,\s*\.truncate\s*{[^}]*max-width:\s*min\(var\(--name-width\), 50vw\);[^}]*text-overflow:\s*ellipsis;[^}]*white-space:\s*nowrap;/,
  );
  expect(screen).not.toMatch(/\.row-name\s*{[^}]*overflow-wrap:\s*anywhere/);
});

it("draws column heads as small muted labels, one row high, on one line", () => {
  expect(screen).toMatch(
    /\.list th\s*{[^}]*height:\s*var\(--row-height\);[^}]*color:\s*var\(--color-text-muted\);[^}]*font-size:\s*var\(--type-label-size\);[^}]*white-space:\s*nowrap;/,
  );
});

it("tints a pill only with a state's own background, and never below the label step", () => {
  for (const tone of ["ok", "bad", "warn", "accent"]) {
    expect(screen).toMatch(
      new RegExp(
        `\\.pill-${tone}\\s*{[^}]*background:\\s*var\\(--color-${tone}-bg\\);[^}]*color:\\s*var\\(--color-${tone}\\);`,
      ),
    );
  }
  expect(screen).toMatch(/\.pill\s*{[^}]*font-size:\s*var\(--type-label-size\);/);
  expect(screen).toMatch(/\.pill\s*{[^}]*border-radius:\s*var\(--radius-control\);/);
});

it("puts every control at --control-height and panels --panel-gap apart", () => {
  expect(screen).toMatch(
    /\.field input\s*{[^}]*box-sizing:\s*border-box;[^}]*min-height:\s*var\(--control-height\);/,
  );
  expect(screen).toMatch(/\.panel-stack\s*{[^}]*gap:\s*var\(--panel-gap\);/);
});

const shell = readFileSync(resolve(import.meta.dirname, "shell.css"), "utf8");

it("lets a page take the full width, with a reading width for running text only", () => {
  expect(shell).toMatch(/\.page\s*{[^}]*min-width:\s*0;[^}]*}/);
  expect(shell).not.toMatch(/\.page\s*{[^}]*max-width/);
  expect(shell).toMatch(/\.page :where\(p\)\s*{[^}]*max-width:\s*var\(--reading-width\);/);
});

it("gives the shared buttons --control-height and the working area --page-gutter", () => {
  expect(shell).toMatch(/\.button\s*{[^}]*min-height:\s*var\(--control-height\);/);
  expect(shell).toMatch(
    /\.shell-main\s*{[^}]*padding:\s*var\(--page-gutter\) var\(--page-gutter\)/,
  );
});

it("leaves no literal 32 px minimum height (a control's) in any stylesheet", () => {
  const root = resolve(import.meta.dirname, "..");
  const sheets = readdirSync(root, { recursive: true, encoding: "utf8" }).filter((path) =>
    path.endsWith(".css"),
  );
  expect(sheets.length).toBeGreaterThan(10);
  for (const path of sheets) {
    const css = readFileSync(resolve(root, path), "utf8");
    expect(css, path).not.toMatch(/min-height:\s*32px/);
  }
});
