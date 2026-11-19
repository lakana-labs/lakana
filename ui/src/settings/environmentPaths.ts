/**
 * The addresses of Settings › Environments and of each environment's page.
 *
 * @packageDocumentation
 */

/** Settings › Environments. */
export const ENVIRONMENTS_PATH = "/settings/environments";

/** The deleted environments' section of the list, which a deleted environment's address names. */
export const DELETED_PATH = `${ENVIRONMENTS_PATH}#deleted`;

/**
 * The address of an environment's page.
 *
 * @param name - The environment's name.
 * @returns The path.
 */
export function environmentPath(name: string): string {
  return `${ENVIRONMENTS_PATH}/${encodeURIComponent(name)}`;
}
