/**
 * Helpers for the end-to-end tests against a real lakana-server: links minted with the
 * operator's bootstrap token, and a virtual authenticator.
 *
 * @packageDocumentation
 */
import { resolve } from "node:path";

import AxeBuilder from "@axe-core/playwright";
import {
  type APIRequestContext,
  type APIResponse,
  type Browser,
  type BrowserContext,
  type CDPSession,
  expect,
  type Page,
} from "@playwright/test";

/** The bootstrap administrator's token, as `playwright.server.config.ts` fixes it. */
const BOOTSTRAP_TOKEN = "lakana_pat_e2ekey_e2esecret";

/** The origin the e2e server answers on, as `playwright.server.config.ts` fixes it. */
export const ORIGIN = "http://localhost:4174";

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

// React Aria's live announcer (a pending button speaks through it) keeps its message for 7 s and
// names it by the ids of the button's parts, which may be gone by then; it is the library's
// transient node, not the page.
const LIVE_ANNOUNCER = "[data-live-announcer]";

/**
 * Opens a browser that has never visited the server.
 *
 * @param browser - The browser.
 * @returns A page of a fresh context.
 */
export async function freshPage(browser: Browser): Promise<Page> {
  const context = await browser.newContext({ baseURL: ORIGIN });
  return context.newPage();
}

/**
 * Runs axe over the page with the WCAG 2.2 AA tags.
 *
 * @param page - The page to check.
 * @returns The violations, each with the elements it found.
 */
export async function violations(page: Page): Promise<string[]> {
  const results = await new AxeBuilder({ page }).withTags(WCAG).exclude(LIVE_ANNOUNCER).analyze();
  return results.violations.map(
    (violation) =>
      `${violation.id}: ${violation.nodes.map((node) => node.target.join(" ")).join(", ")}`,
  );
}

/**
 * Throws with the answer's status and body unless it succeeded.
 *
 * @param what - The request, as the error names it.
 * @param response - The answer.
 */
async function ensureOk(what: string, response: APIResponse): Promise<void> {
  if (!response.ok()) {
    throw new Error(`${what} answered ${String(response.status())}: ${await response.text()}`);
  }
}

/**
 * Asks the API as the bootstrap administrator.
 *
 * @param request - The request context.
 * @param path - The API path.
 * @param body - The JSON body, if any.
 * @returns The answer's JSON.
 */
