import { expect, type Page, test } from "@playwright/test";

import { stubArtifacts } from "./artifacts";
import { stubConnect } from "./connect";
import { expectMarked, poison, poisoned, rawOverrides, RLO } from "./hidden";
import { stubGitOwned, stubManagement } from "./manage";
import { stubPlaces } from "./places";
import { ADMIN_SESSION, signInAsAdmin } from "./session";

// Every stubbed answer carries a right-to-left override in each text field (e2e/hidden.ts); each
// screen must show it as the mark U+202E only. A failure names the element that shows it raw.

/**
 * A name as the poisoned answers hold it, written for an address: a link the page builds from
 * them carries the override too, and so may a link someone sends.
 *
 * @param name - The name as the stubs write it.
 * @returns The name with the override, encoded.
 */
function n(name: string): string {
  return `${name}%E2%80%AE`;
}

const TABLE = `/catalog/${n("prod")}/${n("gold.sales")}/${n("fact_sales")}`;
const ARTIFACT = `/catalog/${n("prod")}/${n("ml")}/${n("model")}`;

test.beforeEach(({ browserName }) => {
  test.skip(browserName !== "chromium", "the check reads the DOM, the same in every browser");
});

/**
 * Opens an address and checks it.
 *
 * @param page - The page.
 * @param address - The address.
 */
async function visit(page: Page, address: string): Promise<void> {
  await page.goto(address);
  await expectMarked(page, address);
}

/**
 * Opens the Actions menu at an address and each of its items in turn, checking each dialog.
 *
 * @param page - The page.
 * @param actions - The name of the Actions button.
 */
async function eachAction(page: Page, actions: RegExp): Promise<void> {
  const button = page.getByRole("button", { name: actions }).first();
  await button.click();
  await expectMarked(page, `${actions.source} menu`);
  const items = await page.getByRole("menuitem").allTextContents();
  await page.keyboard.press("Escape");
  for (const item of items) {
    await button.click();
    const entry = page.getByRole("menuitem", { name: item, exact: true });
    if (await entry.isDisabled()) {
      await page.keyboard.press("Escape");
      continue;
    }
    await entry.click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expectMarked(page, `the ${item} dialog`);
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
  }
}

test("the check finds a hidden character shown as itself, and leaves a field's value alone", async ({
  page,
}) => {
  await page.setContent(
    `<title>t${RLO}</title><p>a<span>b${RLO}</span></p><button aria-label="c${RLO}">c</button>` +
      `<img alt="d${RLO}" src="data:,"><svg><title>e${RLO}</title></svg>` +
      `<textarea>f${RLO}</textarea><input value="g${RLO}"><p>h<span class="char-mark">U+202E</span></p>`,
  );

  expect(await rawOverrides(page)).toEqual([
    "document title: t⟨RLO⟩",
    "text in body > p > span: b⟨RLO⟩",
    "text in body > svg > title: e⟨RLO⟩",
    "aria-label of html > body > button: c⟨RLO⟩",
    "alt of html > body > img: d⟨RLO⟩",
  ]);
});

test("the answers carry the override in every text but codes, ids and times", () => {
  expect(
    poison({
      name: "fact_sales",
      state: "failing",
      type: "decimal(10,2)",
      principal: { type: "USER", name: "kim" },
      error: { type: "StepUpRequired", detail: "Touch your passkey." },
      nodes: [{ id: "table:a", name: "gold.a" }],
      edges: [{ kind: "reads", source: "table:a", target: "table:a" }],
      grant_id: "g-1",
      at: "2026-11-09T10:00:00Z",
      count: 3,
      tags: ["pii", ""],
    }),
  ).toEqual({
    name: `fact_sales${RLO}`,
    state: "failing",
    type: `decimal(10,2)${RLO}`,
    principal: { type: "USER", name: `kim${RLO}` },
    error: { type: "StepUpRequired", detail: `Touch your passkey.${RLO}` },
    nodes: [{ id: "table:a", name: `gold.a${RLO}` }],
    edges: [{ kind: "reads", source: "table:a", target: "table:a" }],
    grant_id: "g-1",
    at: "2026-11-09T10:00:00Z",
    count: 3,
    tags: [`pii${RLO}`, ""],
  });
});

const AT = "2026-11-09T10:00:00Z";

/**
 * Answers what the Access area and one's account read beyond `session.ts`: an open invite, an
 * open recovery link, a passkey, a session, and a sign-in notice about another person.
 *
 * @param page - The page to stub, poisoned.
 */
