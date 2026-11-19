import { readFile } from "node:fs/promises";

import { expect, type Locator, type Page, test } from "@playwright/test";

import { cspViolations, recordCspViolations } from "../e2e/csp";
import {
  administrator,
  createEnvironment,
  grantOn,
  invitedPerson,
  queryStatus,
  savedQueriesAs,
  skipTheWelcome,
  startWorker,
  uniqueName,
} from "./helpers";

// The queries here run: the file starts a worker of its own on the server's database, and stops
// it when the file is done, so the other files keep a server without one.
let stopWorker: (() => Promise<void>) | null = null;

test.beforeAll(async ({ request }, testInfo) => {
  testInfo.setTimeout(120_000);
  stopWorker = await startWorker(request);
});

test.afterAll(async () => {
  test.setTimeout(60_000);
  await stopWorker?.();
});

/**
 * Opens the Queries screen and waits for the editor.
 *
 * @param page - The page.
 * @param path - The address.
 * @returns The editor's text box.
 */
async function openQueries(page: Page, path = "/queries"): Promise<Locator> {
  await page.goto(path);
  const editor = page.getByRole("textbox", { name: "SQL" });
  await expect(editor).toBeVisible();
  return editor;
}

/**
 * Chooses an environment in the picker with the pointer.
 *
 * @param page - The page.
 * @param environment - The environment.
 */
async function choose(page: Page, environment: string): Promise<void> {
  await page.getByRole("button", { name: /^Environment: / }).click();
  await page.getByRole("option", { name: environment, exact: true }).click();
  await expect(page.getByRole("button", { name: `Environment: ${environment}` })).toBeVisible();
}

/**
 * Replaces the editor's text, a line at a time with Enter between (see `e2e/queries.spec.ts`).
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
 * Runs the SQL in the environment and waits for its address.
 *
 * @param page - The page, on the Queries screen.
 * @param environment - The environment.
 * @param sql - The SQL.
 * @returns The run's id.
 */
async function run(page: Page, environment: string, sql: string): Promise<string> {
  const editor = await openQueries(page);
  await choose(page, environment);
  await writeSql(page, editor, sql);
  await page.keyboard.press("ControlOrMeta+Enter");
  await expect(page).toHaveURL(new RegExp(`/queries/${environment}/runs/[0-9a-f-]+$`));
  return new URL(page.url()).pathname.split("/").at(-1) ?? "";
}

/**
 * The result's grid of a run, once its rows are read (a worker takes a moment to start it).
 *
 * @param page - The page.
 * @param id - The run's id.
 * @returns The grid.
 */
async function resultGrid(page: Page, id: string): Promise<Locator> {
  const grid = page.getByRole("grid", { name: `Result of Query ${id.slice(0, 8)}, rows` });
  await expect(grid).toBeVisible({ timeout: 30_000 });
  return grid;
}

/**
 * Saves the editor's query under a name, with a visibility, as a new saved query.
 *
 * @param page - The page.
 * @param name - The name.
 * @param everyone - Whether everyone who may query the environment may run it.
 * @param environment - The environment, which the visibility names.
 */
