/**
 * The focus for a detail page of Settings that loaded on demand.
 *
 * @packageDocumentation
 */
import { useLayoutEffect } from "react";
import { NavigationType, useNavigationType } from "react-router";

/**
 * Gives the page's heading the focus once a page that was loading has shown after a move, when
 * the focus was left on nothing (the link that was pressed is gone with the list). On the first
 * load the skip link stays the first stop.
 *
 * @returns Nothing to show.
 */
export function FocusIfLost(): null {
  const moved = useNavigationType() === NavigationType.Push;
  // A layout effect, as the shell's: the focus moves in the commit that shows the page, so
  // no key pressed in between goes to the page's body.
  useLayoutEffect(() => {
    if (moved && document.activeElement === document.body) {
      document.getElementById("main")?.querySelector("h1")?.focus();
    }
  }, [moved]);
  return null;
}