async function ask(request: APIRequestContext, path: string, body?: object): Promise<string> {
  const response = await request.post(path, {
    headers: { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` },
    ...(body === undefined ? {} : { data: body }),
  });
  await ensureOk(`POST ${path}`, response);
  return ((await response.json()) as { url: string }).url;
}

/**
 * Mints the link that signs the first administrator in. It works once, and only while the
 * administrator has no passkey.
 *
 * @param request - The request context.
 * @returns The link, with its secret after the `#`.
 */
export function setupLink(request: APIRequestContext): Promise<string> {
  return ask(request, "/api/v1/sign-in/setup-link");
}

/**
 * Mints the local sign-in link `lakana server start --local` opens: the first administrator's,
 * with their own token. It works once, for 60 seconds, and only offers a passkey.
 *
 * @param request - The request context.
 * @returns The link, with its secret after the `#`.
 */
export function localLink(request: APIRequestContext): Promise<string> {
  return ask(request, "/api/v1/sign-in/local-link");
}

/**
 * Mints an invite for a new person.
 *
 * @param request - The request context.
 * @param name - The name the person signs up as; unique in the run.
 * @returns The link, with its secret after the `#`.
 */
export function inviteLink(request: APIRequestContext, name: string): Promise<string> {
  return ask(request, "/api/v1/invites", { name });
}

/** What the web interface remembers for a person, as `/interface-state` answers it. */
export interface InterfaceState {
  /** Whether they saw the tour. */
  readonly tour_seen: boolean;
  /** Whether they hid Get started. */
  readonly get_started_hidden: boolean;
  /** The last update note they saw (a release version), if any. */
  readonly update_note_seen: string | null;
}

/**
 * Reads, and with `change` first changes, the bootstrap administrator's interface state, so
 * each test starts from the state it needs whatever ran before it. Only the administrator's:
 * the bootstrap token is theirs, and a person changes only their own.
 *
 * @param request - The request context.
 * @param change - The fields to change; without it, the state is only read.
 * @returns The state afterwards.
 */
export async function interfaceState(
  request: APIRequestContext,
  change?: Partial<InterfaceState>,
): Promise<InterfaceState> {
  const path = "/api/v1/principals/admin/interface-state";
  const headers = { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` };
  const response =
    change === undefined
      ? await request.get(path, { headers })
      : await request.patch(path, { headers, data: change });
  await ensureOk("interface-state", response);
  return (await response.json()) as InterfaceState;
}

/**
 * Records that the bootstrap administrator saw the tour, so the welcome does not open in a test
 * about something else.
 *
 * @param request - The request context.
 */
export async function skipTheTour(request: APIRequestContext): Promise<void> {
  await interfaceState(request, { tour_seen: true });
}

/**
 * Gives an invited person the ADMIN role on the instance, as the bootstrap administrator.
 *
 * @param request - The request context.
 * @param name - The person.
 */
export async function makeAdministrator(request: APIRequestContext, name: string): Promise<void> {
  const response = await request.post("/api/v1/grants", {
    headers: { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` },
    data: { principal: name, role: "ADMIN", kind: "INSTANCE" },
  });
  await ensureOk("POST /api/v1/grants", response);
}

/**
 * Signs the bootstrap administrator in with the local link, as `lakana server start --local`
 * does, and waits for Home. The passkey offer comes only while they have none; it is declined,
 * as adding one would end the setup link a test of another file needs.
 *
 * @param page - A page of a fresh context.
 * @param request - The request context.
 */
export async function signInLocally(page: Page, request: APIRequestContext): Promise<void> {
  await page.goto(await localLink(request));
  const landed = page.getByRole("heading", { level: 1, name: /^(Add a passkey|Home)$/ });
  await expect(landed).toBeVisible();
  if ((await landed.textContent()) === "Add a passkey") {
    await page.getByRole("button", { name: "Not now" }).click();
  }
  await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
}

/**
 * A name no earlier test of this run used.
 *
 * @param prefix - The start of the name.
 * @returns The name.
 */
export function uniqueName(prefix: string): string {
  return `${prefix}-${Math.random().toString(36).slice(2, 8)}`;
}

/**
 * Adds a virtual authenticator (Chromium only) that holds resident keys and verifies users.
 *
 * @param context - The browser context.
 * @param page - A page of the context.
 * @returns The CDP session.
 */
export async function addAuthenticator(context: BrowserContext, page: Page): Promise<CDPSession> {
  const cdp = await context.newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: {
      protocol: "ctap2",
      transport: "internal",
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  });
  return cdp;
}

/**
 * Posts JSON as the bootstrap administrator and throws unless it succeeded.
 *
 * @param request - The request context.
 * @param path - The API path.
 * @param body - The JSON body.
 */
async function create(request: APIRequestContext, path: string, body: object): Promise<void> {
  const response = await request.post(path, {
    headers: { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` },
    data: body,
  });
  await ensureOk(`POST ${path}`, response);
}

/**
 * Creates an environment whose files live under `ui/.e2e-server/`, which the server's start
 * clears.
 *
 * @param request - The request context.
 * @param name - The environment's name; unique in the run.
 * @returns Resolves once the server answered.
 */
export function createEnvironment(request: APIRequestContext, name: string): Promise<void> {
  return create(request, "/api/v1/environments", {
    name,
    storage: {
      scheme: "file",
      root: `file://${resolve(import.meta.dirname, "..", ".e2e-server")}/lake-${name}`,
      options: {},
    },
  });
}

/**
 * Creates a namespace; its parent must exist.
 *
 * @param request - The request context.
 * @param environment - The environment.
 * @param path - The dotted path, such as `gold.sales`.
 * @returns Resolves once the server answered.
 */
export function createNamespace(
  request: APIRequestContext,
  environment: string,
  path: string,
): Promise<void> {
  return create(request, `/api/v1/environments/${environment}/namespaces`, { path });
}

/**
 * Creates a managed table.
 *
 * @param request - The request context.
 * @param environment - The environment.
 * @param namespace - The dotted namespace.
 * @param name - The table's name.
 * @param columns - The columns' names; each is a nullable string.
 * @returns Resolves once the server answered.
 */
export function createTable(
  request: APIRequestContext,
  environment: string,
  namespace: string,
  name: string,
  columns: readonly string[],
): Promise<void> {
  return create(request, `/api/v1/environments/${environment}/namespaces/${namespace}/tables`, {
    name,
    schema: columns.map((column) => ({ name: column, type: "string", nullable: true })),
  });
}

/**
 * Lets a person read one table.
 *
 * @param request - The request context.
 * @param principal - The person.
 * @param environment - The environment.
 * @param namespace - The dotted namespace.
 * @param table - The table's name.
 * @returns Resolves once the server answered.
 */
export function grantTableRead(
  request: APIRequestContext,
  principal: string,
  environment: string,
  namespace: string,
  table: string,
): Promise<void> {
  return create(request, "/api/v1/grants", {
    principal,
    action: "READ_TABLE",
    kind: "TABLE",
    environment,
    namespace,
    table,
  });
}

/**
 * Signs a newly invited person in with a passkey and waits for Home (Chromium only). `prepare`
 * runs once the person exists and before Home reads what they may do, so it is the place to
 * grant them something.
 *
 * @param browser - The browser.
 * @param request - The request context.
 * @param prepare - Called with the person's name before the passkey step.
 * @returns The page, on Home, and the person's name.
 */
export async function invitedPerson(
  browser: Browser,
  request: APIRequestContext,
  prepare: (name: string) => Promise<void>,
): Promise<{ page: Page; name: string }> {
  const name = uniqueName("ada");
  const page = await freshPage(browser);
  await addAuthenticator(page.context(), page);
  await page.goto(await inviteLink(request, name));
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Add a passkey" })).toBeVisible();
  // The person exists once the invite is redeemed.
  await prepare(name);
  await page.getByRole("button", { name: "Add a passkey" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
  return { page, name };
}

/**
 * Waits for the welcome of a person with access who has never seen the tour, skips it, and waits
 * until the server has recorded that, so that a page loaded next does not show it again.
 *
 * @param page - The page, on Home.
 */
export async function skipTheWelcome(page: Page): Promise<void> {
  const welcome = page.getByRole("dialog", { name: "Welcome to Lakana" });
  await expect(welcome).toBeVisible();
  // The tour is remembered by the server; a page loaded before the answer would show it again.
  const remembered = page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" && response.url().endsWith("/interface-state"),
  );
  await welcome.getByRole("button", { name: "Skip" }).click();
  await remembered;
  await expect(welcome).toBeHidden();
}

/**
 * Reads the state of a query run, as the bootstrap administrator.
 *
 * @param request - The request context.
 * @param environment - The environment.
 * @param queryId - The query's id.
 * @returns The state, such as `QUEUED` or `CANCELLED`.
 */
export async function queryStatus(
  request: APIRequestContext,
  environment: string,
  queryId: string,
): Promise<string> {
  const path = `/api/v1/environments/${environment}/queries/${queryId}`;
  const response = await request.get(path, {
    headers: { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` },
  });
  await ensureOk(`GET ${path}`, response);
  return ((await response.json()) as { status: string }).status;
}

/**
 * Signs the bootstrap administrator in on Home without the welcome.
 *
 * @param browser - The browser.
 * @param request - The request context.
 * @returns The page.
 */
export async function administrator(browser: Browser, request: APIRequestContext): Promise<Page> {
  await skipTheTour(request);
  const page = await freshPage(browser);
  await signInLocally(page, request);
  return page;
}
