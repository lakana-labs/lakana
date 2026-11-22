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
 * The browsers and the SQL editor bind nothing to these keys. On a Mac, where Option Shift turns
 * N into "˜", the key's place (`KeyN`) says it is N, but only when the key typed no letter of any
 * script: on a Mac's Dvorak layout B sits at N's place and Option Shift B types "ı". A key held
 * down, and one an input method is composing with, never count.
 *
 * @param event - The key press.
 * @returns True for New query's keys.
 */
export function isNewQueryKey(event: KeyboardEvent): boolean {
  if (event.isComposing || event.repeat) {
    return false;
  }
  if (!event.altKey || !event.shiftKey || event.ctrlKey || event.metaKey) {
    return false;
  }
  return event.key.toLowerCase() === "n" || (!/\p{L}/u.test(event.key) && event.code === "KeyN");
}
