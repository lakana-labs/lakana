/**
 * The writes an administrator makes to people and service accounts: a new service account, and
 * deactivating someone.
 *
 * @remarks
 * Both need a fresh passkey touch; each asks for it before the request (`guarded`). A
 * deactivation cannot be undone in this release.
 * @packageDocumentation
 */
import type { PrincipalOut } from "./access";
import { guarded } from "./stepUpFirst";

/**
 * Creates a service account, after the passkey touch. It can do nothing until it has a grant.
 *
 * @param name - Its name.
 * @returns The service account.
 * @throws {ApiError} for a refusal, such as 409 for a name taken (in any letter case) or an
 *   instance an access file manages, and 422 for a name with hidden characters.
 */
export function createServiceAccount(name: string): Promise<PrincipalOut> {
  return guarded<PrincipalOut>("POST", "/api/v1/principals", { name, type: "SERVICE_ACCOUNT" });
}

/**
 * Deactivates a person or service account, after the passkey touch: their sessions end and
 * their tokens stop working.
 *
 * @param name - The principal name.
 * @throws {ApiError} for a refusal, such as 409 when it would leave no active administrator.
 */
export async function deactivatePrincipal(name: string): Promise<void> {
  await guarded("DELETE", `/api/v1/principals/${encodeURIComponent(name)}`);
}
