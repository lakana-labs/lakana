import { readFile } from "node:fs/promises";

import { expect, type Locator, type Page, test } from "@playwright/test";

import {
  ARTIFACTS_STUBBED,
  CURRENT_PATH,
  MODEL_BLOB,
  stubArtifacts,
  stubGitOwnedArtifact,
  stubStepUpDeleteVersion,
} from "./artifacts";
import { addAdminPasskey, ORIGIN } from "./manage";
import { consoleErrors, tabTo, violations } from "./session";

const KEY = "lakana.preferences";
const ADDRESS = "/catalog/prod/ml/model";
const DELETE_CURRENT = `DELETE ${CURRENT_PATH}`;
const DROPPED =
  "Dropped artifact ml.model. Its files are removed from storage, and Lakana's periodic clean-up removes any it could not.";
const TABS = ["Overview", "Versions", "Lineage", "Access"] as const;

/**
 * A tab of the artifact's page.
 *
 * @param page - The page.
 * @param name - The tab's name.
 * @returns Its link.
 */
function tabLink(page: Page, name: string): Locator {
  return page
    .getByRole("navigation", { name: "Sections of the artifact" })
    .getByRole("link", { name, exact: true });
}

/**
 * Opens a tab of the artifact's page and waits for its tabs.
 *
 * @param page - The page.
 * @param tab - The tab's address segment; Overview without.
 */
async function openArtifact(page: Page, tab = ""): Promise<void> {
  await page.goto(tab === "" ? ADDRESS : `${ADDRESS}/${tab}`);
  await expect(tabLink(page, "Versions")).toBeVisible();
}

/**
 * The Versions tab's table, once its rows are there.
 *
 * @param page - The page.
 * @returns The region the versions scroll in.
 */
async function versionsRegion(page: Page): Promise<Locator> {
  const region = page.getByRole("region", { name: "Versions of ml.model" });
  await expect(region.getByRole("button", { name: "Files of version 2" })).toBeVisible();
  return region;
}

/**
 * Opens the files of version 2 with the pointer and waits for them.
 *
 * @param page - The page.
 * @returns The files' region.
 */
