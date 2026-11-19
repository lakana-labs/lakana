/**
 * The addresses of Settings › Repositories and of each repository's page.
 *
 * @packageDocumentation
 */

/** Settings › Repositories. */
export const REPOSITORIES_PATH = "/settings/repositories";

/**
 * The address of a repository's page.
 *
 * @remarks
 * `new` (in any letter case, as the router matches it) is Connect a repository's address, and
 * the server allows a repository of that name: its page is at `…/new/about` instead.
 * @param name - The repository's name.
 * @returns The path.
 */
export function repositoryPath(name: string): string {
  const path = `${REPOSITORIES_PATH}/${encodeURIComponent(name)}`;
  return name.toLowerCase() === "new" ? `${path}/about` : path;
}