async function stubAccounts(page: Page): Promise<void> {
  await page.route("**/api/v1/sign-in/notices", (route) =>
    route.fulfill({
      json: [{ id: "n-1", kind: "recovery_used", subject: "kim.schulz", created_at: AT }],
    }),
  );
  await page.route("**/api/v1/invites", (route) =>
    route.fulfill({
      json: [{ id: "i-1", name: "sam", display_name: "Sam Kay", created_at: AT, expires_at: AT }],
    }),
  );
  await page.route("**/api/v1/recovery-links", (route) =>
    route.fulfill({ json: [{ id: "r-1", name: "kim.schulz", created_at: AT, expires_at: AT }] }),
  );
  await page.route("**/api/v1/principals/admin/passkeys", (route) =>
    route.fulfill({
      json: [
        {
          id: "p-1",
          name: "Work laptop",
          created_at: AT,
          last_used_at: AT,
          backup_eligible: true,
          disabled_reason: null,
          usable_here: true,
        },
      ],
    }),
  );
  await page.route("**/api/v1/principals/admin/sessions", (route) =>
    route.fulfill({
      json: [
        {
          id: "s-1",
          via: "passkey",
          created_at: AT,
          last_seen_at: AT,
          absolute_expires_at: AT,
          user_agent: "Mozilla/5.0 (X11; Linux x86_64) Chrome/155.0.0.0 Safari/537.36",
          browser: "Chrome 155 on Linux",
          peer: "127.0.0.1",
          current: true,
        },
      ],
    }),
  );
}

test("Home, the Catalog, Access, Settings and the account mark hidden characters", async ({
  page,
}) => {
  await signInAsAdmin(poisoned(page));
  for (const address of [
    "/",
    "/catalog",
    `/catalog?env=${n("prod")}`,
    `/catalog?env=${n("prod")}&ns=${n("gold.sales")}`,
    `/catalog?env=${n("prod")}&ns=${n("gold")}`,
    "/catalog?q=store",
    `/catalog?env=${n("staging")}`,
    "/access",
    `/access/people/${n("admin")}`,
    "/settings/sign-in",
    "/account",
  ]) {
    await visit(page, address);
  }
  await page.getByRole("button", { name: /admin/ }).first().click();
  await expectMarked(page, "the user menu");
});

