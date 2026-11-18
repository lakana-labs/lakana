/**
 * An environment's secrets: their names and uses, never their values.
 *
 * @remarks
 * Creating, rotating and binding a secret need a fresh passkey touch, asked for before the
 * request (`guarded`); deleting one removes access, so it needs none. A value is sent only in a
 * request's body, never in its address, and no answer holds one.
 * @packageDocumentation
 */
import { api } from "./client";
import { guarded } from "./stepUpFirst";

/** What a secret can be used for. */
export type SecretUseKind = "package_index" | "repository" | "connection" | "environment";

/** A secret, as the secrets routes answer it: names and uses, structurally never a value. */
export interface SecretOut {
  /** The secret's id. */
  readonly id: string;
  /** Its name, unique in its environment. */
  readonly name: string;
  /** Its environment. */
  readonly environment: string;
  /** The id of the principal who created it. */
  readonly created_by: string;
  /** When it was created. */
  readonly created_at: string;
  /** When its values were last replaced; null before the first rotation. */
  readonly rotated_at: string | null;
  /** How it is stored. */
  readonly format_version: number;
  /** Which key wrapped its data key. */
  readonly key_id: string;
  /** What it is bound to; null for no use yet. */
  readonly use: SecretUseKind | null;
  /** The index's, repository's or connection's name; null otherwise. */
  readonly use_target: string | null;
  /** Where the secret is sent; null when `use_target` is. */
  readonly use_target_url: string | null;
}

/** A use to bind a secret to. */
export interface SecretUseIn {
  /** What kind of use. */
  readonly kind: SecretUseKind;
  /** The index's, repository's or connection's name; null for the environment. */
  readonly target: string | null;
}

/** What a new secret is made of. */
export interface SecretCreateIn {
  /** Its name. */
  readonly name: string;
  /** Its values, by key. */
  readonly values: Readonly<Record<string, string>>;
  /** Its use; null for none yet. */
  readonly use: SecretUseIn | null;
}

/**
 * The API path of an environment's secrets, or of one of them.
 *
 * @param environment - The environment.
 * @param name - The secret's name, for one secret.
 * @returns The path.
 */
function secretsApiPath(environment: string, name?: string): string {
  const base = `/api/v1/environments/${encodeURIComponent(environment)}/secrets`;
  return name === undefined ? base : `${base}/${encodeURIComponent(name)}`;
}

/**
 * Lists an environment's live secrets.
 *
 * @param environment - The environment.
 * @returns Its secrets.
 * @throws {ApiError} for a refusal, such as 403 without the right to manage them.
 */
export function listSecrets(environment: string): Promise<readonly SecretOut[]> {
  return api.request<readonly SecretOut[]>("GET", secretsApiPath(environment));
}

/**
 * Creates a secret, after the passkey touch.
 *
 * @param environment - The environment.
 * @param body - Its name, values and use; a null use is left out.
 * @returns The secret.
 * @throws {ApiError} for a refusal, such as 409 for a name already taken.
 */
export function createSecret(environment: string, body: SecretCreateIn): Promise<SecretOut> {
  const { use, ...rest } = body;
  return guarded<SecretOut>(
    "POST",
    secretsApiPath(environment),
    use === null ? rest : { ...rest, use },
  );
}

/**
 * Replaces a secret's values, after the passkey touch.
 *
 * @param environment - The environment.
 * @param name - The secret's name.
 * @param values - The new values, by key.
 * @returns The secret.
 * @throws {ApiError} for a refusal, such as 404 for a secret that is gone.
 */
export function rotateSecret(
  environment: string,
  name: string,
  values: Readonly<Record<string, string>>,
): Promise<SecretOut> {
  return guarded<SecretOut>("PATCH", secretsApiPath(environment, name), { values });
}

/**
 * Binds a secret to a use, after the passkey touch.
 *
 * @param environment - The environment.
 * @param name - The secret's name.
 * @param use - The use.
 * @returns The secret.
 * @throws {ApiError} for a refusal, such as 404 for a target that does not exist.
 */
export function bindSecret(
  environment: string,
  name: string,
  use: SecretUseIn,
): Promise<SecretOut> {
  return guarded<SecretOut>("PUT", `${secretsApiPath(environment, name)}/use`, use);
}

/**
 * Deletes a secret; what used it stops working.
 *
 * @param environment - The environment.
 * @param name - The secret's name.
 * @throws {ApiError} for a refusal, such as 404 for a secret that is gone.
 */
export async function revokeSecret(environment: string, name: string): Promise<void> {
  await api.request("DELETE", secretsApiPath(environment, name));
}

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
