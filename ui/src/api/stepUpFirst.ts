/**
 * Actions the server guards with a passkey touch, asked for before the request.
 *
 * @remarks
 * Its own module, loaded only with the places that use it, so the first load does not carry
 * it. The 403 `StepUpRequired` retry of {@link api}.request stays as the fallback, for a touch
 * that ends between the check and the request.
 * @packageDocumentation
 */
import { api, send, stepUpNow } from "./client";
import { goToProvider, startFlow } from "./oidc";

/** How long before the session's passkey touch ends a guarded action asks for a new one. */
export const STEP_UP_MARGIN_MS = 60_000;

/**
 * Starts confirming the session at the identity provider (provider step-up), for a person who
 * signs in there: the provider sends the browser back to `/sign-in/oidc/confirmed`, which opens
 * `returnTo` again.
 *
 * @param returnTo - The page to come back to, as a path with its query.
 * @returns The provider's sign-in page.
 * @throws {ApiError} for a refusal.
 */
async function startProviderStepUp(returnTo: string): Promise<string> {
  return startFlow("/api/v1/sign-in/oidc/step-up/start", { return_to: returnTo });
}

/**
 * Reads the session and asks for the step-up when its touch is missing or ends within
 * {@link STEP_UP_MARGIN_MS}. The read is a background one: it is not the person's activity.
 * A person who signs in through the identity provider confirms there instead of with a passkey:
 * the browser goes to the provider and comes back to the same page, and the person presses the
 * action again. A write is never replayed after a page load, so nothing is sent.
 *
 * @throws {ApiError} when the session cannot be read; whatever the ceremony throws, such as a
 *   cancelled touch; `ProviderRedirect` (`oidc.ts`) once the browser is on its way to the provider.
 */
async function touchIfStale(): Promise<void> {
  const session = await send<{
    readonly step_up_until: string | null;
    readonly provider_linked?: boolean;
  }>("GET", "/api/v1/sign-in/session", undefined, { background: true });
  const until = session.step_up_until === null ? 0 : Date.parse(session.step_up_until);
  if (until - Date.now() > STEP_UP_MARGIN_MS) {
    return;
  }
  if (session.provider_linked === true) {
    const back = window.location.pathname + window.location.search;
    const url = await startProviderStepUp(back);
    throw goToProvider(url, { purpose: "step_up", back });
  }
  await stepUpNow();
}

/**
 * Sends a request the server guards with step-up, asking for the passkey touch first when the
 * session's is stale; a cancelled touch sends nothing.
 *
 * @template T - The JSON the route answers.
 * @param method - The HTTP method.
 * @param path - The path under the server.
 * @param body - The JSON body, if any.
 * @returns The parsed answer; undefined for 204.
 * @throws {ApiError} for a refusal; whatever the ceremony throws; `ProviderRedirect` (`oidc.ts`) when
 *   the browser goes to the identity provider instead.
 */
export async function guarded<T>(method: string, path: string, body?: unknown): Promise<T> {
  await touchIfStale();
  return api.request<T>(method, path, body);
}
