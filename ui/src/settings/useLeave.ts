/**
 * Leaving a page from one of its dialogs.
 *
 * @packageDocumentation
 */
import { useEffect, useState } from "react";
import { type NavigateOptions, useNavigate } from "react-router";

/** Where to go, and how. */
interface Leaving {
  /** The address. */
  readonly to: string;
  /** How: replace, and the state the next page reads. */
  readonly options: NavigateOptions;
}

/**
 * Closes a dialog, then goes to another address once it is gone. A modal dialog makes the rest of
 * the page inert while it is open; going in the same step would leave the next page's heading
 * inert when the shell moves the focus to it, and the focus on nothing.
 *
 * @param close - Closes the dialog.
 * @returns The way to leave.
 */
export function useLeave(close: () => void): (to: string, options: NavigateOptions) => void {
  const navigate = useNavigate();
  const [leaving, setLeaving] = useState<Leaving | null>(null);
  useEffect(() => {
    if (leaving !== null) void navigate(leaving.to, leaving.options);
  }, [leaving, navigate]);
  return (to, options) => {
    close();
    setLeaving({ to, options });
  };
}
