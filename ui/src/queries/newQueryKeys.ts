/**
 * The keys of New query on the Queries screen: Alt Shift N (⌥⇧N on a Mac).
 *
 * @packageDocumentation
 */

/** The keys as `aria-keyshortcuts` names them. */
export const NEW_QUERY_KEYS = "Alt+Shift+N";

/**
 * Whether a key press is New query's: Alt and Shift with N, and neither Ctrl nor ⌘. Ctrl is left
 * out so AltGr (Ctrl Alt on Windows), which types letters such as ń, never starts a new query.
 * The browsers and the SQL editor bind nothing to these keys; on a Mac, where Option turns N into
 * "˜", the key's place (`KeyN`) says it is N.
 *
 * @param event - The key press.
 * @returns True for New query's keys.
 */
export function isNewQueryKey(event: KeyboardEvent): boolean {
  if (!event.altKey || !event.shiftKey || event.ctrlKey || event.metaKey) {
    return false;
  }
  const key = event.key.toLowerCase();
  return key === "n" || (!/^[a-z]$/u.test(key) && event.code === "KeyN");
}
