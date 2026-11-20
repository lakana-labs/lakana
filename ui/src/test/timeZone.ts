/**
 * Pins the time zone for the tests of a file.
 *
 * @packageDocumentation
 */
import { afterEach, beforeEach, vi } from "vitest";

/**
 * Runs each test of the file in a time zone, whatever the machine's, and puts the machine's back
 * after it. Node reads `TZ` again whenever it is set.
 *
 * @param zone - The IANA time zone, such as `Europe/Berlin`.
 */
export function pinTimeZone(zone: string): void {
  beforeEach(() => {
    vi.stubEnv("TZ", zone);
  });
  afterEach(() => {
    vi.unstubAllEnvs();
  });
}
