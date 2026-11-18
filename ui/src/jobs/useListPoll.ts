/**
 * A section of the Jobs list, read every 30 s while the tab is visible and at once when it is
 * asked to.
 *
 * @packageDocumentation
 */
import { useEffect, useRef } from "react";

import { type LivePoll, useLivePoll } from "../shell/useLivePoll";
import { LIST_EVERY_MS } from "./poll";

/**
 * Reads with `load` now, every 30 s while the tab is visible (never in a hidden tab, at once
 * when it shows again) and at once each time `reloadKey` changes, such as after a control acted.
 *
 * @template T - What is read.
 * @param load - Reads it as a background request; keep it stable.
 * @param reloadKey - A number that changes when the section must be read again.
 * @returns The poll.
 */
export function useListPoll<T>(load: () => Promise<T>, reloadKey: number): LivePoll<T> {
  const poll = useLivePoll(load, { everyMs: LIST_EVERY_MS, active: true });
  const { reload } = poll;
  const seen = useRef(reloadKey);
  useEffect(() => {
    if (seen.current !== reloadKey) {
      seen.current = reloadKey;
      reload();
    }
  }, [reloadKey, reload]);
  return poll;
}