async function saveAsNew(
  page: Page,
  name: string,
  everyone: boolean,
  environment: string,
): Promise<void> {
  await page.getByRole("button", { name: "Save…" }).click();
  const dialog = page.getByRole("dialog", { name: "Save the query" });
  await dialog.getByRole("textbox", { name: "Name" }).fill(name);
  // The radio's own input is hidden under its drawn circle; a person presses the words.
  const visibility = everyone ? `Everyone who can run queries in ${environment}` : "Only me";
  await dialog.getByText(visibility, { exact: true }).click();
  await expect(dialog.getByRole("radio", { name: visibility })).toBeChecked();
  await dialog.getByRole("button", { name: "Save as new" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.locator(".queries-name")).toHaveText(name);
}

test("a person runs SELECT 42 AS answer in dev, sees it in the grid, and downloads it as CSV and as a spreadsheet", async ({
  browser,
  request,
}) => {
  const dev = uniqueName("dev");
  await createEnvironment(request, dev);
  const page = await administrator(browser, request);

  const id = await run(page, dev, "SELECT 42 AS answer");
  const grid = await resultGrid(page, id);
  await expect(grid.getByRole("columnheader", { name: /answer/ })).toBeVisible();
  await expect(grid.getByRole("gridcell", { name: "42" })).toBeVisible();
  await expect(page.locator(".queries-result-line")).toHaveText(/^1 row · /);

  let download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download CSV" }).click();
  const csv = await download;
  expect(csv.suggestedFilename()).toMatch(new RegExp(`^query-${id}-\\d{4}-\\d{2}-\\d{2}\\.csv$`));
  // The server quotes every name in the header row.
  expect(await readFile(await csv.path(), "utf8")).toBe('"answer"\r\n42\r\n');

  download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download spreadsheet" }).click();
  const sheet = await download;
  expect(sheet.suggestedFilename()).toMatch(/\.xlsx$/);
  expect((await readFile(await sheet.path())).subarray(0, 2).toString()).toBe("PK");
});

test("a SQL error marks its line and column", async ({ browser, request }) => {
  const environment = uniqueName("err");
  await createEnvironment(request, environment);
  const page = await administrator(browser, request);
  await recordCspViolations(page);
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      errors.push(message.text());
    }
  });
  page.on("pageerror", (error) => errors.push(error.message));

  await run(page, environment, "SELECT 1\nFROM");
  const alert = page.getByRole("alert");
  const goTo = alert.getByRole("button", { name: /^Go to line 2, column \d+$/ });
  await expect(goTo).toBeVisible({ timeout: 30_000 });
  await expect(page.locator(".queries-run-line")).toHaveText(/Failed/);

  // The underline and the gutter's marker, drawn in the editor's shadow root under the policy.
  const range = page.locator(".cm-lintRange-error");
  await expect(range).toBeVisible();
  await expect(page.locator(".cm-gutter-lint .cm-lint-marker-error")).toBeVisible();
  expect(await range.evaluate((node) => getComputedStyle(node).backgroundImage)).toBe("none");

  await goTo.click();
  expect(
    await page.evaluate(() => document.activeElement?.classList.contains("sql-editor") === true),
  ).toBe(true);
  expect(await cspViolations(page)).toEqual([]);
  expect(errors).toEqual([]);
});

test("a saved query shared with everyone is seen and opened by a second person who may query, and not by one who may not", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
  // Two or three people sign in with a passkey each.
  test.setTimeout(120_000);
  const environment = uniqueName("shared");
  await createEnvironment(request, environment);
  const name = uniqueName("Stores by region");
  const sql = "SELECT 'north' AS region";
  const owner = await administrator(browser, request);
  const editor = await openQueries(owner);
  await choose(owner, environment);
  await writeSql(owner, editor, sql);
  await saveAsNew(owner, name, true, environment);

  const reader = await invitedPerson(browser, request, (person) =>
    grantOn(request, person, "SUBMIT_QUERY", environment),
  );
  await skipTheWelcome(reader.page);
  await openQueries(reader.page);
  const row = reader.page.getByRole("region", { name: "Saved queries" }).getByRole("row", {
    name: new RegExp(name),
  });
  await expect(row).toContainText("everyone");
  await row.getByRole("link", { name }).click();
  await expect(reader.page.locator(".queries-name")).toHaveText(name);
  await expect(reader.page.locator(".cm-content")).toHaveText(sql);
  // Not their own: only Save as new, no Delete….
  await expect(reader.page.getByRole("button", { name: "Delete…" })).toHaveCount(0);

  const outsider = await invitedPerson(browser, request, (person) =>
    grantOn(request, person, "READ_ENVIRONMENT", environment),
  );
  await skipTheWelcome(outsider.page);
  await outsider.page.goto("/queries");
  await expect(
    outsider.page.getByText("You may not run queries in any environment yet.", { exact: false }),
  ).toBeVisible();
  // Not even through the API: one who may not query the environment is refused its list.
  expect(await savedQueriesAs(outsider.page, environment)).toEqual({ status: 403, names: [] });
});

