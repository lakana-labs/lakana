import { expect, type Locator, type Page, type Request, test } from "@playwright/test";

import { showInColour, showInGreyscale } from "./colour-vision";
import {
  CONNECTIONS_STUBBED,
  CREATED,
  DEV,
  EXPORTS,
  LEGACY,
  NIGHTLY,
  PROD,
  SCRATCH,
  stubConnections,
  WAREHOUSE,
} from "./connections";
import { cspViolations, recordCspViolations } from "./csp";
import { addAdminPasskey, ORIGIN } from "./manage";
import { consoleErrors, tabTo, violations } from "./session";

const KEY = "lakana.preferences";

/** The nine kinds as the Add form names them, in its order. */
const KINDS = [
  "PostgreSQL",
  "MariaDB",
  "SQLite file",
  "HTTP API",
  "S3",
  "Google Cloud Storage",
  "Azure Blob Storage",
  "Directory",
  "TCP",
];

/**
 * Opens the Connections list and waits for its rows.
 *
 * @param page - The page.
 * @param path - The address.
 */
async function openList(page: Page, path = "/connections"): Promise<void> {
  await page.goto(path);
  await expect(page.getByRole("heading", { level: 1, name: "Connections" })).toBeVisible();
  await expect(page.getByRole("link", { name: EXPORTS, exact: true })).toBeVisible();
}

/**
 * Opens a connection's page and waits for its heading.
 *
 * @param page - The page.
 * @param environment - The environment.
 * @param name - The connection.
 * @param path - What follows its address, `/edit` for Edit.
 */
async function openConnection(
  page: Page,
  environment: string,
  name: string,
  path = "",
): Promise<void> {
  await page.goto(`/connections/${environment}/${name}${path}`);
  await expect(
    page.getByRole("heading", { level: 1, name: path === "/edit" ? "Edit" : name }),
  ).toBeVisible();
}

/**
 * The row of the list that names a connection.
 *
 * @param page - The page.
 * @param name - The connection.
 * @returns The row.
 */
function rowOf(page: Page, name: string): Locator {
  return page.getByRole("row").filter({ has: page.getByRole("link", { name, exact: true }) });
}

/**
 * Chooses a kind in the Add form by its words, as a person presses them.
 *
 * @param page - The page.
 * @param kind - The kind's name.
 */
async function chooseKind(page: Page, kind: string): Promise<void> {
  const group = page.getByRole("radiogroup", { name: "Kind" });
  await group.getByText(kind, { exact: true }).click();
  await expect(group.getByRole("radio", { name: new RegExp(`^${kind}`, "u") })).toBeChecked();
}

/**
 * Opens the Add form and waits for it.
 *
 * @param page - The page.
 */
