/**
 * The identity provider's calls that only the pages loaded on demand make: finishing a flow on
 * its return page, linking one's own account from the profile, the tokens' window, and an
 * administrator's Unlink.
 *
 * @remarks
 * Its own module, loaded with those pages, so the first load carries neither these calls nor
 * `guarded`. Linking and unlinking need a fresh confirmation, asked for before the request.
 * @packageDocumentation
 */
import { api, setCsrfToken } from "./client";
import { goToProvider, PROVIDER_TRIP_KEY, type ProviderTrip } from "./oidc";
import { guarded } from "./stepUpFirst";

/** What finishing a sign-in through the provider answers. */
export interface ProviderSignedIn {
  /** The principal name now signed in. */
  readonly name: string;
  /** The page to open now: the path the start was given, checked by the server, or `/`. */
  readonly return_to: string;
}

/** What finishing a link or a provider step-up answers. */
export interface ProviderConfirmedOut {
  /** The page to open now. */
  readonly return_to: string;
  /** Until when actions that need step-up need no other confirmation, or null. */
  readonly step_up_until: string | null;
}

/**
 * Finishes a sign-in through the provider after its callback, once, and keeps the new session's
 * CSRF token.
 *
 * @returns Who is signed in and the page to open.
 * @throws {ApiError} 403 `OidcSignInRefused` with `reason` when nobody was signed in.
 */
export async function finishProviderSignIn(): Promise<ProviderSignedIn> {
  const signedIn = await api.request<ProviderSignedIn & { readonly csrf_token: string }>(
    "POST",
    "/api/v1/sign-in/oidc/finish",
  );
  setCsrfToken(signedIn.csrf_token);
  return { name: signedIn.name, return_to: signedIn.return_to };
}

/**
 * Finishes a link from the profile or a provider step-up, in the session that started it.
 *
 * @returns The page to open and until when step-up actions need no other confirmation.
 * @throws {ApiError} 403 `OidcSignInRefused` with `reason`.
 */
export async function finishInSession(): Promise<ProviderConfirmedOut> {
  return api.request<ProviderConfirmedOut>("POST", "/api/v1/sign-in/oidc/finish-in-session");
}

/**
 * Reads, and forgets, the in-session flow this tab started, for its return page.
 *
 * @returns The flow, or null when this tab remembers none.
 */
export function takeProviderTrip(): ProviderTrip | null {
  try {
    const kept = sessionStorage.getItem(PROVIDER_TRIP_KEY);
    sessionStorage.removeItem(PROVIDER_TRIP_KEY);
    const trip = kept === null ? null : (JSON.parse(kept) as Partial<ProviderTrip> | null);
    if (
      trip === null ||
      (trip.purpose !== "link" && trip.purpose !== "step_up") ||
      typeof trip.back !== "string"
    ) {
      return null;
    }
    return { purpose: trip.purpose, back: trip.back };
  } catch {
    return null;
  }
}

/**
 * Links one's own account to the provider: after the passkey touch, opens the flow and sends
 * the browser to the provider, which sends it back to `/sign-in/oidc/confirmed`.
 *
 * @param returnTo - The page to come back to, as a path.
 * @returns Never: the browser leaves the page.
 * @throws {ProviderRedirect} once the browser is on its way; {@link ApiError} for a refusal
 *   (`already_linked`, `cannot_link_operator`, `last_unlinked_admin`); whatever the ceremony
 *   throws.
 */
export async function linkToProvider(returnTo: string): Promise<never> {
  const started = await guarded<{ readonly authorization_url: string }>(
    "POST",
    "/api/v1/sign-in/oidc/link/start",
    { return_to: returnTo },
  );
  throw goToProvider(started.authorization_url, { purpose: "link", back: returnTo });
}

/**
 * Reads until when one's personal API tokens work without another sign-in at the provider.
 *
 * @returns The end of the window, or null when one's tokens have none.
 */
export async function tokenWindowEndsAt(): Promise<string | null> {
  const me = await api.request<{ readonly token_window_ends_at?: string | null }>(
    "GET",
    "/api/v1/whoami",
  );
  return me.token_window_ends_at ?? null;
}

/**
 * Unlinks a person from the provider, after the touch: their passkeys are deleted, their
 * personal API tokens revoked and their sessions ended; they come back with a recovery link.
 *
 * @param name - The person's principal name.
 * @throws {ApiError} for a refusal, such as one's own account; whatever the ceremony throws.
 */
export async function unlinkProvider(name: string): Promise<void> {
  await guarded<undefined>("DELETE", `/api/v1/principals/${encodeURIComponent(name)}/oidc-link`);
}
