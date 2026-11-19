/**
 * The running server's settings, read-only, as Settings › Configuration shows them.
 *
 * @remarks
 * Its own module, loaded only with Configuration. The server never sends a secret's value; the
 * page reads a secret's `has_value` alone all the same, so a server that did would still show
 * nothing of it.
 * @packageDocumentation
 */
import { api } from "./client";

/** A value in its JSON form: what a setting holds. */
type JsonValue =
  string | number | boolean | null | readonly JsonValue[] | { readonly [key: string]: JsonValue };

/** One setting of the running server, as `GET /api/v1/settings/configuration` answers it. */
export interface ConfigurationSettingOut {
  /** The environment variable that sets it, such as `LAKANA_RUN__LEASE_SECONDS`. */
  readonly name: string;
  /** The value in its JSON form; null for a secret, and never read for one. */
  readonly value: JsonValue;
  /** Whether the setting holds a credential. */
  readonly secret: boolean;
  /** Whether the setting holds a value; for a secret, set or not set. */
  readonly has_value: boolean;
  /** `set` when the environment or the `.env` file gave it, else `default`. */
  readonly source: "set" | "default";
}

/**
 * Reads the running server's settings, in the order of the settings reference.
 *
 * @returns The settings.
 * @throws {ApiError} for a refusal (403 without the ADMIN role through a personal credential).
 */
export async function getConfiguration(): Promise<readonly ConfigurationSettingOut[]> {
  return api.request<readonly ConfigurationSettingOut[]>("GET", "/api/v1/settings/configuration");
}
