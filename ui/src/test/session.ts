/**
 * A session value for tests that render part of the interface without the session provider.
 *
 * @packageDocumentation
 */
import { vi } from "vitest";

import type { SessionValue } from "../sign-in/SessionProvider";
import { ADMIN_SESSION } from "./server";

/**
 * A signed-in administrator's session value, with spies for its actions.
 *
 * @param overrides - Fields to change.
 * @returns The value for `SessionContext`.
 */
export function signedIn(overrides: Partial<SessionValue> = {}): SessionValue {
  return {
    state: "signed-in",
    session: ADMIN_SESSION,
    passkeyOffered: false,
    refresh: vi.fn(() => Promise.resolve()),
    signOut: vi.fn(() => Promise.resolve()),
    signOutEverywhere: vi.fn(() => Promise.resolve()),
    skipPasskey: vi.fn(),
    ...overrides,
  };
}
