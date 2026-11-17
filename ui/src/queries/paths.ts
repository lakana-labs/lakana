/**
 * The addresses of the Queries screen: a run, and a saved query.
 *
 * @packageDocumentation
 */

/**
 * The address of a run on the Queries screen.
 *
 * @param environment - The run's environment.
 * @param id - The run's id.
 * @returns The path, each part encoded.
 */
export function runPath(environment: string, id: string): string {
  return `/queries/${encodeURIComponent(environment)}/runs/${encodeURIComponent(id)}`;
}

/**
 * The address of a saved query on the Queries screen.
 *
 * @param environment - The saved query's environment.
 * @param id - The saved query's id.
 * @returns The path, each part encoded.
 */
export function savedPath(environment: string, id: string): string {
  return `/queries/${encodeURIComponent(environment)}/saved/${encodeURIComponent(id)}`;
}
