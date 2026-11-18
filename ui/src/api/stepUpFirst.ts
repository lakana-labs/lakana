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

/** How long before the session's passkey touch ends a guarded action asks for a new one. */
export const STEP_UP_MARGIN_MS = 60_000;

/**
 * Reads the session and asks for the passkey touch when its touch is missing or ends within
 * {@link STEP_UP_MARGIN_MS}. The read is a background one: it is not the person's activity.
 *
 * @throws {ApiError} when the session cannot be read; whatever the ceremony throws, such as a
 *   cancelled touch.
 */
async function touchIfStale(): Promise<void> {
  const session = await send<{ readonly step_up_until: string | null }>(
    "GET",
    "/api/v1/sign-in/session",
    undefined,
    { background: true },
  );
  const until = session.step_up_until === null ? 0 : Date.parse(session.step_up_until);
  if (until - Date.now() <= STEP_UP_MARGIN_MS) {
    await stepUpNow();
  }
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
 * @throws {ApiError} for a refusal; whatever the ceremony throws.
 */
export async function guarded<T>(method: string, path: string, body?: unknown): Promise<T> {
  await touchIfStale();
  return api.request<T>(method, path, body);
}
