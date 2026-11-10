import { describe, expect, it } from "vitest";

import { endedReason, problemText } from "./messages";

import { ApiError } from "../api";
import { initI18n } from "../i18n/i18n";

const { t } = initI18n();

describe("problemText", () => {
  it.each([
    ["NotAllowedError", "The passkey request was cancelled or took too long."],
    ["NotSupportedError", "This browser cannot use passkeys."],
    [
      "InvalidStateError",
      "This browser or password manager already holds a passkey for this account. To add another",
    ],
    ["SecurityError", "Passkeys work only at the address Lakana was set up with."],
    ["AbortError", "The browser could not use the passkey (AbortError)."],
  ])("names the browser's %s", (name, start) => {
    expect(problemText(t, new DOMException("x", name))).toMatch(
      new RegExp(`^${start.replace(/[().]/g, "\\$&")}`),
    );
  });

  it("names an unreachable server", () => {
    expect(problemText(t, new ApiError(0, "NetworkError", "x"))).toMatch(
      /^Lakana could not be reached/,
    );
  });

  it("passes the server's own sentence on", () => {
    expect(problemText(t, new ApiError(429, "TooManyAttempts", "Wait a minute."))).toBe(
      "Wait a minute.",
    );
  });

  it("names anything else as the interface's own failure", () => {
    expect(problemText(t, new Error("boom"))).toMatch(/^Something went wrong in the interface/);
  });
});

describe("endedReason", () => {
  it("says why a session ended, in the interface's words", () => {
    expect(endedReason(t, new ApiError(401, "SessionEndedError", "x", { reason: "idle" }))).toBe(
      "Your session ended after a time without activity. Sign in again.",
    );
  });

  it("falls back to the server's sentence for a reason it does not know", () => {
    expect(
      endedReason(
        t,
        new ApiError(401, "SessionEndedError", "Ended for a new reason.", { reason: "new" }),
      ),
    ).toBe("Ended for a new reason.");
  });

  it("says nothing when the browser simply was not signed in", () => {
    expect(endedReason(t, new ApiError(401, "AuthenticationError", "Sign in."))).toBeUndefined();
  });

  it("names other failures", () => {
    expect(endedReason(t, new ApiError(409, "ConflictError", "The interface is off."))).toBe(
      "The interface is off.",
    );
    expect(endedReason(t, new ApiError(0, "NetworkError", "x"))).toMatch(
      /^Lakana could not be reached/,
    );
  });
});