async function openAdd(page: Page): Promise<void> {
  await page.goto("/connections/new");
  await expect(page.getByRole("heading", { level: 1, name: "Add connection" })).toBeVisible();
  await expect(page.getByRole("radiogroup", { name: "Kind" })).toBeVisible();
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
    test.describe(`the Connections screen, ${theme}, ${density}`, () => {
      test.beforeEach(async ({ page }) => {
        await page.addInitScript(
          ([key, value]) => {
            if (window.localStorage.getItem(key ?? "") === null) {
              window.localStorage.setItem(key ?? "", value ?? "");
            }
          },
          [KEY, JSON.stringify({ theme, density, language: "en" })],
        );
      });

      test("axe finds nothing on the list, a page, Add with each kind, Edit, and each dialog, and at 320 px", async ({
        page,
      }) => {
        await stubConnections(page);
        await openList(page);
        expect(await violations(page), "the list").toEqual([]);

        await openConnection(page, DEV, EXPORTS);
        await expect(page.getByText("credentials", { exact: false }).first()).toBeVisible();
        expect(await violations(page), "a page").toEqual([]);

        await openConnection(page, PROD, WAREHOUSE);
        expect(await violations(page), "a reader's page").toEqual([]);

        await openAdd(page);
        for (const kind of KINDS) {
          await chooseKind(page, kind);
          expect(await violations(page), `Add, ${kind}`).toEqual([]);
        }

        await openConnection(page, DEV, EXPORTS, "/edit");
        await expect(page.getByRole("button", { name: "Save" })).toBeVisible();
        expect(await violations(page), "Edit").toEqual([]);

        await openConnection(page, DEV, EXPORTS);
        await page.getByRole("button", { name: "Delete…" }).click();
        const inUse = page.getByRole("dialog", { name: `${EXPORTS} is still in use` });
        await expect(inUse.getByRole("button", { name: "Close" })).toBeVisible();
        expect(await violations(page), "the dialog of a connection in use").toEqual([]);
        await inUse.getByRole("button", { name: "Close" }).click();
        await expect(inUse).toBeHidden();

        await openConnection(page, DEV, SCRATCH);
        await page.getByRole("button", { name: "Delete…" }).click();
        const remove = page.getByRole("dialog", {
          name: `Delete the connection ${SCRATCH} in ${DEV}?`,
        });
        await expect(remove.getByRole("button", { name: "Delete" })).toBeVisible();
        expect(await violations(page), "the Delete dialog").toEqual([]);
        await remove.getByRole("button", { name: "Cancel" }).click();
        await expect(remove).toBeHidden();

        await page.setViewportSize({ width: 320, height: 640 });
        await openList(page);
        expect(await violations(page), "the list at 320 px").toEqual([]);
        await openConnection(page, DEV, EXPORTS);
        expect(await violations(page), "a page at 320 px").toEqual([]);
        await openAdd(page);
        expect(await violations(page), "Add at 320 px").toEqual([]);
        await openConnection(page, DEV, EXPORTS, "/edit");
        expect(await violations(page), "Edit at 320 px").toEqual([]);
      });

      test("the whole screen works by keyboard alone: filter, rows, Test, Test all, Add with a kind and a secret, Edit and Delete", async ({
        page,
        context,
        browserName,
      }) => {
        test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
        const stub = await stubConnections(page);
        await addAdminPasskey(context, page);
        await openList(page, `${ORIGIN}/connections`);

        // The filter: open with Enter, move to dev, choose it.
        const filter = page.getByRole("button", { name: "Environment: All environments" });
        await tabTo(page, filter);
        await page.keyboard.press("Enter");
        await expect(page.getByRole("option", { name: "All environments" })).toBeFocused();
        await page.keyboard.press("ArrowDown");
        await page.keyboard.press("Enter");
        await expect(page.getByRole("button", { name: `Environment: ${DEV}` })).toBeFocused();
        await expect(page.getByRole("link", { name: WAREHOUSE })).toHaveCount(0);

        // Test on a row, with Enter; the row says it is testing and the button waits.
        await tabTo(page, page.getByRole("button", { name: `Test ${SCRATCH}` }));
        await page.keyboard.press("Enter");
        await expect(rowOf(page, SCRATCH)).toContainText("queued");
        await expect(rowOf(page, SCRATCH).getByRole("button", { name: /^Test/ })).toBeDisabled();
        expect(await violations(page), "a test queued").toEqual([]);

        // Test all, with Space, and its status line.
        const all = page.getByRole("button", { name: `Test all in ${DEV}` });
        await tabTo(page, all);
        await page.keyboard.press("Space");
        await expect(
          page.getByRole("status").filter({ hasText: /^Testing \d+ connections/u }),
        ).toBeVisible();
        await expect(
          page
            .getByRole("status")
            .filter({ hasText: `Tested 4 connections in ${DEV}: 4 OK, 0 not OK.` }),
        ).toBeVisible({ timeout: 15_000 });

        // A row opens with Enter on its name, and Edit with Enter; Save sends the change.
        await tabTo(page, page.getByRole("link", { name: SCRATCH, exact: true }));
        await page.keyboard.press("Enter");
        await expect(page.getByRole("heading", { level: 1, name: SCRATCH })).toBeVisible();
        await tabTo(page, page.getByRole("link", { name: "Edit" }));
        await page.keyboard.press("Enter");
        // The form's chunk is loaded now for the first time; the heading holds the focus meanwhile.
        await expect(page.getByRole("heading", { level: 1, name: "Edit" })).toBeFocused();
        await expect(page.getByRole("button", { name: "Save" })).toBeVisible();
        const path = page.getByRole("textbox", { name: "path" });
        await tabTo(page, path);
        await page.keyboard.press("End");
        await page.keyboard.type("-two");
        await tabTo(page, page.getByRole("button", { name: "Save" }));
        await page.keyboard.press("Enter");
        const saved = page.getByRole("status").filter({ hasText: `Saved ${SCRATCH}.` });
        await expect(saved).toBeFocused();
        // The touch is asked for once, and the same change is sent again after it.
        const change = {
          method: "PATCH",
          path: `/api/v1/environments/${DEV}/connections/${SCRATCH}`,
          body: { address: { path: "scratch-two" } },
        };
        expect(stub.requests.filter(({ method }) => method === "PATCH")).toEqual([change, change]);

        // Delete with its question, by keyboard.
        await tabTo(page, page.getByRole("button", { name: "Delete…" }));
        await page.keyboard.press("Enter");
        const remove = page.getByRole("dialog", {
          name: `Delete the connection ${SCRATCH} in ${DEV}?`,
        });
        await expect(remove).toBeVisible();
        await tabTo(page, remove.getByRole("button", { name: "Delete" }));
        await page.keyboard.press("Enter");
        await expect(page).toHaveURL(`${ORIGIN}/connections`);
        await expect(
          page.getByRole("status").filter({ hasText: `Deleted ${SCRATCH}.` }),
        ).toBeFocused();
        await expect(page.getByRole("link", { name: SCRATCH, exact: true })).toHaveCount(0);

        // Add: a kind with the arrow keys, the fields and the secret typed, Save with Enter.
        await tabTo(page, page.getByRole("link", { name: "Add connection" }));
        await page.keyboard.press("Enter");
        await expect(page.getByRole("heading", { level: 1, name: "Add connection" })).toBeFocused();
        const kinds = page.getByRole("radiogroup", { name: "Kind" });
        await tabTo(page, kinds.getByRole("radio", { name: /^PostgreSQL/u }));
        await expect(kinds.getByRole("radio", { name: /^PostgreSQL/u })).toBeChecked();
        for (const [label, text] of [
          ["Name", CREATED],
          ["host", "db.example.com"],
          ["database", "events"],
          ["user", "loader"],
        ] as const) {
          const field = page.getByRole("textbox", { name: label, exact: true });
          await tabTo(page, field);
          await page.keyboard.type(text);
        }
        const password = page.getByLabel("password", { exact: true });
        await tabTo(page, password);
        await page.keyboard.type("s3cret-value");
        await page.keyboard.press("Enter");
        await expect(page).toHaveURL(`${ORIGIN}/connections/${DEV}/${CREATED}`);
        await expect(
          page.getByRole("status").filter({ hasText: `Created ${CREATED}.` }),
        ).toBeFocused();
        await expect(page.getByRole("button", { name: "Test now" })).toBeVisible();
        // The password went in the create and nowhere else; it is not on the page now.
        const create = stub.requests.filter(({ path }) => path.endsWith(`/${DEV}/connections`));
        expect(create).toHaveLength(2);
        expect(JSON.stringify(create[1]?.body)).toContain("s3cret-value");
        expect(
          stub.requests.filter(({ body }) => JSON.stringify(body ?? "").includes("s3cret-value")),
        ).toEqual(create);
        expect(await page.content()).not.toContain("s3cret-value");
      });

      test("the status reads in greyscale", async ({ page }) => {
        await stubConnections(page);
        await openList(page);
        // The test of nightly is running at first, and done after its third read.
        await expect(rowOf(page, NIGHTLY)).toContainText("testing…");
        await expect(rowOf(page, EXPORTS)).toContainText(/credentials · /u);
        await expect(rowOf(page, SCRATCH)).toContainText("never tested");
        await expect(rowOf(page, LEGACY)).toContainText(/tested before the last change · /u);
        await expect(rowOf(page, WAREHOUSE)).toContainText(/OK · /u);

        await showInGreyscale(page);
        const filter = await page.evaluate(() => document.documentElement.style.filter);
        expect(filter).toBe("grayscale(1)");
        // Each status keeps its word, and a state with a glyph keeps it beside the word.
        await expect(rowOf(page, EXPORTS)).toContainText("credentials");
        await expect(rowOf(page, EXPORTS).locator(".status-glyph-failed")).toBeVisible();
        await expect(rowOf(page, WAREHOUSE).locator(".status-glyph-succeeded")).toBeVisible();
        await expect(rowOf(page, SCRATCH)).toContainText("never tested");
        await expect(rowOf(page, LEGACY)).toContainText("tested before the last change");
        // The running one has gone on to done by now, and says so in words too.
        await expect(rowOf(page, NIGHTLY)).toContainText(/testing…|OK · /u);
        await showInColour(page);
      });

      test("no horizontal scroll at 320 px, with where and used by under the name", async ({
        page,
      }) => {
        await stubConnections(page);
        await page.setViewportSize({ width: 320, height: 640 });
        await openList(page);
        expect(await sidewaysOverflow(page), "the list").toBeLessThanOrEqual(0);
        await expect(page.getByRole("columnheader", { name: "Where" })).toBeHidden();
        await expect(page.getByRole("columnheader", { name: "Used by" })).toBeHidden();
        // Where the connection points and what uses it follow its name.
        await expect(rowOf(page, EXPORTS).locator(".narrow-only")).toContainText(
          "s3://acme-exports (eu-central-1) · 2 assets",
        );
        await expect(rowOf(page, EXPORTS).locator(".narrow-only")).toBeVisible();

        await openConnection(page, DEV, EXPORTS);
        expect(await sidewaysOverflow(page), "a page").toBeLessThanOrEqual(0);
        await openAdd(page);
        expect(await sidewaysOverflow(page), "Add").toBeLessThanOrEqual(0);
        for (const kind of KINDS) {
          await chooseKind(page, kind);
          expect(await sidewaysOverflow(page), `Add, ${kind}`).toBeLessThanOrEqual(0);
        }
        await openConnection(page, DEV, EXPORTS, "/edit");
        expect(await sidewaysOverflow(page), "Edit").toBeLessThanOrEqual(0);
        await openConnection(page, DEV, EXPORTS);
        await page.getByRole("button", { name: "Delete…" }).click();
        const dialog = page.getByRole("dialog");
        await expect(dialog).toBeVisible();
        expect(await sidewaysOverflow(page), "the dialog").toBeLessThanOrEqual(0);
        const box = await dialog.boundingBox();
        expect((box?.x ?? -1) >= 0 && (box?.x ?? 0) + (box?.width ?? 0) <= 320).toBe(true);
      });

      test("with the room of a desktop, where and used by have their own columns", async ({
        page,
      }) => {
        await stubConnections(page);
        await openList(page);
        await expect(page.getByRole("columnheader", { name: "Where" })).toBeVisible();
        await expect(page.getByRole("columnheader", { name: "Used by" })).toBeVisible();
        await expect(rowOf(page, EXPORTS).locator(".narrow-only")).toBeHidden();
      });

      test("opening /connections loads its chunk, and the first load does not", async ({
        page,
      }) => {
        await stubConnections(page);
        const scripts: string[] = [];
        page.on("request", (request) => {
          if (request.resourceType() === "script") {
            scripts.push(new URL(request.url()).pathname);
          }
        });
        const chunk = (name: string) => (path: string) =>
          new RegExp(`/${name}-[^/]+\\.js$`, "u").test(path);

        await page.goto("/");
        await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
        expect(scripts.filter(chunk("ConnectionsScreen")), "the first load").toEqual([]);
        expect(scripts.filter(chunk("ConnectionForm")), "the first load").toEqual([]);
        const before = scripts.length;

        await page
          .getByRole("navigation", { name: "Areas" })
          .getByRole("link", { name: "Connections" })
          .click();
        await expect(page.getByRole("link", { name: EXPORTS, exact: true })).toBeVisible();
        expect(scripts.slice(before).filter(chunk("ConnectionsScreen")), "the list").toHaveLength(
          1,
        );
        // The form's chunk waits for Add and Edit.
        expect(scripts.filter(chunk("ConnectionForm")), "the list").toEqual([]);

        await page.getByRole("link", { name: "Add connection" }).click();
        await expect(page.getByRole("radiogroup", { name: "Kind" })).toBeVisible();
        expect(scripts.filter(chunk("ConnectionForm")), "Add").toHaveLength(1);
      });

      test("the screen asks only for stubbed routes and logs no console error and no CSP violation", async ({
        page,
        context,
        browserName,
      }) => {
        test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
        const stub = await stubConnections(page);
        await addAdminPasskey(context, page);
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

        await openList(page, `${ORIGIN}/connections`);
        // A Test on a row, then the running one turns done by its reads.
        await rowOf(page, SCRATCH)
          .getByRole("button", { name: `Test ${SCRATCH}` })
          .click();
        await expect(rowOf(page, SCRATCH)).toContainText(/OK · /u, { timeout: 15_000 });
        await expect(rowOf(page, NIGHTLY)).toContainText(/OK · /u, { timeout: 15_000 });
        expect(stub.reads(DEV, NIGHTLY), "nightly was read").toBeGreaterThanOrEqual(3);

        // Test all in one environment.
        await page.getByRole("button", { name: /^Environment: / }).click();
        await page.getByRole("option", { name: DEV, exact: true }).click();
        await page.getByRole("button", { name: `Test all in ${DEV}` }).click();
        await expect(
          page.getByRole("status").filter({ hasText: /^Tested 4 connections/u }),
        ).toBeVisible({ timeout: 15_000 });

        // A page, an edit with its touch, and a delete.
        await rowOf(page, SCRATCH).getByRole("link", { name: SCRATCH, exact: true }).click();
        await page.getByRole("link", { name: "Edit" }).click();
        await page.getByRole("textbox", { name: "path" }).fill("scratch-two");
        await page.getByRole("button", { name: "Save" }).click();
        await expect(
          page.getByRole("status").filter({ hasText: `Saved ${SCRATCH}.` }),
        ).toBeVisible();
        await page.getByRole("button", { name: "Delete…" }).click();
        await page
          .getByRole("dialog", { name: `Delete the connection ${SCRATCH} in ${DEV}?` })
          .getByRole("button", { name: "Delete" })
          .click();
        await expect(page).toHaveURL(`${ORIGIN}/connections`);

        // Add, and its Test now.
        await page.getByRole("link", { name: "Add connection" }).click();
        await chooseKind(page, "Directory");
        await page.getByRole("textbox", { name: "Name" }).fill(CREATED);
        await page.getByRole("textbox", { name: "root" }).fill("landing");
        await page.getByRole("textbox", { name: "path" }).fill("events");
        await page.getByRole("button", { name: "Save" }).click();
        await expect(page).toHaveURL(`${ORIGIN}/connections/${DEV}/${CREATED}`);
        await page.getByRole("button", { name: "Test now" }).click();
        await expect(page.getByRole("button", { name: "Testing…" })).toBeVisible();
        await expect.poll(() => open.size, { timeout: 15_000 }).toBeLessThanOrEqual(1);

        const api = requests
          .filter((url) => url.includes("/api/"))
          .map((url) => new URL(url).pathname);
        expect(api).toContain("/api/v1/connections/environments");
        expect(api.filter((path) => !CONNECTIONS_STUBBED.includes(path))).toEqual([]);
        expect(stub.sent).toEqual([
          `POST /api/v1/environments/${DEV}/connections/${SCRATCH}/tests`,
          `POST /api/v1/environments/${DEV}/connection-tests`,
          `PATCH /api/v1/environments/${DEV}/connections/${SCRATCH}`,
          "POST /api/v1/sign-in/step-up/options",
          "POST /api/v1/sign-in/step-up",
          `PATCH /api/v1/environments/${DEV}/connections/${SCRATCH}`,
          `DELETE /api/v1/environments/${DEV}/connections/${SCRATCH}`,
          `POST /api/v1/environments/${DEV}/connections`,
          "POST /api/v1/sign-in/step-up/options",
          "POST /api/v1/sign-in/step-up",
          `POST /api/v1/environments/${DEV}/connections`,
          `POST /api/v1/environments/${DEV}/connections/${CREATED}/tests`,
        ]);
        expect(await cspViolations(page)).toEqual([]);
        // The browser logs each 403 of a step-up the stubs ask for (the create and the update),
        // and nothing else.
        expect(errors.filter((error) => !error.includes("status of 403"))).toEqual([]);
        expect(errors.filter((error) => error.includes("status of 403"))).toHaveLength(2);
      });
    });
  }
}
