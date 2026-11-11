/**
 * Helpers for the end-to-end tests against a real lakana-server: links minted with the
 * operator's bootstrap token, and a virtual authenticator.
 *
 * @packageDocumentation
 */
import {
  type APIRequestContext,
  type APIResponse,
  type BrowserContext,
  type CDPSession,
  expect,
  type Page,
} from "@playwright/test";

/** The bootstrap administrator's token, as `playwright.server.config.ts` fixes it. */
const BOOTSTRAP_TOKEN = "lakana_pat_e2ekey_e2esecret";

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
