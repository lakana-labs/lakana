import { expect, type Locator, type Page, type Request, test } from "@playwright/test";

import { addAdminPasskey, ORIGIN } from "./manage";
import { BRONZE_PATH, PLACES_STUBBED, stubPlaces, stubStepUpDelete } from "./places";
import { consoleErrors, tabTo, violations } from "./session";

const KEY = "lakana.preferences";
const DELETE_BRONZE = `DELETE ${BRONZE_PATH}`;

/** The items of a namespace's menu, with the dialog each opens from `prod.bronze`. */
const BRONZE_ITEMS = [
  ["New namespace…", "New namespace in bronze"],
  ["New external table…", "New external table in bronze"],
  ["Delete namespace…", "Delete namespace bronze?"],
] as const;

/**
 * Opens the Catalog at a place and waits for its Actions button.
 *
 * @param page - The page.
 * @param environment - The environment.
 * @param namespace - The dotted namespace; the environment itself without.
 * @returns The place's Actions button.
 */
async function openPlace(page: Page, environment: string, namespace?: string): Promise<Locator> {
  await page.goto(
    `/catalog?env=${environment}${namespace === undefined ? "" : `&ns=${namespace}`}`,
  );
  await expect(page.getByRole("heading", { level: 1, name: "Catalog" })).toBeVisible();
  const actions = page.getByRole("button", {
    name: `Actions for ${namespace ?? environment}`,
    exact: true,
  });
  await expect(actions).toBeVisible();
  return actions;
}

/**
 * Opens an item of the place menu with the pointer.
 *
 * @param actions - The place's Actions button.
 * @param item - The item's name.
 */
