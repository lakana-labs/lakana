/**
 * The addresses of the Connections screen: the list, Add, a connection and its Edit.
 *
 * @packageDocumentation
 */

/** The address of the Connections list. */
export const CONNECTIONS_PATH = "/connections";

/** The address of the page that adds a connection. */
export const NEW_CONNECTION_PATH = "/connections/new";

/**
 * The address of a connection's page.
 *
 * @param environment - The connection's environment.
 * @param name - The connection's name.
 * @returns The path, each part encoded.
 */
export function connectionPath(environment: string, name: string): string {
  return `/connections/${encodeURIComponent(environment)}/${encodeURIComponent(name)}`;
}

/**
 * The address of a connection's Edit page.
 *
 * @param environment - The connection's environment.
 * @param name - The connection's name.
 * @returns The path, each part encoded.
 */
export function editPath(environment: string, name: string): string {
  return `${connectionPath(environment, name)}/edit`;
}
