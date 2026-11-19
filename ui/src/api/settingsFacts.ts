/**
 * What Settings' rules need to know about the signed-in person beyond the first load's access.
 *
 * @remarks
 * Its own module, apart from the secrets' and policies' calls: the Settings frame loads it on
 * every visit, and those calls load only with their places.
 * @packageDocumentation
 */
import { api } from "./client";

/** What Settings' rules read from `GET /api/v1/whoami`. */
export interface SettingsFacts {
  /** The environments where the person may manage secrets. */
  readonly secretEnvironments: readonly string[];
  /** Whether the person may manage row filters and column masks. */
  readonly managePolicy: boolean;
  /** Whether the person may read at least one environment, and so the repositories. */
  readonly readsEnvironments: boolean;
  /** The actions the person may take on the instance, such as `MANAGE_REPOSITORY`. */
  readonly instanceActions: readonly string[];
}

/**
 * Reads, in one whoami read, where the person may manage secrets, whether they manage row
 * filters and masks, whether they read an environment and what they may do on the instance; a
 * background read, since nobody asked for it.
 *
 * @returns The facts; none and no from a server that does not say.
 * @throws {ApiError} for a refusal or an unreachable server.
 */
export async function mySettingsFacts(): Promise<SettingsFacts> {
  const me = await api.request<{
    readonly secret_environments?: readonly string[];
    readonly manage_policy?: boolean;
    readonly reads_environments?: boolean;
    readonly instance_actions?: readonly string[];
  }>("GET", "/api/v1/whoami", undefined, { background: true });
  return {
    secretEnvironments: me.secret_environments ?? [],
    managePolicy: me.manage_policy ?? false,
    readsEnvironments: me.reads_environments ?? false,
    instanceActions: me.instance_actions ?? [],
  };
}
