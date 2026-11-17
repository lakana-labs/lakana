import { readFile } from "node:fs/promises";

import { expect, type Locator, type Page, type Request, test } from "@playwright/test";

import { showInColour, showInGreyscale } from "./colour-vision";
import { cspViolations, recordCspViolations } from "./csp";
import {
  BROKEN_SQL,
  CSV,
  DRAFT,
  LONG_SQL,
  PARSER_ERROR,
  QUERIES_STUBBED,
  RUN_ID,
  SALES_SQL,
  stubQueries,
  WEEKLY,
} from "./queries";
import { consoleErrors, tabTo, violations } from "./session";

const KEY = "lakana.preferences";

/** The run's name in the toolbar and the grid's label once Run moved to its address. */
const RUN_NAME = `Query ${RUN_ID.slice(0, 8)}`;

/**
 * Opens the Queries screen at an address and waits for the editor and the library.
 *
 * @param page - The page.
 * @param path - The address.
 * @returns The editor's text box.
 */
async function openQueries(page: Page, path = "/queries"): Promise<Locator> {
  await page.goto(path);
  await expect(page.getByRole("heading", { level: 1, name: "Queries" })).toBeVisible();
  const editor = page.getByRole("textbox", { name: "SQL" });
  await expect(editor).toBeVisible();
  return editor;
}

/**
 * Replaces the editor's text: selects it all and deletes it, then inserts the new text a line at
 * a time with Enter between, so no completion list opens. (Inserting over a selection would let
 * the browser's own editing copy the old text's style into a style attribute, which the content
 * security policy refuses; a person's typing and pasting go through the editor.)
 *
 * @param page - The page.
 * @param editor - The editor's text box.
 * @param sql - The new text; no line but the last ends in a word.
 */
async function writeSql(page: Page, editor: Locator, sql: string): Promise<void> {
  await editor.click();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.press("Backspace");
  for (const [index, line] of sql.split("\n").entries()) {
    if (index > 0) {
      await page.keyboard.press("Enter");
    }
    await page.keyboard.insertText(line);
  }
}

/**
 * Whether the focus is in the SQL editor, whose text box lives in the editor's shadow root.
 *
 * @param page - The page.
 * @returns True when it is.
 */
function inEditor(page: Page): Promise<boolean> {
  return page.evaluate(() => document.activeElement?.classList.contains("sql-editor") === true);
}

/**
 * Moves the focus with Tab, or with Shift Tab back up the page, until a check holds. (Firefox
 * leaves the page after its last stop rather than coming round to the first, so a stop above the
 * focus is reached backwards.)
 *
 * @param page - The page.
 * @param there - Whether the focus has arrived.
 * @param key - `Tab` forwards, `Shift+Tab` backwards.
 */
async function tabUntil(
  page: Page,
  there: () => Promise<boolean>,
  key: "Tab" | "Shift+Tab",
): Promise<void> {
  for (let step = 0; step < 40; step += 1) {
    if (await there()) {
      return;
    }
    await page.keyboard.press(key);
  }
  throw new Error(`${key} never reached the element`);
}

/**
 * Moves the focus with Tab or Shift Tab until it is in the SQL editor.
 *
 * @param page - The page.
 * @param key - `Tab` forwards, `Shift+Tab` backwards.
 * @returns Resolves once the focus is there.
 */
function tabToEditor(page: Page, key: "Tab" | "Shift+Tab"): Promise<void> {
  return tabUntil(page, () => inEditor(page), key);
}

/**
 * Moves the focus with Tab or Shift Tab until it is on an element.
 *
 * @param target - The element.
 * @param key - `Tab` forwards, `Shift+Tab` backwards.
 * @returns Resolves once the focus is there.
 */
function tabOnto(target: Locator, key: "Tab" | "Shift+Tab"): Promise<void> {
  return tabUntil(
    target.page(),
    () => target.evaluate((node) => node === document.activeElement),
    key,
  );
}

/**
 * The result's grid, once the rows have been read.
 *
 * @param page - The page.
 * @returns The grid.
 */
async function resultGrid(page: Page): Promise<Locator> {
  const grid = page.getByRole("grid", { name: `Result of ${RUN_NAME}, rows` });
  await expect(grid).toBeVisible();
  return grid;
}

/**
 * The run's state line.
 *
 * @param page - The page.
 * @returns The line.
 */
function runLine(page: Page): Locator {
  return page.locator(".queries-run-line");
}

/**
 * The completion list of the editor (inside its shadow root, which Playwright's CSS reaches).
 *
 * @param page - The page.
 * @returns The list.
 */
