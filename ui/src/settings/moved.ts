/**
 * Where the old `/access/*` addresses went.
 *
 * @packageDocumentation
 */

/** Old pages that moved under Sign-in, by their last segment. */
const SIGN_IN_PAGES: ReadonlySet<string> = new Set(["invites", "recovery-links"]);

/**
 * The Settings address that an old Access address now leads to.
 *
 * @remarks
 * Trailing slashes are dropped; what follows `/access/people/` keeps its encoding as it was.
 * @param pathname - The old path, beginning with `/access`.
 * @returns The new path.
 * @example
 * `movedAccessPath("/access/invites")` is `"/settings/sign-in/invites"`.
 */
export function movedAccessPath(pathname: string): string {
  const rest = pathname.replace(/^\/access/, "").replace(/\/+$/, "");
  if (rest === "" || rest === "/people") return "/settings/access/people";
  if (SIGN_IN_PAGES.has(rest.slice(1))) return `/settings/sign-in${rest}`;
  return `/settings/access${rest}`;
}
