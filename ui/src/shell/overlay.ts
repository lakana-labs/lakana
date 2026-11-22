/**
 * Whether a modal dialog, a menu or a popover is open, for the keys that must not reach the page
 * behind one (Alt+Left in the shell, New query in Queries).
 *
 * @packageDocumentation
 */

/**
 * Whether an element is shown: not inside something hidden, inert or hidden from assistive
 * technology, nor left out of the layout.
 *
 * @param element - The element.
 * @returns True when it is shown.
 */
function shown(element: Element): boolean {
  if (element.closest('[hidden], [inert], [aria-hidden="true"]') !== null) {
    return false;
  }
  // Where the browser can say (not every test environment can), `display: none` and the like too.
  return typeof element.checkVisibility === "function" ? element.checkVisibility() : true;
}

/**
 * Whether a modal dialog, a menu or a popover is open and shown: React Aria marks each popover
 * with the trigger that opened it (`data-trigger`), and a dialog or a menu has its role. One that
 * is hidden or inert (a dialog kept in the page while closed) does not count.
 *
 * @returns True when one is.
 */
export function overlayOpen(): boolean {
  return [
    ...document.querySelectorAll(
      '[data-trigger], [role="dialog"], [role="alertdialog"], [role="menu"]',
    ),
  ].some(shown);
}
