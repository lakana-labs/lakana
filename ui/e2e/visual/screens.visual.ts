import { expect, type Locator, type Page, test } from "@playwright/test";

import { stubAccess, stubPolicies, stubSignInAndConfiguration, stubTokensSecrets } from "../access";
import { stubArtifacts } from "../artifacts";
import { stubConnect } from "../connect";
import { stubConnections } from "../connections";
import { stubEnvironments } from "../environments";
import { FAILED_ID, MISSING_ID, stubJobs, TIMED_OUT } from "../jobs";
import { stubManagement } from "../manage";
import { stubPlaces } from "../places";
import { SALES_SQL, stubQueries, WEEKLY } from "../queries";
import { stubRepositories } from "../repositories";
import { ADMIN_SESSION, signInAsAdmin } from "../session";

// Screenshots of every screen of the interface in light and dark, compared with the committed
// baselines in screenshots/ (docs/agent/testing.md, "Visual regression"). Run only inside the
// pinned Playwright image (`npm --prefix ui run visual`), so the fonts and the rendering are the
// same on every machine; `npm --prefix ui run visual:update` writes new baselines, which are
// reviewed like code.

/** The moment every screen is shown at: after the stubs' times, so "ago" reads the same. */
const NOW = Date.parse("2026-11-18T10:30:00Z");

// The connection stubs write their times from the clock of this process ("an hour ago");
// fixed, they give the same answer on every run, as the browser's fixed clock does.
const RealDate = Date;
class FixedDate extends RealDate {
  constructor(...args: unknown[]) {
    if (args.length === 0) {
      super(NOW);
    } else {
      // @ts-expect-error -- Date's overloads take what the caller gave.
      super(...args);
    }
  }

  static override now(): number {
    return NOW;
  }
}
globalThis.Date = FixedDate as DateConstructor;

/** One screenshot: a screen, or a state of one. */
interface Shot {
  /** The file's name, without `.png`. */
  readonly name: string;
  /** Answers the screen's routes like a server would. */
  readonly stub: (page: Page) => Promise<unknown>;
  /** Its address. */
  readonly path: string;
  /** What is visible once the screen has loaded. */
  readonly ready: (page: Page) => Locator;
  /** What to do before the picture: open a dialog or a menu, or show a state. */
  readonly act?: (page: Page) => Promise<void>;
  /** Also taken at the phone width. */
  readonly phone?: boolean;
}

/**
 * Signs in as an administrator, then answers the routes of more stubs.
 *
 * @param stubs - The screen's stubs.
 * @returns What a shot calls with its page.
 */
function admin(...stubs: ((page: Page) => Promise<unknown>)[]): (page: Page) => Promise<void> {
  return async (page) => {
    await signInAsAdmin(page);
    for (const stub of stubs) {
      await stub(page);
    }
  };
}

/**
 * The page's first-level heading.
 *
 * @param name - Its text.
 * @returns A locator for it.
 */
function heading(name: string | RegExp): (page: Page) => Locator {
  return (page) => page.getByRole("heading", { level: 1, name });
}

/**
 * Some text on the page.
 *
 * @param text - The text.
 * @returns A locator for its first match.
 */
function text(text: string | RegExp): (page: Page) => Locator {
  return (page) => page.getByText(text).filter({ visible: true }).first();
}

/**
 * Presses a button and waits for the dialog it opens.
 *
 * @param name - The button's accessible name.
 * @returns What a shot calls with its page.
 */
function dialog(name: string | RegExp): (page: Page) => Promise<void> {
  return async (page) => {
    await page.getByRole("button", { name }).first().click();
    await expect(page.getByRole("dialog")).toBeVisible();
  };
}

/**
 * Opens a menu and chooses one of its items.
 *
 * @param menu - The menu button's accessible name.
 * @param item - The item, or none to show the open menu.
 * @returns What a shot calls with its page.
 */
function menu(menu: string | RegExp, item?: string): (page: Page) => Promise<void> {
  return async (page) => {
    await page.getByRole("button", { name: menu }).first().click();
    await expect(page.getByRole("menu")).toBeVisible();
    if (item !== undefined) {
      await page.getByRole("menuitem", { name: item }).click();
      await expect(page.getByRole("dialog")).toBeVisible();
    }
  };
}

