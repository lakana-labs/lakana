/**
 * Where the signed-in person may manage secrets, for Settings' rules.
 *
 * @remarks
 * Its own module, apart from the secrets' calls: the Settings frame loads it on every visit,
 * and the secrets' calls load only with Secrets.
 * @packageDocumentation
 */
import { api } from "./client";

/**
 * Reads the environments where the person may manage secrets, from `GET /api/v1/whoami`; a
 * background read, since nobody asked for it.
 *
 * @returns Their names; none from a server that does not say.
 * @throws {ApiError} for a refusal or an unreachable server.
 */
export async function mySecretEnvironments(): Promise<readonly string[]> {
  const me = await api.request<{ readonly secret_environments?: readonly string[] }>(
    "GET",
    "/api/v1/whoami",
    undefined,
    { background: true },
  );
  return me.secret_environments ?? [];
}
