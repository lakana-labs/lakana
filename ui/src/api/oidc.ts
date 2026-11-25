/**
 * Signing in through the organisation's identity provider (OIDC, #32): what the sign-in page and
 * a sign-in link's page need before anyone is signed in.
 *
 * @remarks
 * Not re-exported from `index.ts`, and kept to what the first load needs: whether the provider
 * is on and its name, and the two starts that need no session. The calls of the return pages,
 * the profile and an administrator are in `providerSession.ts`, which loads with those pages.
 * The page the person comes back to is kept on the server, never in the provider's URL.
 * @packageDocumentation
 */
import { api } from "./client";

/** Whether people sign in through a provider, as `GET /api/v1/sign-in/oidc` answers it. */
export interface OidcInfoOut {
  /** True while the server has an identity provider (`LAKANA_OIDC__ISSUER`). */
  readonly enabled: boolean;
  /** The provider's name for the sign-in button; null while it is off. */
  readonly display_name: string | null;
}

/** What starting a flow answers: the provider's sign-in page for it. */
interface OidcStartOut {
  /** Where to send the browser. */
  readonly authorization_url: string;
}

/** The `sessionStorage` key of the flow this tab started inside a session, for its return page. */
export const PROVIDER_TRIP_KEY = "lakana.oidc.trip";

/** What a tab remembers of a flow it started inside a session, for the page it comes back to. */
export interface ProviderTrip {
  /** `link` from the profile, or `step_up` for an action that needs a fresh confirmation. */
  readonly purpose: "link" | "step_up";
  /** The page the flow was started from, to go back to after a refusal. */
  readonly back: string;
}

/**
 * Thrown by an action that sent the browser to the provider instead of sending its request: the
 * page is about to be left, and nothing was sent.
 */
export class ProviderRedirect extends Error {
  /** Builds the error. */
  constructor() {
    super("The browser is on its way to the identity provider.");
    this.name = "ProviderRedirect";
  }
}

/**
 * Reads whether people sign in through a provider; a background read, before any sign-in.
 *
 * @returns Whether it is on, and its name.
 * @throws {ApiError} when the server cannot be read.
 */
export async function getProvider(): Promise<OidcInfoOut> {
  return api.request<OidcInfoOut>("GET", "/api/v1/sign-in/oidc", undefined, {
    background: true,
  });
}

/**
 * Opens a flow at the server and answers the provider's sign-in page for it.
 *
 * @param path - The start route.
 * @param body - Its body.
 * @returns The provider's sign-in page.
 * @throws {ApiError} for a refusal, such as `provider_unreachable`.
 */
export async function startFlow(path: string, body: unknown): Promise<string> {
  const started = await api.request<OidcStartOut>("POST", path, body);
  return started.authorization_url;
}

/**
 * Sends the browser to the provider: remembers the in-session flow for its return page first,
 * when there is one.
 *
 * @param url - The provider's sign-in page.
 * @param trip - The in-session flow, or undefined for a sign-in.
 * @returns The error the caller throws, so that nothing after it runs.
 */
export function goToProvider(url: string, trip?: ProviderTrip): ProviderRedirect {
  if (trip !== undefined) {
    try {
      sessionStorage.setItem(PROVIDER_TRIP_KEY, JSON.stringify(trip));
    } catch {
      // Storage is blocked: the return page then says the general words.
    }
  }
  window.location.assign(url);
  return new ProviderRedirect();
}

/**
 * Starts signing in through the provider.
 *
 * @param returnTo - The page to open afterwards, as a path; the server keeps it and checks it.
 * @returns The provider's sign-in page.
 * @throws {ApiError} for a refusal, such as `provider_unreachable`.
 */
export async function startProviderSignIn(returnTo: string): Promise<string> {
  return startFlow("/api/v1/sign-in/oidc/start", { return_to: returnTo });
}

/**
 * Starts accepting an invite or using a recovery link through the provider. The link's secret
 * goes in the body only, as `/links/redeem` sends it; the server uses the link at the finish.
 *
 * @param linkId - The link's public id (`l=`).
 * @param secret - The link's secret (`s=`).
 * @param purpose - `invite` or `recovery`, as the link's kind.
 * @returns The provider's sign-in page.
 * @throws {ApiError} 410 with `state` for a link that cannot be used.
 */
export async function startWithLink(
  linkId: string,
  secret: string,
  purpose: "invite" | "recovery",
): Promise<string> {
  return startFlow("/api/v1/sign-in/oidc/start", { purpose, link_id: linkId, secret });
}
