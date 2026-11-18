/**
 * A clock for the times the Connections screen words as "2 hours ago".
 *
 * @packageDocumentation
 */
import { useEffect, useState } from "react";

/** How often the clock ticks, so "2 hours ago" ages without a reload. */
const TICK_MS = 30_000;

/**
 * The present, in milliseconds since the epoch, read again every 30 seconds.
 *
 * @returns The present as of the latest tick.
 */
export function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => {
      setNow(Date.now());
    }, TICK_MS);
    return () => {
      clearInterval(timer);
    };
  }, []);
  return now;
}
