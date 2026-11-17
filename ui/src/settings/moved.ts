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
 * Trailing slashes are dropped; what follows `/access/people/` keeps its encoding and letter case as they were; the rest matches in any case.
 * @param pathname - The old path, beginning with `/access`.
 * @returns The new path.
 * @example
 * `movedAccessPath("/access/invites")` is `"/settings/sign-in/invites"`.
 */
export function movedAccessPath(pathname: string): string {
  // The router matches in any letter case: /Access/Invites is the old Invites page.
  const rest = pathname.replace(/^\/access/i, "").replace(/\/+$/, "");
  const lower = rest.toLowerCase();
  if (lower === "" || lower === "/people") return "/settings/access/people";
  if (SIGN_IN_PAGES.has(lower.slice(1))) return `/settings/sign-in${lower}`;
  if (lower.startsWith("/people/")) return `/settings/access/people${rest.slice("/people".length)}`;
  return `/settings/access${rest}`;
}
