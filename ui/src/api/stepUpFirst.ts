/**
 * Actions the server guards with a passkey touch, asked for before the request.
 *
 * @remarks
 * The 403 `StepUpRequired` retry of {@link api}.request stays as the fallback, for a touch
 * that ends between the check and the request; its ceremony is {@link confirmPresence}, which
 * sends a person linked to the identity provider there as `guarded` does.
 * @packageDocumentation
 */
import { api, ApiError, send, stepUpNow } from "./client";
import { goToProvider, type ProviderRedirect, startFlow } from "./oidc";
import { stepUp } from "./passkeys";

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
 * Sends the browser to the identity provider to confirm the session, back to this page.
 *
 * @returns The error the caller throws, so that nothing is sent.
 * @throws {ApiError} when the provider step-up cannot start.
 */
async function toProvider(): Promise<ProviderRedirect> {
  const back = window.location.pathname + window.location.search;
  return goToProvider(await startProviderStepUp(back), { purpose: "step_up", back });
}

/**
 * The step-up ceremony: a passkey touch, or for a person linked to the identity provider (the
 * server answers the passkey's options with 409 `ProviderStepUpRequired`) the trip to the
 * provider. It is what {@link api}.request runs on a 403 `StepUpRequired`, so a write sent
 * without `guarded`, or whose touch ran out after `guarded`'s check, is not sent again: the
 * person presses it again after the provider.
 *
 * @throws {ApiError} for a refusal; whatever the touch throws; `ProviderRedirect` (`oidc.ts`)
 *   once the browser is on its way to the provider.
 */
export async function confirmPresence(): Promise<void> {
  try {
    await stepUp();
  } catch (error) {
    if (error instanceof ApiError && error.type === "ProviderStepUpRequired") {
      throw await toProvider();
    }
    throw error;
  }
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
    throw await toProvider();
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