/**
 * Answers one API path with a server error, after the other stubs.
 *
 * @param path - The path, as a glob.
 * @returns A stub.
 */
function failing(path: string): (page: Page) => Promise<void> {
  return async (page) => {
    await page.route(path, (route) =>
      route.fulfill({
        status: 503,
        json: { type: "ServiceUnavailable", detail: "The catalog database does not answer." },
      }),
    );
  };
}

/**
 * Answers one API path with an empty page of a list, after the other stubs.
 *
 * @param path - The path.
 * @returns A stub.
 */
function empty(path: string): (page: Page) => Promise<void> {
  return async (page) => {
    await page.route(
      (url) => url.pathname === path,
      (route) => route.fulfill({ json: { items: [], next_cursor: null } }),
    );
  };
}

/**
 * A route that never answers, after the other stubs: the screen stays in its loading state.
 *
 * @param path - The path, as a glob.
 * @returns A stub.
 */
function pending(path: string): (page: Page) => Promise<void> {
  return async (page) => {
    await page.route(path, () => undefined);
  };
}

/**
 * Answers the session as signed out, with an identity provider to sign in with.
 *
 * @param page - The page.
 */
async function signedOut(page: Page): Promise<void> {
  await page.route("**/api/v1/sign-in/session", (route) =>
    route.fulfill({ status: 401, json: { detail: "Sign in.", type: "AuthenticationError" } }),
  );
  await page.route("**/api/v1/sign-in/oidc", (route) =>
    route.fulfill({ json: { enabled: true, display_name: "Okta" } }),
  );
}

/**
 * Signs in a person who has no access to anything yet.
 *
 * @param page - The page.
 */
async function noAccess(page: Page): Promise<void> {
  await signInAsAdmin(page);
  await page.route("**/api/v1/sign-in/session", (route) =>
    route.fulfill({ json: { ...ADMIN_SESSION, name: "kim.schulz" } }),
  );
  await page.route("**/api/v1/whoami", (route) =>
    route.fulfill({
      json: {
        name: "kim.schulz",
        type: "USER",
        task_token: null,
        ci_token: null,
        language: null,
        admin: false,
        has_access: false,
        may_deploy: false,
        secret_environments: [],
        manage_policy: false,
        reads_environments: false,
        instance_actions: [],
        managed_environments: [],
        picture: null,
      },
    }),
  );
}

const TABLE = "/catalog/prod/gold.sales/fact_sales";
const ARTIFACT = "/catalog/prod/ml/model";
const RUN = `/jobs/prod/runs/${FAILED_ID}`;
const CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";

