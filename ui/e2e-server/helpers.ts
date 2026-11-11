/**
 * Helpers for the end-to-end tests against a real lakana-server: links minted with the
 * operator's bootstrap token, and a virtual authenticator.
 *
 * @packageDocumentation
 */
import type { APIRequestContext, BrowserContext, CDPSession, Page } from "@playwright/test";

/** The bootstrap administrator's token, as `playwright.server.config.ts` fixes it. */
const BOOTSTRAP_TOKEN = "lakana_pat_e2ekey_e2esecret";

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
  if (!response.ok()) {
    throw new Error(`POST ${path} answered ${String(response.status())}: ${await response.text()}`);
  }
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

/**
 * Records that the bootstrap administrator saw the tour, so the welcome does not open in a test
 * about something else.
 *
 * @param request - The request context.
 */
export async function skipTheTour(request: APIRequestContext): Promise<void> {
  const response = await request.patch("/api/v1/principals/admin/interface-state", {
    headers: { Authorization: `Bearer ${BOOTSTRAP_TOKEN}` },
    data: { tour_seen: true },
  });
  if (!response.ok()) {
    throw new Error(`PATCH interface-state answered ${String(response.status())}`);
  }
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
