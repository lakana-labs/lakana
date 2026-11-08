/**
 * The sign-in link in the address: `/sign-in#l=<public id>&s=<secret>`.
 *
 * @packageDocumentation
 */

/** The two halves of a sign-in link. */
export interface LinkFragment {
  /** The public id (`l=`); empty when the address lacked it. */
  readonly linkId: string;
  /** The secret (`s=`); empty when the address lacked it. */
  readonly secret: string;
}

/**
 * Takes the sign-in link out of the address: reads it, then at once replaces the history entry
 * with the address without its fragment, before the interface sends any request, so the secret
 * stays neither in the address bar, nor in history, nor in a bookmark.
 *
 * @param location - The browser's location.
 * @param history - The browser's history.
 * @returns The link, or null when the address holds none.
 */
export function takeLinkFragment(location: Location, history: History): LinkFragment | null {
  if (location.pathname !== "/sign-in") {
    return null;
  }
  const fields = new URLSearchParams(location.hash.slice(1));
  const linkId = fields.get("l");
  const secret = fields.get("s");
  if (linkId === null && secret === null) {
    return null;
  }
  history.replaceState(history.state, "", location.pathname + location.search);
  return { linkId: linkId ?? "", secret: secret ?? "" };
}
