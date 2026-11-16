import { readFile } from "node:fs/promises";

import { expect, type Locator, type Page, test } from "@playwright/test";

import {
  administrator,
  artifactState,
  beginUploadAs,
  createEnvironment,
  createNamespace,
  invitedPerson,
  makeAdministrator,
  skipTheWelcome,
  uniqueName,
  uploadArtifact,
  uploadsAs,
  violations,
} from "./helpers";

type Request = Parameters<typeof createEnvironment>[0];

/** The files of the first version. */
const FIRST = { "models/model.pkl": "weights, first run", "config.json": '{"layers": 2}' };

/** The files of the second version. */
const SECOND = { "models/model.pkl": "weights, second run", "config.json": '{"layers": 3}' };

/** What the page says for the server's 409 `NoPasskeyError`: the account has no passkey to touch. */
const NO_PASSKEY =
  "Your account has no passkey yet, and this action needs a passkey touch. Add one first under Passkeys and sessions, in the menu under your name, then try again.";

/**
 * A new environment with the namespace `ml` and the artifact `ml.model` of one version, so that
 * each test acts on its own data on the run's one shared server.
 *
 * @param request - The request context.
 * @returns The environment's name and the artifact page's address.
 */
async function lake(request: Request): Promise<{ environment: string; address: string }> {
  const environment = uniqueName("art");
  await createEnvironment(request, environment);
  await createNamespace(request, environment, "ml");
  expect(await uploadArtifact(request, environment, "ml.model", FIRST)).toBe(1);
  return { environment, address: `/catalog/${environment}/ml/model` };
}

/**
 * The row of a version on the Versions tab.
 *
 * @param page - The page, on the Versions tab.
 * @param version - The version's number.
 * @returns The row.
 */
function versionRow(page: Page, version: number): Locator {
  return page
    .getByRole("region", { name: "Versions of ml.model" })
    .getByRole("row")
    .filter({
      has: page.locator(".version-number", { hasText: new RegExp(`^${String(version)}$`) }),
    });
}

test("an administrator opens an artifact from the catalog list, downloads a file and gets its bytes", async ({
  browser,
  request,
}) => {
  const { environment, address } = await lake(request);
  const page = await administrator(browser, request);

  await page.goto(`/catalog?env=${environment}&ns=ml`);
  const row = page.getByRole("row").filter({ hasText: "model" });
  // The row's second line names the kind, then the namespace.
  await expect(row.getByRole("cell").first()).toContainText(/^model\s*Artifact · ml\b/);
  await row.getByRole("link", { name: "model" }).click();
  await expect(page).toHaveURL(new RegExp(`${address}$`));
  expect(await violations(page), "the Overview").toEqual([]);

  await page
    .getByRole("navigation", { name: "Sections of the artifact" })
    .getByRole("link", { name: "Versions", exact: true })
    .click();
  await page.getByRole("button", { name: "Files of version 1" }).click();
  const files = page.getByRole("region", { name: "Files of version 1" });
  const download = files.getByRole("button", { name: "Download models/model.pkl" });
  await expect(download).toBeVisible();
  expect(await violations(page), "the Versions tab with files open").toEqual([]);

  const saved = page.waitForEvent("download");
  await download.click();
  const file = await saved;
  expect(file.suggestedFilename()).toBe("model.pkl");
  expect(await readFile(await file.path(), "utf8")).toBe(FIRST["models/model.pkl"]);
  await page.context().close();
});

test("an administrator with a passkey deletes a version, then drops the artifact and lands on its namespace", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
  const { environment, address } = await lake(request);
  expect(await uploadArtifact(request, environment, "ml.model", SECOND)).toBe(2);
  const { page, name } = await invitedPerson(browser, request, (person) =>
    makeAdministrator(request, person),
  );
  await skipTheWelcome(page);

  await page.goto(`${address}/versions`);
  await expect(versionRow(page, 2)).toContainText("current");
  await page.getByRole("button", { name: "Delete version 2…" }).click();
  const dialog = page.getByRole("dialog", { name: /^Deleted? version 2 of ml\.model\??$/ });
  expect(await violations(page), "the Delete dialog").toEqual([]);
  // The passkey touch the server asks for is answered by the virtual authenticator.
  await dialog.getByRole("button", { name: "Delete the version" }).click();
  await expect(dialog.getByText(/^Deleted version 2 of ml\.model\. Freed /)).toBeVisible();
  await dialog.getByRole("button", { name: "Close" }).click();
  await expect(dialog).toBeHidden();

  await expect(versionRow(page, 2)).toContainText(`by ${name}`);
  await expect(versionRow(page, 2)).toContainText("Deleted");
  await expect(versionRow(page, 1)).toContainText("current");
  const afterDelete = await artifactState(request, environment, "ml.model");
  expect(afterDelete.versions.map((row) => [row.version, row.deleted_at !== null])).toEqual([
    [2, true],
    [1, false],
  ]);

  await page.getByRole("button", { name: "Actions" }).click();
  await page.getByRole("menuitem", { name: "Drop…" }).click();
  await page
    .getByRole("dialog", { name: "Drop ml.model?" })
    .getByRole("button", { name: "Drop the artifact" })
    .click();
  await expect(page).toHaveURL(new RegExp(`/catalog\\?env=${environment}&ns=ml$`));
  await expect(page.getByText(/^Dropped artifact ml\.model\./)).toBeFocused();
  expect(await violations(page), "the note after the drop").toEqual([]);
  expect((await artifactState(request, environment, "ml.model")).status).toBe(404);
  await page.context().close();
});

test("a person withdraws their own open upload", async ({ browser, browserName, request }) => {
  // Withdraw itself needs no passkey: invitedPerson enrols one with the CDP virtual authenticator.
  test.skip(
    browserName !== "chromium",
    "invitedPerson enrols a passkey, and the authenticator is CDP",
  );
  const { environment, address } = await lake(request);
  const { page } = await invitedPerson(browser, request, (person) =>
    makeAdministrator(request, person),
  );
  await skipTheWelcome(page);
  const upload = await beginUploadAs(page, environment, "ml.model");
  expect(await uploadsAs(page, environment, "ml.model")).toEqual([upload]);

  await page.goto(`${address}/versions`);
  const uploads = page.getByRole("region", { name: "Your open uploads of ml.model" });
  await expect(uploads.getByText(upload)).toBeVisible();
  expect(await violations(page), "the uploads block").toEqual([]);
  await uploads.getByRole("button", { name: `Withdraw upload ${upload}` }).click();
  await expect(page.getByText(`Withdrew upload ${upload}.`)).toBeFocused();
  expect(await uploadsAs(page, environment, "ml.model")).toEqual([]);
  await page.context().close();
});

test("a local administrator without a passkey cannot delete a version: the dialog says why and the version stays", async ({
  browser,
  request,
}) => {
  const { environment, address } = await lake(request);
  const page = await administrator(browser, request);

  await page.goto(`${address}/versions`);
  await page.getByRole("button", { name: "Delete version 1…" }).click();
  const dialog = page.getByRole("dialog", { name: "Delete version 1 of ml.model?" });
  await dialog.getByRole("button", { name: "Delete the version" }).click();
  // The session has no passkey, so there is nothing to touch: the server's 409 NoPasskeyError,
  // in the interface's words for it.
  await expect(dialog.getByRole("alert")).toContainText(NO_PASSKEY);
  await expect(dialog).toBeVisible();
  const state = await artifactState(request, environment, "ml.model");
  expect(state.versions.map((row) => [row.version, row.deleted_at])).toEqual([[1, null]]);
  await page.context().close();
});