function completionList(page: Page): Locator {
  return page.locator(".cm-tooltip-autocomplete");
}

/**
 * How far the page is wider than the window.
 *
 * @param page - The page.
 * @returns The overflow in pixels; zero or less when nothing scrolls sideways.
 */
function sidewaysOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

for (const theme of ["light", "dark"] as const) {
  for (const density of ["roomy", "compact"] as const) {
    test.describe(`the Queries screen, ${theme}, ${density}`, () => {
      let sent: string[] = [];

      test.beforeEach(async ({ page }) => {
        sent = await stubQueries(page);
        await page.addInitScript(
          ([key, value]) => {
            if (window.localStorage.getItem(key ?? "") === null) {
              window.localStorage.setItem(key ?? "", value ?? "");
            }
          },
          [KEY, JSON.stringify({ theme, density, language: "en" })],
        );
      });

      test("axe finds nothing on the screen idle, running, with a result, with an error, with each dialog open, and folded at 320 px", async ({
        page,
      }) => {
        const editor = await openQueries(page);
        await expect(page.getByRole("link", { name: WEEKLY.name })).toBeVisible();
        expect(await violations(page), "idle").toEqual([]);

        await writeSql(page, editor, LONG_SQL);
        await page.keyboard.press("ControlOrMeta+Enter");
        await expect(runLine(page)).toHaveText(/Running/);
        await expect(page.getByRole("button", { name: "Cancel" })).toBeVisible();
        expect(await violations(page), "running").toEqual([]);
        await page.getByRole("button", { name: "Cancel" }).click();
        await expect(runLine(page)).toHaveText(/Cancelled/);
        expect(await violations(page), "cancelled").toEqual([]);

        await writeSql(page, editor, SALES_SQL);
        await page.keyboard.press("ControlOrMeta+Enter");
        await resultGrid(page);
        await expect(page.getByRole("button", { name: "Download CSV" })).toBeVisible();
        expect(await violations(page), "with a result").toEqual([]);

        await writeSql(page, editor, BROKEN_SQL);
        await page.keyboard.press("ControlOrMeta+Enter");
        await expect(page.getByRole("alert")).toContainText(PARSER_ERROR);
        expect(await violations(page), "with an error").toEqual([]);

        await page.getByRole("button", { name: "Save…" }).click();
        const save = page.getByRole("dialog", { name: "Save the query" });
        await expect(save.getByRole("textbox", { name: "Name" })).toBeVisible();
        expect(await violations(page), "the Save dialog").toEqual([]);
        await save.getByRole("button", { name: "Cancel" }).click();
        await expect(save).toBeHidden();

        await page.getByRole("link", { name: WEEKLY.name }).click();
        await expect(page.locator(".queries-name")).toHaveText(WEEKLY.name);
        await page.getByRole("button", { name: "Delete…" }).click();
        const remove = page.getByRole("dialog", { name: `Delete the saved query ${WEEKLY.name}?` });
        await expect(remove.getByRole("button", { name: "Delete" })).toBeVisible();
        expect(await violations(page), "the Delete dialog").toEqual([]);
        await remove.getByRole("button", { name: "Cancel" }).click();
        await expect(remove).toBeHidden();

        await page.getByRole("link", { name: DRAFT.name }).click();
        await expect(page.getByText("You may not read the SQL of this query.")).toBeVisible();
        expect(await violations(page), "a saved query without its SQL").toEqual([]);

        await page.setViewportSize({ width: 320, height: 640 });
        await openQueries(page);
        const fold = page.getByRole("button", { name: "Queries" });
        await expect(page.getByRole("complementary")).toHaveCount(0);
        expect(await violations(page), "folded at 320 px").toEqual([]);
        await fold.click();
        await expect(
          page.getByRole("dialog", { name: "Saved and recent queries" }).getByRole("link", {
            name: WEEKLY.name,
          }),
        ).toBeVisible();
        expect(await violations(page), "the fold open at 320 px").toEqual([]);
      });

      test("the whole screen works by keyboard alone: picker, editor, Run with Ctrl Enter, Cancel with Escape, the grid, downloads and the library", async ({
        page,
      }) => {
        await openQueries(page);

        // The picker: open with Enter, move to dev, choose it.
        const picker = page.getByRole("button", { name: "Environment: prod, protected" });
        await tabTo(page, picker);
        await page.keyboard.press("Enter");
        await expect(page.getByRole("option", { name: /prod/ })).toBeFocused();
        await page.keyboard.press("ArrowDown");
        await page.keyboard.press("Enter");
        const dev = page.getByRole("button", { name: "Environment: dev" });
        await expect(dev).toBeFocused();

        // The editor: type, run with Ctrl/⌘ Enter.
        await tabToEditor(page, "Tab");
        await page.keyboard.insertText(SALES_SQL);
        await page.keyboard.press("ControlOrMeta+Enter");
        await expect(page).toHaveURL(new RegExp(`/queries/dev/runs/${RUN_ID}$`));
        const grid = await resultGrid(page);
        await expect(page.getByRole("status").filter({ hasText: "3 rows · 2.4 s" })).toBeVisible();
        expect(sent).toEqual(["POST /api/v1/environments/dev/queries"]);

        // The grid is one Tab stop, and the arrow keys move inside it.
        await tabUntil(
          page,
          () => grid.evaluate((node) => node.contains(document.activeElement)),
          "Tab",
        );
        const inside = grid.locator(":focus");
        await expect(inside).toHaveCount(1);
        const first = await inside.textContent();
        await page.keyboard.press("ArrowDown");
        await page.keyboard.press("ArrowRight");
        await expect(inside).toHaveCount(1);
        expect(await inside.textContent()).not.toBe(first);

        // The downloads, above the grid: each saves the whole result under the run's name.
        await tabOnto(page.getByRole("button", { name: "Download CSV" }), "Shift+Tab");
        let download = page.waitForEvent("download");
        await page.keyboard.press("Enter");
        const csvFile = await download;
        expect(csvFile.suggestedFilename()).toMatch(
          new RegExp(`^query-${RUN_ID}-\\d{4}-\\d{2}-\\d{2}\\.csv$`),
        );
        expect(await readFile(await csvFile.path(), "utf8")).toBe(CSV);
        await tabOnto(page.getByRole("button", { name: "Download spreadsheet" }), "Tab");
        download = page.waitForEvent("download");
        await page.keyboard.press("Enter");
        const sheet = await download;
        expect(sheet.suggestedFilename()).toMatch(/\.xlsx$/);
        expect((await readFile(await sheet.path())).subarray(0, 2).toString()).toBe("PK");

        // Cancel with Escape: a run that goes on, cancelled from the editor above.
        await tabToEditor(page, "Shift+Tab");
        await page.keyboard.press("ControlOrMeta+A");
        await page.keyboard.press("Backspace");
        await page.keyboard.insertText(LONG_SQL);
        await page.keyboard.press("ControlOrMeta+Enter");
        await expect(page.getByRole("button", { name: "Cancel" })).toBeVisible();
        await expect(runLine(page)).toHaveText(/Running/);
        await expect(completionList(page)).toHaveCount(0);
        expect(await inEditor(page)).toBe(true);
        await page.keyboard.press("Escape");
        await expect(runLine(page)).toHaveText(/Cancelled/);
        expect(sent.at(-1)).toMatch(/^POST \/api\/v1\/environments\/dev\/queries\/[^/]+\/cancel$/);

        // The library, before the work: a saved query opens with Enter and brings its SQL and
        // environment.
        await tabOnto(page.getByRole("link", { name: WEEKLY.name }), "Shift+Tab");
        await page.keyboard.press("Enter");
        await expect(page).toHaveURL(new RegExp(`/queries/prod/saved/${WEEKLY.id}$`));
        await expect(page.locator(".queries-name")).toHaveText(WEEKLY.name);
        await expect(
          page.getByRole("button", { name: "Environment: prod, protected" }),
        ).toBeVisible();
        await expect(page.locator(".cm-content")).toContainText("GROUP BY store");
        expect(
          await page.evaluate(() => document.activeElement !== document.body),
          "the focus is never on <body>",
        ).toBe(true);
      });

      test("Escape closes the completion list first and cancels only on the next press", async ({
        page,
      }) => {
        const editor = await openQueries(page);
        await writeSql(page, editor, LONG_SQL);
        await page.keyboard.press("ControlOrMeta+Enter");
        const cancel = page.getByRole("button", { name: "Cancel" });
        await expect(cancel).toBeVisible();
        await expect(runLine(page)).toHaveText(/Running/);

        // A keyword's start opens the completion list.
        await page.keyboard.press("End");
        await page.keyboard.type(" SEL");
        await expect(completionList(page)).toBeVisible();
        await expect(completionList(page)).toContainText(/\bselect\b/i);

        await page.keyboard.press("Escape");
        await expect(completionList(page)).toHaveCount(0);
        // Two polls later the run still goes on, and no cancel was sent.
        await expect(runLine(page)).toHaveText(/Running/);
        await page.waitForTimeout(1_200);
        await expect(cancel).toBeVisible();
        expect(sent.filter((line) => line.endsWith("/cancel"))).toEqual([]);

        await page.keyboard.press("Escape");
        await expect(runLine(page)).toHaveText(/Cancelled/);
        expect(sent.filter((line) => line.endsWith("/cancel"))).toHaveLength(1);
      });

      test("a protected environment reads as protected in greyscale", async ({
        page,
        browserName,
      }) => {
        await openQueries(page);
        const picker = page.getByRole("button", { name: "Environment: prod, protected" });
        await expect(picker).toBeVisible();
        const word = picker.locator(".env-protected").filter({ hasText: "protected" });
        await expect(word).toBeVisible();
        await expect(word).toHaveText("· protected");
        await expect(picker).toContainText("● prod · protected");

        await showInGreyscale(page);
        const shot = `test-results/queries-protected-${browserName}-${theme}-${density}`;
        await page.locator(".queries-toolbar").screenshot({ path: `${shot}-grey.png` });
        await expect(word).toBeVisible();
        await picker.click();
        const option = page.getByRole("option", { name: /prod/ });
        await expect(
          option.locator(".env-protected").filter({ hasText: "protected" }),
        ).toBeVisible();
        await page.getByRole("listbox").screenshot({ path: `${shot}-options-grey.png` });
        await showInColour(page);
        await page.getByRole("listbox").screenshot({ path: `${shot}-options.png` });
      });

      test("no horizontal scroll at 320 px, with the library folded", async ({ page }) => {
        await page.setViewportSize({ width: 320, height: 640 });
        const editor = await openQueries(page);
        await expect(page.getByRole("complementary")).toHaveCount(0);
        const fold = page.getByRole("button", { name: "Queries" });
        await expect(fold).toBeVisible();
        expect(await sidewaysOverflow(page), "idle").toBeLessThanOrEqual(0);

        await writeSql(page, editor, SALES_SQL);
        await page.keyboard.press("ControlOrMeta+Enter");
        await resultGrid(page);
        expect(await sidewaysOverflow(page), "with a result").toBeLessThanOrEqual(0);

        await writeSql(page, editor, BROKEN_SQL);
        await page.keyboard.press("ControlOrMeta+Enter");
        await expect(page.getByRole("alert")).toContainText(PARSER_ERROR);
        expect(await sidewaysOverflow(page), "with an error").toBeLessThanOrEqual(0);

        await fold.click();
        const library = page.getByRole("dialog", { name: "Saved and recent queries" });
        await expect(library.getByRole("link", { name: WEEKLY.name })).toBeVisible();
        expect(await sidewaysOverflow(page), "the fold open").toBeLessThanOrEqual(0);
        const box = await library.boundingBox();
        expect((box?.x ?? -1) >= 0 && (box?.x ?? 0) + (box?.width ?? 0) <= 320).toBe(true);

        // A row opened from the fold closes it.
        await library.getByRole("link", { name: WEEKLY.name }).click();
        await expect(library).toBeHidden();
        await expect(page.locator(".queries-name")).toHaveText(WEEKLY.name);
        await page.getByRole("button", { name: "Save…" }).click();
        await expect(page.getByRole("dialog", { name: "Save the query" })).toBeVisible();
        expect(await sidewaysOverflow(page), "the Save dialog").toBeLessThanOrEqual(0);
      });

      test("opening /queries loads the editor chunk, and the first load does not", async ({
        page,
      }) => {
        const scripts: string[] = [];
        page.on("request", (request) => {
          if (request.resourceType() === "script") {
            scripts.push(new URL(request.url()).pathname);
          }
        });
        const editorChunk = (path: string): boolean => /\/SqlEditor-[^/]+\.js$/u.test(path);

        await page.goto("/");
        await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
        expect(scripts.filter(editorChunk), "the first load").toEqual([]);
        const before = scripts.length;

        await page
          .getByRole("navigation", { name: "Areas" })
          .getByRole("link", { name: "Queries" })
          .click();
        await expect(page.getByRole("textbox", { name: "SQL" })).toBeVisible();
        expect(scripts.slice(before).filter(editorChunk), "opening Queries").toHaveLength(1);
        // Format's own chunk waits for Format.
        expect(scripts.filter((path) => /\/formatSql-[^/]+\.js$/u.test(path))).toEqual([]);
      });

      test("a failed run marks its line and column, drawn under the content security policy", async ({
        page,
      }) => {
        await recordCspViolations(page);
        const errors = consoleErrors(page);
        const editor = await openQueries(page);
        await writeSql(page, editor, BROKEN_SQL);
        await page.keyboard.press("ControlOrMeta+Enter");
        const alert = page.getByRole("alert");
        await expect(alert).toContainText(PARSER_ERROR);

        // The underline runs over the word at line 2, column 1; the gutter marks the line.
        const range = page.locator(".cm-lintRange-error");
        await expect(range).toHaveText("FROM");
        const marker = page.locator(".cm-gutter-lint .cm-lint-marker-error");
        await expect(marker).toBeVisible();
        const looks = await page.evaluate(() => {
          const host = document.querySelector(".sql-editor")?.shadowRoot;
          const underline = host?.querySelector(".cm-lintRange-error");
          const mark = host?.querySelector(".cm-lint-marker-error");
          if (!underline || !mark) {
            return null;
          }
          const line = getComputedStyle(underline);
          const dot = getComputedStyle(mark);
          return {
            image: line.backgroundImage,
            decoration: line.textDecorationLine,
            content: dot.content,
            width: mark.getBoundingClientRect().width,
          };
        });
        expect(looks?.image).toBe("none");
        expect(looks?.decoration).toBe("underline");
        expect(looks?.content).not.toContain("url(");
        expect(looks?.width).toBeGreaterThan(0);

        // Go to moves the cursor there; an edit takes the mark away.
        await alert.getByRole("button", { name: "Go to line 2, column 1" }).click();
        expect(await inEditor(page)).toBe(true);
        await page.keyboard.type(" ");
        await expect(range).toHaveCount(0);
        await expect(marker).toHaveCount(0);
        expect(await cspViolations(page)).toEqual([]);
        expect(errors).toEqual([]);
      });

      test("the screen asks only for stubbed routes and logs no console error and no CSP violation", async ({
        page,
      }) => {
        await recordCspViolations(page);
        const errors = consoleErrors(page);
        const requests: string[] = [];
        const open = new Set<Request>();
        page.on("request", (request) => {
          requests.push(request.url());
          open.add(request);
        });
        page.on("requestfinished", (request) => open.delete(request));
        page.on("requestfailed", (request) => open.delete(request));

        const editor = await openQueries(page);
        await writeSql(page, editor, SALES_SQL);
        await page.keyboard.press("ControlOrMeta+Enter");
        await resultGrid(page);
        const download = page.waitForEvent("download");
        await page.getByRole("button", { name: "Download CSV" }).click();
        await download;

        await writeSql(page, editor, BROKEN_SQL);
        await page.keyboard.press("ControlOrMeta+Enter");
        await expect(page.getByRole("alert")).toContainText(PARSER_ERROR);
        // Recent lists the run and how it ended, with no save, delete or reload in between.
        await expect(page).toHaveURL(/\/queries\/prod\/runs\//u);
        const brokenRun = new URL(page.url()).pathname;
        const recent = page.getByRole("region", { name: "Recent queries" });
        await expect(
          recent.getByRole("row").filter({ has: page.locator(`a[href="${brokenRun}"]`) }),
        ).toContainText("Failed");

        // Save as new, then delete it again.
        await page.getByRole("button", { name: "Save…" }).click();
        const save = page.getByRole("dialog", { name: "Save the query" });
        await save.getByRole("textbox", { name: "Name" }).fill("Broken on purpose");
        await save.getByRole("button", { name: "Save as new" }).click();
        await expect(page.locator(".queries-name")).toHaveText("Broken on purpose");
        await page.getByRole("button", { name: "Delete…" }).click();
        await page
          .getByRole("dialog", { name: "Delete the saved query Broken on purpose?" })
          .getByRole("button", { name: "Delete" })
          .click();
        await expect(
          page.getByRole("status").filter({ hasText: "Deleted Broken on purpose." }),
        ).toBeFocused();

        // The run again from Recent, with its result.
        await recent.getByRole("link").first().click();
        await expect(page).toHaveURL(new RegExp(`${brokenRun}$`, "u"));
        await expect(page.getByRole("alert")).toContainText(PARSER_ERROR);
        await expect.poll(() => open.size).toBe(0);

        const api = requests
          .filter((url) => url.includes("/api/"))
          .map((url) => new URL(url).pathname);
        expect(api).toContain("/api/v1/queries/environments");
        expect(api.filter((path) => !QUERIES_STUBBED.includes(path))).toEqual([]);
        expect(sent).toEqual([
          "POST /api/v1/environments/prod/queries",
          "POST /api/v1/environments/prod/queries",
          "POST /api/v1/environments/prod/saved-queries",
          expect.stringMatching(/^DELETE \/api\/v1\/environments\/prod\/saved-queries\//u),
        ]);
        expect(await cspViolations(page)).toEqual([]);
        expect(errors).toEqual([]);
      });
    });
  }
}
