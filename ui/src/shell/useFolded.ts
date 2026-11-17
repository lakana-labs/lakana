/**
 * Whether the window is narrow enough that the shell folds the sidebar behind its Menu button.
 *
 * @packageDocumentation
 */
import { useEffect, useState } from "react";

/** Below this width the sidebar is folded behind its Menu button (shell.css). */
const FOLDED = "(max-width: 639.98px)";

/**
 * Whether the sidebar is folded, following the window as it changes.
 *
 * @returns True below 640 px.
 */
export function useFolded(): boolean {
  const [folded, setFolded] = useState(
    () => typeof window.matchMedia === "function" && window.matchMedia(FOLDED).matches,
  );
  useEffect(() => {
    if (typeof window.matchMedia !== "function") {
      return undefined;
    }
    const media = window.matchMedia(FOLDED);
    const onChange = (event: { readonly matches: boolean }): void => {
      setFolded(event.matches);
    };
    media.addEventListener("change", onChange);
    return () => {
      media.removeEventListener("change", onChange);
    };
  }, []);
  return folded;
}