async function choose(actions: Locator, item: string): Promise<void> {
  await actions.click();
  await actions.page().getByRole("menuitem", { name: item }).click();
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
 * Waits until a dialog's confirm button may be pressed: not while it waits to learn whether the
 * environment is protected.
 *
 * @param button - The confirm button.
 */
async function ready(button: Locator): Promise<void> {
  await expect(button).toBeEnabled();
  await expect(button).not.toHaveAttribute("data-pending");
}

/**
 * Presses a dialog's confirm button with the pointer once it may be pressed.
 *
 * @param button - The confirm button.
 */
async function confirm(button: Locator): Promise<void> {
  await ready(button);
  await button.click();
}

/**
 * The note a create or a delete leaves above the result line.
 *
 * @param page - The page.
 * @param text - The note's sentence.
 * @returns The note.
 */
function placeNote(page: Page, text: string): Locator {
  const whole = new RegExp(`^${text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`);
  return page.locator(".catalog-result", { hasText: whole });
}

for (const theme of ["light", "dark"] as const) {
  for (const density of ["roomy", "compact"] as const) {
    test.describe(`the place menu, ${theme}, ${density}`, () => {
      test.beforeEach(async ({ page }) => {
        await stubPlaces(page);
        await page.addInitScript(
          ([key, value]) => {
            if (window.localStorage.getItem(key ?? "") === null) {
              window.localStorage.setItem(key ?? "", value ?? "");
            }
          },
          [KEY, JSON.stringify({ theme, density, language: "en" })],
        );
      });

      test("axe finds nothing on the open menu, each dialog, and the notes after create and delete", async ({
        page,
      }) => {
        let actions = await openPlace(page, "prod", "bronze");
        await actions.click();
        await expect(page.getByRole("menu")).toBeVisible();
        expect(await violations(page), "the open menu").toEqual([]);
        await page.keyboard.press("Escape");

        await choose(actions, "New external table…");
        const external = page.getByRole("dialog", { name: "New external table in bronze" });
        await expect(external.getByRole("textbox", { name: "Location" })).toHaveValue("raw/");
        expect(await violations(page), "the New external table form").toEqual([]);
        await external.getByRole("button", { name: "Cancel" }).click();

        await choose(actions, "Delete namespace…");
        const remove = page.getByRole("dialog", { name: "Delete namespace bronze?" });
        await expect(remove.getByRole("button", { name: "Delete the namespace" })).toBeEnabled();
        expect(await violations(page), "the Delete namespace dialog").toEqual([]);
        await remove.getByRole("button", { name: "Cancel" }).click();

        await choose(actions, "New namespace…");
        const create = page.getByRole("dialog", { name: "New namespace in bronze" });
        const path = create.getByRole("textbox", { name: "Path" });
        await expect(path).toHaveValue("bronze.");
        expect(await violations(page), "the New namespace form").toEqual([]);
        await path.fill("bronze.landing");
        await confirm(create.getByRole("button", { name: "Create" }));
        await expect(page).toHaveURL(/\/catalog\?env=prod&ns=bronze\.landing$/);
        const createdNote = placeNote(page, "Created namespace bronze.landing.");
        await expect(createdNote).toBeFocused();
        expect(await violations(page), "the note after a create").toEqual([]);

        actions = page.getByRole("button", { name: "Actions for bronze.landing", exact: true });
        await choose(actions, "Delete namespace…");
        await confirm(
          page
            .getByRole("dialog", { name: "Delete namespace bronze.landing?" })
            .getByRole("button", { name: "Delete the namespace" }),
        );
        await expect(page).toHaveURL(/\/catalog\?env=prod&ns=bronze$/);
        await expect(placeNote(page, "Deleted namespace bronze.landing.")).toBeFocused();
        expect(await violations(page), "the note after a delete").toEqual([]);

        // Deleting bronze lands on prod, whose tables fill the result line under the note.
        await choose(
          page.getByRole("button", { name: "Actions for bronze", exact: true }),
          "Delete namespace…",
        );
        await confirm(page.getByRole("button", { name: "Delete the namespace" }));
        await expect(placeNote(page, "Deleted namespace bronze.")).toBeFocused();
        await expect(page.getByRole("status").filter({ hasText: /\S/ })).toHaveCount(2);
        expect(await violations(page), "the note and the result line, both status regions").toEqual(
          [],
        );

        actions = await openPlace(page, "prod", "gold.sales");
        await choose(actions, "New external table…");
        const refused = page.getByRole("dialog", { name: "New external table in gold.sales" });
        await refused.getByRole("textbox", { name: "Name" }).fill("orders");
        await refused.getByRole("textbox", { name: "Location" }).fill("raw/none/*.csv");
        await confirm(refused.getByRole("button", { name: "Register" }));
        await expect(refused.getByRole("alert")).toContainText("No file matches 'raw/none/*.csv'.");
        expect(await violations(page), "the refused registration").toEqual([]);

        actions = await openPlace(page, "dev", "gold");
        await choose(actions, "New external table…");
        const guarded = page.getByRole("dialog", { name: "New external table in gold" });
        await expect(
          guarded.getByRole("textbox", { name: "Why you break the protection of dev" }),
        ).toBeVisible();
        expect(await violations(page), "the form in a protected environment").toEqual([]);
      });

      test("the menu and each dialog work by keyboard alone; focus returns to Actions or lands on the note", async ({
        page,
      }) => {
        const actions = await openPlace(page, "prod", "bronze");
        for (const [item, title] of BRONZE_ITEMS) {
          await tabTo(page, actions);
          await page.keyboard.press("Enter");
          const entry = page.getByRole("menuitem", { name: item });
          await expect(entry).toBeVisible();
          for (let step = 0; step < 4; step += 1) {
            if (await entry.evaluate((node) => node === document.activeElement)) {
              break;
            }
            await page.keyboard.press("ArrowDown");
          }
          await expect(entry).toBeFocused();
          await page.keyboard.press("Enter");
          const dialog = page.getByRole("dialog", { name: title });
          await expect(dialog).toBeVisible();
          await page.keyboard.press("Escape");
          await expect(dialog).toBeHidden();
          await expect(actions).toBeFocused();
        }

        // New namespace: the menu opens on its first item, and Enter in the field creates.
        await page.keyboard.press("Enter");
        await expect(page.getByRole("menuitem", { name: "New namespace…" })).toBeFocused();
        await page.keyboard.press("Enter");
        const path = page.getByRole("textbox", { name: "Path" });
        // Enter acts once the dialog knows whether the environment is protected.
        await ready(page.getByRole("button", { name: "Create" }));
        await tabTo(page, path);
        await page.keyboard.press("End");
        await page.keyboard.type("landing");
        await page.keyboard.press("Enter");
        await expect(placeNote(page, "Created namespace bronze.landing.")).toBeFocused();

        // Delete namespace: the confirm button is reached with Tab.
        const landing = page.getByRole("button", {
          name: "Actions for bronze.landing",
          exact: true,
        });
        await tabTo(page, landing);
        await page.keyboard.press("Enter");
        await page.keyboard.press("End");
        await expect(page.getByRole("menuitem", { name: "Delete namespace…" })).toBeFocused();
        await page.keyboard.press("Enter");
        const deleteButton = page.getByRole("button", { name: "Delete the namespace" });
        await ready(deleteButton);
        await tabTo(page, deleteButton);
        await page.keyboard.press("Enter");
        await expect(placeNote(page, "Deleted namespace bronze.landing.")).toBeFocused();

        // An environment offers only New namespace, which starts empty.
        const prod = await openPlace(page, "prod");
        await tabTo(page, prod);
        await page.keyboard.press("Enter");
        await expect(page.getByRole("menuitem")).toHaveCount(1);
        await page.keyboard.press("Enter");
        await expect(page.getByRole("textbox", { name: "Path" })).toHaveValue("");
        await page.keyboard.press("Escape");
        await expect(prod).toBeFocused();
      });

      test("a namespace with tables shows Delete disabled, and a screen reader hears why on opening", async ({
        page,
      }) => {
        const actions = await openPlace(page, "prod", "gold");
        await tabTo(page, actions);
        await page.keyboard.press("Enter");
        const menu = page.getByRole("menu");
        await expect(menu).toBeVisible();
        await expect(page.getByRole("menuitem", { name: "New namespace…" })).toBeFocused();
        const note = await menu.getAttribute("aria-describedby");
        expect(note, "the menu is described").not.toBeNull();
        await expect(page.locator(`[id="${note ?? ""}"]`)).toHaveText(
          "Only an empty namespace can be deleted.",
        );
        await expect(page.getByRole("menuitem", { name: "Delete namespace…" })).toHaveAttribute(
          "aria-disabled",
          "true",
        );
        await expect(page.getByRole("menuitem", { name: "New namespace…" })).toBeEnabled();
      });

      test("no horizontal scroll at 320 px with the menu and each dialog open", async ({
        page,
      }) => {
        await page.setViewportSize({ width: 320, height: 640 });
        const actions = await openPlace(page, "prod", "bronze");
        await actions.click();
        await expect(page.getByRole("menu")).toBeVisible();
        expect(await sidewaysOverflow(page), "the open menu").toBeLessThanOrEqual(0);
        await page.keyboard.press("Escape");
        for (const [item, title] of BRONZE_ITEMS) {
          await choose(actions, item);
          const dialog = page.getByRole("dialog", { name: title });
          await expect(dialog).toBeVisible();
          expect(await sidewaysOverflow(page), title).toBeLessThanOrEqual(0);
          const box = await dialog.boundingBox();
          expect((box?.x ?? -1) >= 0 && (box?.x ?? 0) + (box?.width ?? 0) <= 320, title).toBe(true);
          await page.keyboard.press("Escape");
          await expect(dialog).toBeHidden();
        }
        const guarded = await openPlace(page, "dev", "gold");
        await choose(guarded, "New external table…");
        await expect(
          page.getByRole("textbox", { name: /Why you break the protection/ }),
        ).toBeVisible();
        expect(await sidewaysOverflow(page), "the form with a reason").toBeLessThanOrEqual(0);
      });

      test("the place menu asks only for stubbed routes and logs no console error", async ({
        page,
      }) => {
        const errors = consoleErrors(page);
        const requests: string[] = [];
        const open = new Set<Request>();
        page.on("request", (request) => {
          requests.push(request.url());
          open.add(request);
        });
        page.on("requestfinished", (request) => open.delete(request));
        page.on("requestfailed", (request) => open.delete(request));

        const actions = await openPlace(page, "prod", "bronze");
        await choose(actions, "New namespace…");
        await page.getByRole("textbox", { name: "Path" }).fill("bronze.landing");
        await confirm(page.getByRole("button", { name: "Create" }));
        await expect(placeNote(page, "Created namespace bronze.landing.")).toBeVisible();
        await choose(
          page.getByRole("button", { name: "Actions for bronze.landing", exact: true }),
          "Delete namespace…",
        );
        await confirm(page.getByRole("button", { name: "Delete the namespace" }));
        await expect(placeNote(page, "Deleted namespace bronze.landing.")).toBeVisible();
        const guarded = await openPlace(page, "dev", "gold");
        await choose(guarded, "New external table…");
        await expect(
          page.getByRole("textbox", { name: /Why you break the protection/ }),
        ).toBeVisible();
        await page.keyboard.press("Escape");
        await expect(page.getByRole("dialog")).toHaveCount(0);
        await expect(guarded).toBeFocused();
        // Every request answered, so a late console error would have been seen.
        await expect.poll(() => open.size).toBe(0);

        const api = requests
          .filter((url) => url.includes("/api/"))
          .map((url) => new URL(url).pathname);
        expect(api).toContain("/api/v1/environments/prod/namespaces");
        expect(api.filter((path) => !PLACES_STUBBED.includes(path))).toEqual([]);
        expect(errors).toEqual([]);
      });
    });
  }
}

test.describe("Delete namespace with the passkey in real Chromium", () => {
  test.skip(({ browserName }) => browserName !== "chromium", "the virtual authenticator is CDP");

  test("Delete namespace asks for the passkey touch, sends again and lands on the environment", async ({
    page,
    context,
  }) => {
    await stubPlaces(page);
    await addAdminPasskey(context, page);
    const sent = await stubStepUpDelete(page);

    await page.goto(`${ORIGIN}/catalog?env=prod&ns=bronze`);
    await choose(
      page.getByRole("button", { name: "Actions for bronze", exact: true }),
      "Delete namespace…",
    );
    await confirm(
      page
        .getByRole("dialog", { name: "Delete namespace bronze?" })
        .getByRole("button", { name: "Delete the namespace" }),
    );

    await expect(page).toHaveURL(`${ORIGIN}/catalog?env=prod`);
    await expect(placeNote(page, "Deleted namespace bronze.")).toBeFocused();
    await expect(page.getByRole("treegrid").getByText("bronze")).toHaveCount(0);
    expect(sent).toEqual([
      DELETE_BRONZE,
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      DELETE_BRONZE,
    ]);
  });
});
