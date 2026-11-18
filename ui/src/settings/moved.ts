/**
 * Where the old `/access/*` addresses went.
 *
 * @packageDocumentation
 */

/** Old pages that moved under Sign-in, by their last segment. */
const SIGN_IN_PAGES: ReadonlySet<string> = new Set(["invites", "recovery-links"]);

/**
 * The segments as the router reads them: each one decoded, or all as they are when one is not
 * valid percent-encoding (the router then matches the address undecoded).
 *
 * @param segments - The segments of the address.
 * @returns The decoded segments.
 */
function decoded(segments: readonly string[]): readonly string[] {
  try {
    return segments.map((segment) => decodeURIComponent(segment));
  } catch {
    return segments;
  }
}

/**
 * The Settings address that an old Access address now leads to.
 *
 * @remarks
 * Trailing slashes are dropped; what follows `/access/people/` keeps its encoding and letter case
 * as they were; the rest matches as the router matches it, in any case and with any segment
 * percent-encoded (`/access/%69nvites` is the old Invites page).
 * @param pathname - The old path, whose first segment is `access`.
 * @returns The new path.
 * @example
 * `movedAccessPath("/access/invites")` is `"/settings/sign-in/invites"`.
 */
export function movedAccessPath(pathname: string): string {
  // Drop the leading slash, then `access` itself (only the `access/*` route renders this).
  const raw = pathname.replace(/\/+$/, "").split("/").slice(2);
  const [first, ...below] = decoded(raw).map((segment) => segment.toLowerCase());
  if (first === undefined || (first === "people" && below.length === 0)) {
    return "/settings/access/people";
  }
  if (below.length === 0 && SIGN_IN_PAGES.has(first)) return `/settings/sign-in/${first}`;
  if (first === "people") return `/settings/access/people/${raw.slice(1).join("/")}`;
  return `/settings/access/${raw.join("/")}`;
}