const SHOTS: readonly Shot[] = [
  // Signing in.
  { name: "sign-in", stub: signedOut, path: "/sign-in", ready: text("Okta"), phone: true },
  {
    name: "sign-in-invite",
    stub: async (page) => {
      await signedOut(page);
      await page.route("**/api/v1/sign-in/links/describe", (route) =>
        route.fulfill({
          json: { kind: "invite", name: "ada.berg", display_name: "Ada Berg", state: "open" },
        }),
      );
    },
    path: "/sign-in#l=pub-1&s=sec-1",
    ready: text("Ada Berg"),
  },
  {
    name: "sign-in-cli",
    stub: signInAsAdmin,
    path: `/sign-in/cli?port=49152&state=s-1&challenge=${CHALLENGE}&host=laptop`,
    ready: text("laptop"),
  },
  {
    name: "sign-in-device",
    stub: signInAsAdmin,
    path: "/sign-in/device",
    ready: (page) => page.getByRole("textbox", { name: "Code" }),
  },

  // Home.
  { name: "home", stub: signInAsAdmin, path: "/", ready: text("daily_sales"), phone: true },
  { name: "home-no-access", stub: noAccess, path: "/", ready: text(/no access yet/) },
  {
    name: "home-palette",
    stub: signInAsAdmin,
    path: "/",
    ready: text("daily_sales"),
    act: async (page) => {
      await page.getByRole("button", { name: "Search" }).click();
      await expect(page.getByRole("dialog", { name: "Search" })).toBeVisible();
    },
  },
  {
    name: "home-user-menu",
    stub: signInAsAdmin,
    path: "/",
    ready: text("daily_sales"),
    act: menu("Signed in as admin"),
  },
  {
    name: "home-shortcuts",
    stub: signInAsAdmin,
    path: "/",
    ready: text("daily_sales"),
    act: async (page) => {
      await page.getByRole("button", { name: "Help" }).click();
      await page.getByRole("menuitem", { name: "Keyboard shortcuts" }).click();
      await expect(page.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
    },
  },
  { name: "not-found", stub: signInAsAdmin, path: "/nowhere", ready: heading(/./) },
  { name: "catalog-not-available", stub: noAccess, path: "/catalog", ready: heading(/./) },

  // Catalog.
  {
    name: "catalog",
    stub: signInAsAdmin,
    path: "/catalog?env=prod&ns=gold.sales",
    ready: (page) => page.getByRole("region", { name: "Tables" }),
    phone: true,
  },
  {
    name: "catalog-search",
    stub: signInAsAdmin,
    path: "/catalog?q=store",
    ready: text("store_id"),
  },
  {
    name: "catalog-no-hits",
    stub: signInAsAdmin,
    path: "/catalog?q=nothing",
    ready: text(/No table in .* matches/),
  },
  {
    name: "catalog-empty-namespace",
    stub: signInAsAdmin,
    path: "/catalog?env=prod&ns=bronze",
    ready: text("No tables in bronze yet"),
  },
  {
    name: "catalog-error",
    stub: admin(failing("**/api/v1/tables?*")),
    path: "/catalog?env=prod&ns=gold.sales",
    ready: text("The catalog database does not answer."),
  },
  {
    name: "catalog-dropped",
    stub: stubManagement,
    path: "/catalog?env=prod&view=dropped",
    ready: text("fact_returns"),
  },
  {
    name: "catalog-expiring",
    stub: stubManagement,
    path: "/catalog?env=prod&view=expiring",
    ready: text("late_arrival"),
  },
  {
    name: "catalog-orphans",
    stub: stubManagement,
    path: "/catalog?env=prod&view=orphans",
    ready: text("clean"),
  },
  {
    name: "catalog-new-namespace",
    stub: stubPlaces,
    path: "/catalog?env=prod&ns=bronze",
    ready: text("bronze"),
    act: menu(/^Actions for bronze/, "New namespace…"),
  },

  // A table's page and its tabs.
  {
    name: "table-overview",
    stub: signInAsAdmin,
    path: TABLE,
    ready: text("Every sale in every store, one row per sale."),
    phone: true,
  },
  { name: "table-columns", stub: signInAsAdmin, path: `${TABLE}/columns`, ready: text("store_id") },
  {
    name: "table-preview",
    stub: signInAsAdmin,
    path: `${TABLE}/preview`,
    ready: text("customer_email"),
  },
  {
    name: "table-lineage",
    stub: signInAsAdmin,
    path: `${TABLE}/lineage`,
    ready: text("gold.sales.daily_sales"),
  },
  {
    name: "table-references",
    stub: signInAsAdmin,
    path: `${TABLE}/references`,
    ready: text("gold.dim_store"),
  },
  {
    name: "table-health",
    stub: signInAsAdmin,
    path: `${TABLE}/health`,
    ready: text("unique_sale_id"),
  },
  { name: "table-access", stub: signInAsAdmin, path: `${TABLE}/access`, ready: text("kim.schulz") },
  {
    name: "table-loading",
    stub: admin(pending("**/api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales")),
    path: TABLE,
    ready: (page) => page.locator(".table-skeleton-tabs"),
  },
  {
    name: "table-actions",
    stub: stubManagement,
    path: TABLE,
    ready: heading("fact_sales"),
    act: menu(/^Actions$/),
  },
  {
    name: "table-retention",
    stub: stubManagement,
    path: TABLE,
    ready: heading("fact_sales"),
    act: menu(/^Actions$/, "Retention…"),
  },

  // An artifact's page.
  { name: "artifact-overview", stub: stubArtifacts, path: ARTIFACT, ready: text("ana.nowak") },
  {
    name: "artifact-versions",
    stub: stubArtifacts,
    path: `${ARTIFACT}/versions`,
    ready: (page) => page.getByRole("button", { name: /^Files of version 2/ }),
  },

  // Queries.
  {
    name: "queries",
    stub: stubQueries,
    path: "/queries",
    ready: (page) => page.getByRole("textbox", { name: "SQL" }),
    phone: true,
  },
  {
    name: "queries-result",
    stub: stubQueries,
    path: "/queries",
    ready: (page) => page.getByRole("textbox", { name: "SQL" }),
    act: async (page) => {
      await page.getByRole("textbox", { name: "SQL" }).click();
      await page.keyboard.press("ControlOrMeta+A");
      await page.keyboard.press("Backspace");
      await page.keyboard.insertText(SALES_SQL);
      await page.keyboard.press("ControlOrMeta+Enter");
      await expect(page.getByRole("button", { name: "Download spreadsheet" })).toBeVisible();
    },
  },
  {
    name: "queries-saved",
    stub: stubQueries,
    path: `/queries/prod/saved/${WEEKLY.id}`,
    ready: text(WEEKLY.name),
  },

  // Jobs.
  { name: "jobs", stub: stubJobs, path: "/jobs", ready: text("daily_sales"), phone: true },
  { name: "jobs-runs", stub: stubJobs, path: "/jobs/runs", ready: text("daily_sales") },
  {
    name: "jobs-deploys",
    stub: stubJobs,
    path: "/jobs/deploys?environment=prod",
    ready: text("ana.nowak"),
  },
  {
    name: "jobs-deploys-empty",
    stub: stubJobs,
    path: "/jobs/deploys?environment=dev",
    ready: text("No deploy in dev yet."),
  },
  {
    name: "job",
    stub: stubJobs,
    path: "/jobs/prod/job/analytics/daily_sales",
    ready: text("Waits for silver.orders to finish."),
  },
  {
    name: "job-pause",
    stub: stubJobs,
    path: "/jobs/prod/job/analytics/daily_sales",
    ready: text("Waits for silver.orders to finish."),
    act: dialog("Pause schedule"),
  },
  { name: "run", stub: stubJobs, path: RUN, ready: text(TIMED_OUT), phone: true },
  {
    name: "run-task",
    stub: stubJobs,
    path: `${RUN}/tasks/${TIMED_OUT}`,
    ready: text("The log is complete."),
  },
  {
    name: "run-missing",
    stub: stubJobs,
    path: `/jobs/prod/runs/${MISSING_ID}`,
    ready: text("does not exist"),
  },
  {
    name: "jobs-rollback",
    stub: stubJobs,
    path: "/jobs/deploys?environment=prod",
    ready: text("ana.nowak"),
    act: dialog(/^Roll back to this… \(analytics/),
  },

  // Connections.
  {
    name: "connections",
    stub: stubConnections,
    path: "/connections",
    ready: text("warehouse"),
    phone: true,
  },
  {
    name: "connections-empty",
    stub: async (page) => {
      await stubConnections(page);
      await empty("/api/v1/connections")(page);
    },
    path: "/connections",
    ready: text("No connections yet."),
  },
  {
    name: "connection",
    stub: stubConnections,
    path: "/connections/dev/exports",
    ready: text("bronze.exports"),
  },
  {
    name: "connection-edit",
    stub: stubConnections,
    path: "/connections/dev/exports/edit",
    ready: heading("Edit"),
  },
  {
    name: "connection-new",
    stub: stubConnections,
    path: "/connections/new",
    ready: heading(/./),
  },
  {
    name: "connection-delete",
    stub: stubConnections,
    path: "/connections/dev/scratch",
    ready: text("scratch"),
    act: dialog("Delete…"),
  },

  // Settings.
  {
    name: "settings-status",
    stub: admin(stubRepositories),
    path: "/settings/status",
    ready: text("worker-1.internal"),
    phone: true,
  },
  {
    name: "settings-environments",
    stub: admin(stubEnvironments),
    path: "/settings/environments",
    ready: text("s3://corp-lake/prod"),
  },
  {
    name: "settings-environment",
    stub: admin(stubEnvironments),
    path: "/settings/environments/dev",
    ready: text("file:///srv/lakana/dev"),
  },
  {
    name: "settings-environment-storage",
    stub: admin(stubEnvironments),
    path: "/settings/environments/dev",
    ready: text("file:///srv/lakana/dev"),
    act: dialog(/^Change storage of dev/),
  },
  {
    name: "settings-repositories",
    stub: admin(stubRepositories),
    path: "/settings/repositories",
    ready: text("dbt-models"),
  },
  {
    name: "settings-repository",
    stub: admin(stubRepositories),
    path: "/settings/repositories/analytics",
    ready: text("lakana analytics dev"),
  },
  {
    name: "settings-repository-new",
    stub: admin(stubConnect),
    path: "/settings/repositories/new",
    ready: (page) => page.getByRole("textbox", { name: "Repository URL" }),
  },
  {
    name: "settings-people",
    stub: admin(stubAccess),
    path: "/settings/access/people",
    ready: (page) => page.locator(".list tbody tr").first(),
  },
  {
    name: "settings-person",
    stub: admin(stubAccess),
    path: "/settings/access/people/maria",
    ready: text("analysts"),
  },
  {
    name: "settings-groups",
    stub: admin(stubAccess),
    path: "/settings/access/groups",
    ready: text("analysts"),
  },
  {
    name: "settings-group",
    stub: admin(stubAccess),
    path: "/settings/access/groups/analysts",
    ready: text("ci-bot"),
  },
  {
    name: "settings-grants",
    stub: admin(stubAccess),
    path: "/settings/access/grants",
    ready: (page) => page.getByRole("region", { name: "Grants" }),
  },
  {
    name: "settings-grants-add",
    stub: admin(stubAccess),
    path: "/settings/access/grants",
    ready: (page) => page.getByRole("region", { name: "Grants" }),
    act: dialog("Add grant"),
  },
  {
    name: "settings-what-i-may-do",
    stub: admin(stubAccess),
    path: "/settings/access/what-i-may-do",
    ready: text("analysts"),
  },
  {
    name: "settings-tokens",
    stub: admin(stubTokensSecrets),
    path: "/settings/access/tokens",
    ready: text("ci-bot"),
  },
  {
    name: "settings-secrets",
    stub: admin(stubTokensSecrets),
    path: "/settings/access/secrets?environment=dev",
    ready: text("pypi-corp"),
  },
  {
    name: "settings-filters-and-masks",
    stub: admin(stubPolicies),
    path: "/settings/access/filters-and-masks",
    ready: (page) => page.locator(".policies-scroll tbody tr").first(),
  },
  {
    name: "settings-access-file",
    stub: admin(async (page) => {
      await stubPolicies(page, { managed: ["prod"], overrides: true });
    }),
    path: "/settings/access/access-file",
    ready: text("access.yaml"),
  },
  {
    name: "settings-sign-in",
    stub: admin(stubSignInAndConfiguration),
    path: "/settings/sign-in",
    ready: heading(/./),
  },
  {
    name: "settings-invites",
    stub: admin(stubSignInAndConfiguration),
    path: "/settings/sign-in/invites",
    ready: (page) => page.locator(".list-toolbar").first(),
  },
  {
    name: "settings-recovery-links",
    stub: admin(stubSignInAndConfiguration),
    path: "/settings/sign-in/recovery-links",
    ready: (page) => page.locator(".list-toolbar").first(),
  },
  {
    name: "settings-configuration",
    stub: admin(stubSignInAndConfiguration),
    path: "/settings/configuration",
    ready: (page) => page.locator(".list-toolbar").first(),
  },

  // One's own account.
  { name: "account", stub: signInAsAdmin, path: "/account", ready: heading(/./) },
];

for (const shot of SHOTS) {
  // The phone projects of playwright.visual.config.ts take only the shots tagged @phone.
  test(shot.name, { tag: shot.phone === true ? ["@phone"] : [] }, async ({ page }) => {
    await page.clock.setFixedTime(NOW);
    await shot.stub(page);
    await page.goto(shot.path);
    await expect(shot.ready(page)).toBeVisible({ timeout: 10_000 });
    if (shot.act !== undefined) {
      await shot.act(page);
    }
    // The fonts are part of the picture.
    await page.evaluate(() => document.fonts.ready);
    await expect(page).toHaveScreenshot(`${shot.name}.png`, { fullPage: true });
  });
}