test("every tab of a table's page, its editors and its dialogs mark hidden characters", async ({
  page,
}) => {
  await signInAsAdmin(poisoned(page));
  for (const tab of ["", "/columns", "/preview", "/lineage", "/references", "/health", "/access"]) {
    await visit(page, `${TABLE}${tab}`);
  }
  await visit(page, `${TABLE}/preview`);
  await page.getByRole("gridcell").nth(2).click();
  await expectMarked(page, "the Value panel");

  await visit(page, `${TABLE}/lineage`);
  await page.getByRole("button", { name: "Full screen" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expectMarked(page, "the full-screen lineage");
  await page.keyboard.press("Escape");

  await visit(page, TABLE);
  await page.getByRole("button", { name: /fact_sales\.sql/ }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expectMarked(page, "the file of Defined in");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await expectMarked(page, "the editor of the table's texts");

  await visit(page, `${TABLE}/columns`);
  await page.getByRole("button", { name: /^Edit store_id/ }).click();
  await expectMarked(page, "the editor of a column's texts");
});

test("a table's actions, the catalog's views and a git-owned table mark hidden characters", async ({
  page,
}) => {
  await stubManagement(poisoned(page));
  for (const view of ["dropped", "expiring", "orphans"]) {
    await visit(page, `/catalog?env=${n("prod")}&view=${view}`);
  }
  await visit(page, TABLE);
  await eachAction(page, /^Actions$/);
  await stubGitOwned(poisoned(page));
  await visit(page, TABLE);
  await eachAction(page, /^Actions$/);
});

test("the place menu and its dialogs mark hidden characters", async ({ page }) => {
  await stubPlaces(poisoned(page));
  await visit(page, `/catalog?env=${n("prod")}&ns=${n("bronze")}`);
  await eachAction(page, /^Actions for bronze/);
  await visit(page, `/catalog?env=${n("prod")}&ns=${n("gold")}`);
  await eachAction(page, /^Actions for gold/);
  await visit(page, `/catalog?env=${n("dev")}&ns=${n("gold")}`);
  await eachAction(page, /^Actions for gold/);
});

test("an artifact's page marks hidden characters", async ({ page }) => {
  await stubArtifacts(poisoned(page));
  for (const tab of ["", "/lineage", "/access", "/versions"]) {
    await visit(page, `${ARTIFACT}${tab}`);
  }
  await page.getByRole("button", { name: /^Files of version 2/ }).click();
  await expectMarked(page, "the files of version 2");
  await visit(page, ARTIFACT);
  await eachAction(page, /^Actions$/);
});

test("the Access area, one's account and a sign-in notice mark hidden characters", async ({
  page,
}) => {
  await signInAsAdmin(poisoned(page));
  await stubAccounts(poisoned(page));
  for (const address of [
    "/",
    "/access/invites",
    "/access/recovery-links",
    `/access/people/${n("admin")}`,
    "/account",
  ]) {
    await visit(page, address);
  }
});

test("an invite link marks hidden characters in the name it signs in as", async ({ page }) => {
  const stub = poisoned(page);
  await stub.route("**/api/v1/sign-in/session", (route) =>
    route.fulfill({ status: 401, json: { detail: "Sign in.", type: "AuthenticationError" } }),
  );
  await stub.route("**/api/v1/sign-in/links/describe", (route) =>
    route.fulfill({
      json: { kind: "invite", name: ADMIN_SESSION.name, display_name: "Ada", state: "open" },
    }),
  );
  await visit(page, "/sign-in#l=pub-1&s=sec-1");
});

test("Connect a repository marks hidden characters in every step", async ({ page }) => {
  await signInAsAdmin(poisoned(page));
  await stubConnect(poisoned(page));
  await page.goto("/settings/repositories/new");
  const url = page.getByRole("textbox", { name: "Repository URL" });
  await url.fill("https://github.com/acme/analytics");
  await url.blur();
  await page.getByRole("button", { name: "Use a token instead" }).click();
  await page.getByRole("button", { name: "Fetch the host's keys" }).click();
  // The host's preferred key type carries the override too, so every key shows at once.
  await expect(page.getByRole("checkbox", { name: /^This matches: ssh-rsa/ })).toBeVisible();
  await expectMarked(page, "the host's keys");
  await page.getByRole("checkbox", { name: /^This matches: ssh-ed25519/ }).check();
  await page.getByRole("button", { name: "Create the repository and its keys" }).click();
  await expect(page.getByRole("button", { name: /^Test access in dev/ })).toBeVisible();
  await expectMarked(page, "the keys");
  await page.getByRole("button", { name: /^Test access in dev/ }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expectMarked(page, "a refused test");
});

test("the results and refusals of the catalog's actions mark hidden characters", async ({
  page,
}) => {
  await stubManagement(poisoned(page));
  await visit(page, TABLE);
  await page.getByRole("button", { name: "Actions" }).click();
  await page.getByRole("menuitem", { name: "Maintain now…" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Maintain now" }).click();
  await expect(page.getByRole("dialog").getByText(/is queued/)).toBeVisible();
  await expectMarked(page, "the queued maintenance");
  await page.keyboard.press("Escape");

  await visit(page, `/catalog?env=${n("prod")}&view=dropped`);
  await page.getByRole("button", { name: /^Undrop gold\.sales.*fact_returns/ }).click();
  await expect(page.getByRole("status").filter({ hasText: "Restored" })).toBeVisible();
  await expectMarked(page, "an Undrop");
});

test("the notes and refusals of the place menu mark hidden characters", async ({ page }) => {
  await stubPlaces(poisoned(page));
  await visit(page, `/catalog?env=${n("prod")}&ns=${n("bronze")}`);
  await page.getByRole("button", { name: /^Actions for bronze/ }).click();
  await page.getByRole("menuitem", { name: "New namespace…" }).click();
  const create = page.getByRole("dialog");
  await create.getByRole("textbox", { name: "Path" }).pressSequentially("landing");
  const button = create.getByRole("button", { name: "Create" });
  await expect(button).not.toHaveAttribute("data-pending");
  await button.click();
  await expect(page.locator(".catalog-result", { hasText: /^Created namespace/ })).toBeVisible();
  await expectMarked(page, "the note after a create");

  await visit(page, `/catalog?env=${n("prod")}&ns=${n("gold.sales")}`);
  await page.getByRole("button", { name: /^Actions for gold\.sales/ }).click();
  await page.getByRole("menuitem", { name: "New external table…" }).click();
  const refused = page.getByRole("dialog");
  await refused.getByRole("textbox", { name: "Name" }).fill("orders");
  await refused.getByRole("textbox", { name: "Location" }).fill("raw/none/*.csv");
  const register = refused.getByRole("button", { name: "Register" });
  await expect(register).not.toHaveAttribute("data-pending");
  await register.click();
  await expect(refused.getByRole("alert")).toBeVisible();
  await expectMarked(page, "a refused registration");
});
