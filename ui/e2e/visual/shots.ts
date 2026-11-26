/**
 * The screenshots of the visual regression suite: every screen and its important states, each
 * with the stubs that answer it (docs/agent/testing.md, "Visual regression").
 *
 * @packageDocumentation
 */
import { expect, type Locator, type Page } from "@playwright/test";

import { stubAccess, stubPolicies, stubSignInAndConfiguration, stubTokensSecrets } from "../access";
import { stubAudit } from "../audit";
import { stubArtifacts } from "../artifacts";
import { PROD_RULE, stubCiTrust } from "../ciTrust";
import { stubConnect } from "../connect";
import { stubConnections } from "../connections";
import { stubEnvironments } from "../environments";
import {
  FAILED_ID,
  keepRollbackRunning,
  MISSING_ID,
  stubJobs,
  stubWaitingJob,
  TIMED_OUT,
} from "../jobs";
import { stubManagement } from "../manage";
import { stubPlaces } from "../places";
import { SALES_SQL, stubQueries, WEEKLY } from "../queries";
import { stubRepositories } from "../repositories";
import { ADMIN_SESSION, signInAsAdmin } from "../session";

/** One screenshot: a screen, or a state of one. */
export interface Shot {
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
  /** Taken at the phone width only: what only a phone shows, such as its Menu. */
  readonly phoneOnly?: boolean;
  /**
   * A loading state: a request stays open on purpose, so the shot does not wait for the
   * network to go quiet.
   */
  readonly loadingState?: boolean;
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

/**
 * Answers the session as signed out, without an identity provider: passkeys only.
 *
 * @param page - The page.
 */
async function passkeysOnly(page: Page): Promise<void> {
  await signedOut(page);
  await page.route("**/api/v1/sign-in/oidc", (route) =>
    route.fulfill({ json: { enabled: false, display_name: null } }),
  );
}

/** The identity provider's refusal, as the server answers it. */
const REFUSED = {
  status: 403,
  json: { detail: "Refused: not_linked.", type: "OidcSignInRefused", reason: "not_linked" },
};

/**
 * Answers the end of a trip to the identity provider: never (the page waits), or refused.
 *
 * @param path - The finishing route.
 * @param refused - Whether the server refuses it.
 * @returns A stub, after the session's.
 */
function finish(path: string, refused: boolean): (page: Page) => Promise<void> {
  return async (page) => {
    await page.route(
      (url) => url.pathname === path,
      (route) => (refused ? route.fulfill(REFUSED) : undefined),
    );
  };
}

/**
 * Answers the interface state as for a first sign-in: the tour not yet seen.
 *
 * @param page - The page.
 */
async function firstSignIn(page: Page): Promise<void> {
  await signInAsAdmin(page);
  await page.route(
    (url) => url.pathname === "/api/v1/principals/admin/interface-state",
    (route) =>
      route.fulfill({
        json: { tour_seen: false, get_started_hidden: true, update_note_seen: null },
      }),
  );
}

const TABLE = "/catalog/prod/gold.sales/fact_sales";
const ARTIFACT = "/catalog/prod/ml/model";
const RUN = `/jobs/prod/runs/${FAILED_ID}`;
const CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";

/** Every shot, in the order the docs list them. */
export const SHOTS: readonly Shot[] = [
  // Signing in.
  { name: "sign-in", stub: signedOut, path: "/sign-in", ready: text("Okta"), phone: true },
  {
    name: "sign-in-passkeys-only",
    stub: passkeysOnly,
    path: "/sign-in",
    ready: (page) => page.getByRole("button").first(),
  },
  {
    name: "sign-in-oidc-finishing",
    stub: async (page) => {
      await signedOut(page);
      await finish("/api/v1/sign-in/oidc/finish", false)(page);
    },
    path: "/sign-in/oidc",
    ready: (page) => page.getByRole("status"),
    loadingState: true,
  },
  {
    name: "sign-in-oidc-refused",
    stub: async (page) => {
      await signedOut(page);
      await finish("/api/v1/sign-in/oidc/finish", true)(page);
    },
    path: "/sign-in/oidc",
    ready: (page) => page.getByRole("button", { name: "Try again" }),
  },
  {
    name: "sign-in-oidc-confirming",
    stub: admin(finish("/api/v1/sign-in/oidc/finish-in-session", false)),
    path: "/sign-in/oidc/confirmed",
    ready: (page) => page.getByRole("status"),
    loadingState: true,
  },
  {
    name: "sign-in-oidc-confirm-refused",
    stub: admin(finish("/api/v1/sign-in/oidc/finish-in-session", true)),
    path: "/sign-in/oidc/confirmed",
    ready: (page) => page.getByRole("button", { name: "Try again" }),
  },
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
    name: "sign-in-recovery",
    stub: async (page) => {
      await signedOut(page);
      await page.route("**/api/v1/sign-in/links/describe", (route) =>
        route.fulfill({
          json: { kind: "recovery", name: "kim.schulz", display_name: "Kim Schulz", state: "open" },
        }),
      );
    },
    path: "/sign-in#l=pub-2&s=sec-2",
    ready: text("Kim Schulz"),
  },
  {
    name: "sign-in-link-used",
    stub: async (page) => {
      await signedOut(page);
      await page.route("**/api/v1/sign-in/links/describe", (route) =>
        route.fulfill({
          status: 410,
          json: { type: "LinkUnusable", detail: "This link was used.", state: "used" },
        }),
      );
    },
    path: "/sign-in#l=pub-3&s=sec-3",
    ready: heading("This link was already used"),
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
    name: "home-tour-welcome",
    stub: firstSignIn,
    path: "/",
    ready: (page) => page.getByRole("dialog", { name: "Welcome to Lakana" }),
  },
  {
    name: "home-tour-step",
    stub: firstSignIn,
    path: "/",
    ready: (page) => page.getByRole("dialog", { name: "Welcome to Lakana" }),
    act: async (page) => {
      await page.getByRole("button", { name: "Start the tour" }).press("Enter");
      await expect(page.getByRole("button", { name: "Next" })).toBeVisible();
    },
  },
  {
    name: "home-menu",
    stub: signInAsAdmin,
    path: "/",
    ready: text("daily_sales"),
    act: async (page) => {
      await page.getByRole("button", { name: "Menu" }).click();
      await expect(page.getByRole("link", { name: "Settings" })).toBeVisible();
    },
    phoneOnly: true,
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
    loadingState: true,
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
  {
    name: "artifact-lineage",
    stub: stubArtifacts,
    path: `${ARTIFACT}/lineage`,
    ready: text(/Nothing upstream or downstream/),
  },
  {
    name: "artifact-access",
    stub: stubArtifacts,
    path: `${ARTIFACT}/access`,
    ready: text("kim.schulz"),
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
      // Typing opens the completions; the result is the picture, not them.
      const completions = page.locator(".cm-tooltip-autocomplete");
      await expect(completions).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(completions).toBeHidden();
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
  {
    name: "job-paused",
    stub: stubJobs,
    path: "/jobs/prod/job/analytics/daily_sales",
    ready: text("Waits for silver.orders to finish."),
    act: async (page) => {
      await dialog("Pause schedule")(page);
      await page.getByRole("dialog").getByRole("button", { name: "Pause schedule" }).click();
      await expect(page.getByText(/· paused since .+ by admin$/)).toBeVisible();
    },
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
  {
    // A rollback followed: every Roll back to this… held, the one pressed focused by keyboard.
    name: "jobs-rollback-held",
    stub: async (page) => {
      await stubJobs(page);
      await keepRollbackRunning(page);
    },
    path: "/jobs/deploys?environment=prod",
    ready: text("ana.nowak"),
    act: async (page) => {
      await page
        .getByRole("button", { name: /^Roll back to this… \(analytics/ })
        .first()
        .focus();
      await page.keyboard.press("Enter");
      const ask = page.getByRole("dialog");
      await ask.getByRole("button", { name: "Roll back", exact: true }).focus();
      await page.keyboard.press("Enter");
      await expect(page.getByText(/^Rolling back analytics in prod to /)).toBeVisible();
    },
  },
  {
    // The undeploy's question: what goes and what stays, Undeploy held until the name is typed.
    name: "jobs-undeploy",
    stub: async (page) => {
      await stubJobs(page);
      await stubWaitingJob(page);
    },
    path: "/jobs/deploys?environment=prod",
    ready: text("ana.nowak"),
    act: async (page) => {
      await page.getByRole("button", { name: /^Undeploy… \(analytics/ }).click();
      await expect(page.getByRole("dialog").getByText(/^hourly_stock: removed/)).toBeVisible();
    },
  },
  {
    // After the undeploy: nothing current, nothing offered, the note focused by keyboard.
    name: "jobs-undeployed",
    stub: stubJobs,
    path: "/jobs/deploys?environment=prod",
    ready: text("ana.nowak"),
    act: async (page) => {
      await page.getByRole("button", { name: /^Undeploy… \(analytics/ }).focus();
      await page.keyboard.press("Enter");
      const ask = page.getByRole("dialog");
      await ask.getByRole("textbox").fill("analytics");
      await ask.getByRole("textbox").press("Enter");
      await expect(page.getByText(/^Undeployed analytics from prod\./)).toBeVisible();
      await expect(page.locator(".jobs-rollback-note")).toBeFocused();
    },
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
    name: "settings-ci-trust",
    stub: admin(stubCiTrust),
    path: "/settings/ci-trust",
    ready: text("ci-prod"),
    phone: true,
  },
  {
    name: "settings-ci-trust-add",
    stub: admin(stubCiTrust),
    path: "/settings/ci-trust",
    ready: text("ci-prod"),
    act: dialog("Add rule"),
  },
  {
    name: "settings-ci-trust-rule",
    stub: admin(stubCiTrust),
    path: `/settings/ci-trust/${PROD_RULE}`,
    ready: text("refs/heads/main"),
  },
  {
    name: "settings-ci-trust-rule-managed",
    stub: admin((page) => stubCiTrust(page, ["prod"])),
    path: `/settings/ci-trust/${PROD_RULE}`,
    ready: text(/is managed by/),
  },
  {
    name: "settings-ci-trust-edit",
    stub: admin(stubCiTrust),
    path: `/settings/ci-trust/${PROD_RULE}`,
    ready: text("refs/heads/main"),
    act: dialog(/^Edit the rule for analytics/),
  },
  {
    name: "settings-ci-trust-explain",
    stub: admin(stubCiTrust),
    path: "/settings/ci-trust/explain",
    ready: heading("Explain a CI job's claims"),
    act: async (page) => {
      await page
        .getByRole("textbox", { name: "Claims" })
        .fill('{"iss": "https://token.actions.githubusercontent.com", "ref": "refs/heads/main"}');
      await page.getByRole("button", { name: /Environment$/ }).click();
      await page.getByRole("option", { name: /^prod/ }).click();
      await page.getByRole("button", { name: /Repository$/ }).click();
      await page.getByRole("option", { name: "analytics" }).click();
      await page.getByRole("button", { name: "Explain" }).click();
      await expect(page.getByRole("region", { name: "Every rule" })).toBeVisible();
    },
  },
  {
    name: "settings-environment-run-as",
    stub: admin(stubEnvironments),
    path: "/settings/environments/prod",
    ready: text("etl-prod"),
    act: dialog(/^Change the run-as account of analytics/),
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
    name: "settings-people-new-service-account",
    stub: admin(stubAccess),
    path: "/settings/access/people",
    ready: (page) => page.locator(".list tbody tr").first(),
    act: dialog("New service account"),
    phone: true,
  },
  {
    name: "settings-person-deactivate",
    stub: admin(stubAccess),
    path: "/settings/access/people/maria",
    ready: text("analysts"),
    act: dialog("Deactivate maria"),
    phone: true,
  },
  {
    name: "settings-person-deactivated",
    stub: admin(stubAccess),
    path: "/settings/access/people/ben",
    ready: text("ben is deactivated"),
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
  {
    name: "settings-audit",
    stub: admin(stubAudit),
    path: "/settings/audit",
    ready: text("QUERY_COMPLETED"),
  },
  {
    name: "settings-audit-details",
    stub: admin(stubAudit),
    path: "/settings/audit",
    ready: text("QUERY_COMPLETED"),
    act: async (page) => {
      await page.getByRole("button", { name: /^Show details of QUERY_COMPLETED/ }).click();
      await page.getByText("tables_touched").waitFor();
    },
  },

  // One's own account.
  { name: "account", stub: signInAsAdmin, path: "/account", ready: heading(/./) },
];

/** The projects of playwright.visual.config.ts that take every shot. */
export const DESKTOP_PROJECTS = ["light", "dark"] as const;

/** The projects that take only the shots marked `phone`. */
export const PHONE_PROJECTS = ["phone-light", "phone-dark"] as const;