test("Recent shows the run again after a reload, with its result", async ({ browser, request }) => {
  const environment = uniqueName("recent");
  await createEnvironment(request, environment);
  const page = await administrator(browser, request);
  const id = await run(page, environment, "SELECT 42 AS answer");
  await resultGrid(page, id);

  await openQueries(page);
  await page.reload();
  const recent = page.getByRole("region", { name: "Recent queries" });
  const link = recent.locator(`a[href$="/queries/${environment}/runs/${id}"]`);
  await expect(link).toBeVisible();
  await expect(
    recent.getByRole("row").filter({
      has: page.locator(`a[href$="/queries/${environment}/runs/${id}"]`),
    }),
  ).toContainText("Succeeded");
  await link.click();
  const grid = await resultGrid(page, id);
  await expect(grid.getByRole("gridcell", { name: "42" })).toBeVisible();
  await expect(page.locator(".cm-content")).toHaveText("SELECT 42 AS answer");
});

test("an administrator sees another's private saved query without its SQL and deletes it", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
  // Two or three people sign in with a passkey each.
  test.setTimeout(120_000);
  const environment = uniqueName("private");
  await createEnvironment(request, environment);
  const name = uniqueName("My draft");
  const author = await invitedPerson(browser, request, (person) =>
    grantOn(request, person, "SUBMIT_QUERY", environment),
  );
  await skipTheWelcome(author.page);
  const editor = await openQueries(author.page);
  await choose(author.page, environment);
  await writeSql(author.page, editor, "SELECT 'secret' AS word");
  await saveAsNew(author.page, name, false, environment);

  const page = await administrator(browser, request);
  await openQueries(page);
  const row = page.getByRole("region", { name: "Saved queries" }).getByRole("row", {
    name: new RegExp(name),
  });
  await expect(row).toContainText(`by ${author.name}`);
  await row.getByRole("link", { name }).click();
  await expect(page.locator(".queries-name")).toHaveText(name);
  await expect(page.getByText("You may not read the SQL of this query.")).toBeVisible();
  await expect(page.locator(".cm-content")).toHaveText("");
  await expect(page.getByRole("button", { name: "Run" })).toBeDisabled();

  await page.getByRole("button", { name: "Delete…" }).click();
  await page
    .getByRole("dialog", { name: `Delete the saved query ${name}?` })
    .getByRole("button", { name: "Delete" })
    .click();
  await expect(page).toHaveURL(/\/queries$/);
  await expect(page.getByRole("status").filter({ hasText: `Deleted ${name}.` })).toBeFocused();
  await expect(
    page.getByRole("region", { name: "Saved queries" }).getByRole("link", { name }),
  ).toHaveCount(0);
  expect(await savedQueriesAs(author.page, environment)).toEqual({ status: 200, names: [] });
});

test("a long query goes on while the page is left, shows as running in Recent, and Cancel there stops it", async ({
  browser,
  request,
}) => {
  test.setTimeout(120_000);
  const environment = uniqueName("long");
  await createEnvironment(request, environment);
  const page = await administrator(browser, request);
  const id = await run(page, environment, "SELECT sum(n) FROM range(100000000000) t(n)");
  await expect(page.getByRole("button", { name: "Cancel", exact: true })).toBeVisible();
  await expect(page.locator(".queries-run-line")).toHaveText(/Running/u, { timeout: 30_000 });

  // Leaving the page does not cancel: Recent lists the run as running.
  await page.getByRole("navigation", { name: "Areas" }).getByRole("link", { name: "Home" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
  await page
    .getByRole("navigation", { name: "Areas" })
    .getByRole("link", { name: "Queries" })
    .click();
  const recent = page.getByRole("region", { name: "Recent queries" });
  const row = recent
    .getByRole("row")
    .filter({ has: page.locator(`a[href$="/queries/${environment}/runs/${id}"]`) });
  await expect(row).toContainText("Running");
  await row.getByRole("link").click();
  await expect(page).toHaveURL(new RegExp(`/queries/${environment}/runs/${id}$`, "u"));
  await expect(page.locator(".queries-run-line")).toHaveText(/Running/u);

  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("The query was cancelled.", {
    timeout: 30_000,
  });
  await expect(page.getByRole("button", { name: "Run" })).toBeFocused();
  expect(await queryStatus(request, environment, id)).toBe("CANCELLED");
});