async function openFiles(page: Page): Promise<Locator> {
  await (await versionsRegion(page)).getByRole("button", { name: "Files of version 2" }).click();
  const files = page.getByRole("region", { name: "Files of version 2" });
  await expect(files.getByRole("button", { name: "Download models/v2/model.pkl" })).toBeVisible();
  return files;
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

/**
 * Whether the focus is on an element of the page rather than on `<body>`.
 *
 * @param page - The page.
 * @returns The focused element's tag, lowercase.
 */
function focusedTag(page: Page): Promise<string> {
  return page.evaluate(() => (document.activeElement ?? document.body).tagName.toLowerCase());
}

/**
 * Presses a dialog's confirm button with the pointer once it may be pressed: not while it waits
 * to learn whether the environment is protected.
 *
 * @param button - The confirm button.
 */
async function confirm(button: Locator): Promise<void> {
  await expect(button).toBeEnabled();
  await expect(button).not.toHaveAttribute("data-pending");
  await button.click();
}

/**
 * Drops the artifact from its Actions menu with the pointer.
 *
 * @param page - The page, on the artifact.
 */
async function dropWithPointer(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Actions" }).click();
  await page.getByRole("menuitem", { name: "Drop…" }).click();
  await confirm(
    page
      .getByRole("dialog", { name: "Drop ml.model?" })
      .getByRole("button", { name: "Drop the artifact" }),
  );
}

for (const theme of ["light", "dark"] as const) {
  for (const density of ["roomy", "compact"] as const) {
    test.describe(`an artifact's page, ${theme}, ${density}`, () => {
      test.beforeEach(async ({ page }) => {
        await stubArtifacts(page);
        await page.addInitScript(
          ([key, value]) => {
            if (window.localStorage.getItem(key ?? "") === null) {
              window.localStorage.setItem(key ?? "", value ?? "");
            }
          },
          [KEY, JSON.stringify({ theme, density, language: "en" })],
        );
      });

      test("axe finds nothing on the Overview, the Versions tab with files open, each dialog, the uploads block and the note after a drop", async ({
        page,
      }) => {
        await openArtifact(page);
        await expect(page.getByText("None: every version is deleted")).toHaveCount(0);
        await expect(page.getByText("2, deleted ones included")).toBeVisible();
        expect(await violations(page), "the Overview").toEqual([]);

        await openArtifact(page, "versions");
        await openFiles(page);
        await expect(page.getByRole("heading", { name: "Your open uploads" })).toBeVisible();
        expect(await violations(page), "the Versions tab with files and uploads").toEqual([]);

        await page.getByRole("button", { name: "Delete version 2…" }).click();
        const remove = page.getByRole("dialog", { name: /^Deleted? version 2 of ml\.model\??$/ });
        await expect(remove.getByRole("button", { name: "Delete the version" })).toBeEnabled();
        expect(await violations(page), "the Delete dialog").toEqual([]);
        await confirm(remove.getByRole("button", { name: "Delete the version" }));
        await expect(
          remove.getByText(/^Deleted version 2 of ml\.model\. Freed 1 file/),
        ).toBeVisible();
        expect(await violations(page), "the Delete dialog's summary").toEqual([]);
        await remove.getByRole("button", { name: "Close" }).click();

        await page.getByRole("button", { name: /^Withdraw upload / }).click();
        await expect(page.getByText(/^Withdrew upload /)).toBeFocused();
        expect(await violations(page), "the uploads block after a withdrawal").toEqual([]);

        await page.getByRole("button", { name: "Actions" }).click();
        await page.getByRole("menuitem", { name: "Drop…" }).click();
        const drop = page.getByRole("dialog", { name: "Drop ml.model?" });
        await expect(drop.getByRole("button", { name: "Drop the artifact" })).toBeEnabled();
        expect(await violations(page), "the Drop dialog").toEqual([]);
        await confirm(drop.getByRole("button", { name: "Drop the artifact" }));
        await expect(page).toHaveURL(/\/catalog\?env=prod&ns=ml$/);
        await expect(page.getByText(DROPPED, { exact: true })).toBeFocused();
        expect(await violations(page), "the note after a drop").toEqual([]);
      });

      test("the tabs, Files, Download, Delete, Withdraw and Drop work by keyboard alone; focus returns or lands on the status", async ({
        page,
      }) => {
        await openArtifact(page);
        // The tabs: each is a link reached with Tab and followed with Enter.
        for (const tab of [...TABS.slice(1), TABS[0], TABS[1]]) {
          const link = tabLink(page, tab);
          await tabTo(page, link);
          await page.keyboard.press("Enter");
          await expect(link).toHaveAttribute("aria-current", "page");
        }

        // Files opens below its version; Download saves the file.
        const files = (await versionsRegion(page)).getByRole("button", {
          name: "Files of version 2",
        });
        await tabTo(page, files);
        await page.keyboard.press("Enter");
        await expect(files).toHaveAttribute("aria-expanded", "true");
        const download = page.getByRole("button", { name: "Download models/v2/model.pkl" });
        await tabTo(page, download);
        const saved = page.waitForEvent("download");
        await page.keyboard.press("Enter");
        expect((await saved).suggestedFilename()).toBe("model.pkl");
        await expect(download).toBeFocused();
        await tabTo(page, files);
        await page.keyboard.press("Enter");
        await expect(files).toHaveAttribute("aria-expanded", "false");
        await expect(files).toBeFocused();

        // Delete…: Escape returns the focus to the button that opened it.
        const deleteButton = page.getByRole("button", { name: "Delete version 2…" });
        await tabTo(page, deleteButton);
        await page.keyboard.press("Enter");
        const dialog = page.getByRole("dialog", { name: /^Deleted? version 2 of ml\.model\??$/ });
        await expect(dialog).toBeVisible();
        // The dialog takes the focus as it opens; an Escape before that reaches the page behind it.
        await expect
          .poll(() => dialog.evaluate((node) => node.contains(document.activeElement)))
          .toBe(true);
        await page.keyboard.press("Escape");
        await expect(dialog).toBeHidden();
        await expect(deleteButton).toBeFocused();

        // Delete the version: the summary, then Close puts the focus on the version's row.
        await page.keyboard.press("Enter");
        const confirmButton = dialog.getByRole("button", { name: "Delete the version" });
        await expect(confirmButton).toBeEnabled();
        await expect(confirmButton).not.toHaveAttribute("data-pending");
        await tabTo(page, confirmButton);
        await page.keyboard.press("Enter");
        const close = dialog.getByRole("button", { name: "Close" });
        await expect(dialog.getByText(/^Deleted version 2 of ml\.model\./)).toBeVisible();
        await tabTo(page, close);
        await page.keyboard.press("Enter");
        await expect(dialog).toBeHidden();
        await expect(page.getByText(/^Deleted .* by admin$/)).toBeVisible();
        expect(await focusedTag(page), "the focus after Delete").not.toBe("body");
        await expect(page.locator(":focus")).toHaveText(/^2/);

        // Withdraw the last upload: the status takes the focus.
        const withdraw = page.getByRole("button", { name: /^Withdraw upload / });
        await tabTo(page, withdraw);
        await page.keyboard.press("Enter");
        const withdrew = page.getByText(/^Withdrew upload /);
        await expect(withdrew).toBeFocused();
        await expect(page.getByRole("button", { name: /^Withdraw upload / })).toHaveCount(0);
        expect(await focusedTag(page), "the focus after the last Withdraw").not.toBe("body");

        // Drop: Actions opens the menu, Enter opens the dialog, the confirm button is a Tab away.
        const actions = page.getByRole("button", { name: "Actions" });
        await tabTo(page, actions);
        await page.keyboard.press("Enter");
        await expect(page.getByRole("menuitem", { name: "Drop…" })).toBeFocused();
        await page.keyboard.press("Escape");
        await expect(actions).toBeFocused();
        await page.keyboard.press("Enter");
        await page.keyboard.press("Enter");
        const drop = page.getByRole("button", { name: "Drop the artifact" });
        await expect(drop).toBeEnabled();
        await expect(drop).not.toHaveAttribute("data-pending");
        await tabTo(page, drop);
        await page.keyboard.press("Enter");
        await expect(page.getByText(DROPPED, { exact: true })).toBeFocused();
      });

      test("a git-owned artifact shows Drop disabled, and a screen reader hears why on opening", async ({
        page,
      }) => {
        await stubGitOwnedArtifact(page);
        await openArtifact(page, "versions");
        const actions = page.getByRole("button", { name: "Actions" });
        await tabTo(page, actions);
        await page.keyboard.press("Enter");
        const menu = page.getByRole("menu");
        await expect(menu).toBeVisible();
        const note = await menu.getAttribute("aria-describedby");
        expect(note, "the menu is described").not.toBeNull();
        await expect(page.locator(`[id="${note ?? ""}"]`)).toHaveText(
          "Drop is off: analytics/assets/ml/model.py defines this. Remove it there and deploy, then drop it from Orphans.",
        );
        await expect(page.getByRole("menuitem", { name: "Drop…" })).toHaveAttribute(
          "aria-disabled",
          "true",
        );
        await page.keyboard.press("Escape");
        await expect(actions).toBeFocused();
        // Deleting one version stays: keep_versions in git only sets the limit.
        await expect(page.getByRole("button", { name: "Delete version 2…" })).toBeVisible();
      });

      test("no horizontal scroll at 320 px on each tab with files open; the lists scroll in their own region", async ({
        page,
      }) => {
        await page.setViewportSize({ width: 320, height: 640 });
        for (const tab of ["", "lineage", "access"]) {
          await openArtifact(page, tab);
          await page.waitForLoadState("networkidle");
          expect(await sidewaysOverflow(page), tab || "overview").toBeLessThanOrEqual(0);
        }
        await openArtifact(page, "versions");
        await openFiles(page);
        expect(await sidewaysOverflow(page), "versions with files open").toBeLessThanOrEqual(0);
        // The versions and the files below them scroll inside the versions' focusable region.
        const region = page.getByRole("region", { name: "Versions of ml.model" });
        await expect(region).toHaveAttribute("tabindex", "0");
        const [scrollWidth, clientWidth] = await region.evaluate((node) => [
          node.scrollWidth,
          node.clientWidth,
        ]);
        expect(scrollWidth, "the versions scroll sideways in their region").toBeGreaterThan(
          clientWidth,
        );
        await expect(
          region.getByRole("region", { name: "Files of version 2" }),
          "the files are inside the versions' region",
        ).toBeVisible();
        const uploads = page.getByRole("region", { name: "Your open uploads of ml.model" });
        await expect(uploads).toHaveAttribute("tabindex", "0");
        expect(await sidewaysOverflow(page), "with the uploads").toBeLessThanOrEqual(0);
      });

      test("Download saves the file with its name", async ({ page }) => {
        const downloads: string[] = [];
        page.on("download", (download) => downloads.push(download.suggestedFilename()));
        await openArtifact(page, "versions");
        const files = await openFiles(page);

        const saved = page.waitForEvent("download");
        await files.getByRole("button", { name: "Download models/v2/model.pkl" }).click();
        const download = await saved;
        expect(download.suggestedFilename()).toBe("model.pkl");
        expect(await readFile(await download.path(), "utf8")).toBe("weights of the model");

        // Bytes that differ from the manifest save nothing and say so.
        await page.route(
          (url) => url.pathname === MODEL_BLOB,
          (route) => route.fulfill({ contentType: "application/octet-stream", body: "weights" }),
        );
        await files.getByRole("button", { name: "Download models/v2/model.pkl" }).click();
        await expect(
          files.getByText(
            "models/v2/model.pkl did not arrive whole, so nothing was saved. Download it again, or use the command above.",
          ),
        ).toBeVisible();
        await expect(
          files.getByRole("button", { name: "Download models/v2/model.pkl" }),
        ).toBeEnabled();
        // A later download is saved; the one that did not arrive whole never was.
        const config = page.waitForEvent("download");
        await files.getByRole("button", { name: "Download config.json" }).click();
        expect((await config).suggestedFilename()).toBe("config.json");
        expect(downloads).toEqual(["model.pkl", "config.json"]);
      });

      test("the artifact page asks only for stubbed routes and logs no console error", async ({
        page,
      }) => {
        const errors = consoleErrors(page);
        const requests: string[] = [];
        page.on("request", (request) => requests.push(request.url()));

        await page.goto("/catalog?env=prod&ns=ml");
        const row = page.getByRole("row").filter({ hasText: "model" });
        await expect(row).toContainText("Artifact");
        await row.getByRole("link", { name: "model" }).click();
        await expect(page).toHaveURL(new RegExp(`${ADDRESS}$`));
        for (const tab of TABS) {
          await tabLink(page, tab).click();
          await page.waitForLoadState("networkidle");
        }
        await tabLink(page, "Versions").click();
        const files = await openFiles(page);
        const saved = page.waitForEvent("download");
        await files.getByRole("button", { name: "Download models/v2/model.pkl" }).click();
        await saved;
        await page.getByRole("button", { name: /^Withdraw upload / }).click();
        await expect(page.getByText(/^Withdrew upload /)).toBeVisible();
        await page.getByRole("button", { name: "Delete version 2…" }).click();
        await confirm(page.getByRole("button", { name: "Delete the version" }));
        await page.getByRole("button", { name: "Close" }).click();
        await dropWithPointer(page);
        await expect(page.getByText(DROPPED, { exact: true })).toBeVisible();
        await page.waitForLoadState("networkidle");

        const api = requests
          .filter((url) => url.includes("/api/"))
          .map((url) => new URL(url).pathname);
        expect(api).toContain(MODEL_BLOB);
        expect(api.filter((path) => !ARTIFACTS_STUBBED.includes(path))).toEqual([]);
        expect(errors).toEqual([]);
      });
    });
  }
}

test.describe("Delete a version with the passkey in real Chromium", () => {
  test.skip(({ browserName }) => browserName !== "chromium", "the virtual authenticator is CDP");

  test("Delete… asks for the passkey touch, sends again and shows what was freed", async ({
    page,
    context,
  }) => {
    await stubArtifacts(page);
    await addAdminPasskey(context, page);
    const sent = await stubStepUpDeleteVersion(page);

    await page.goto(`${ORIGIN}${ADDRESS}/versions`);
    await page.getByRole("button", { name: "Delete version 2…" }).click();
    const dialog = page.getByRole("dialog", { name: /^Deleted? version 2 of ml\.model\??$/ });
    await confirm(dialog.getByRole("button", { name: "Delete the version" }));

    await expect(
      dialog.getByText(/^Deleted version 2 of ml\.model\. Freed 1 file \(20 B\)\./),
    ).toBeVisible();
    await expect(dialog.getByText(/Lakana's periodic clean-up decides about/)).toBeVisible();
    await dialog.getByRole("button", { name: "Close" }).click();
    await expect(page.getByText(/^Deleted .* by admin$/)).toBeVisible();
    expect(sent).toEqual([
      DELETE_CURRENT,
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      DELETE_CURRENT,
    ]);
  });
});
