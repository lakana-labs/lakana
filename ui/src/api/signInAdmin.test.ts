import { describe, expect, it, vi } from "vitest";

import { send, setStepUp } from "./client";
import { issueInvite, issueRecoveryLink, saveSignInSettings } from "./signInAdmin";

import { fakeServer, type FakeRoute } from "../test/server";

const SETTINGS = {
  idle_timeout_minutes: 60,
  absolute_lifetime_minutes: 1440,
  bounds: {
    idle_timeout_minutes_min: 15,
    idle_timeout_minutes_max: 1440,
    absolute_lifetime_minutes_min: 60,
    absolute_lifetime_minutes_max: 43200,
  },
};
const INVITE = {
  id: "i-1",
  name: "kim",
  display_name: null,
  url: "http://localhost:8000/sign-in#l=a&s=b",
  expires_at: "2026-11-15T10:00:00Z",
};
const LINK = {
  id: "r-1",
  name: "kim lee",
  url: "http://localhost:8000/sign-in#l=a&s=b",
  expires_at: "2026-11-15T10:00:00Z",
};

/**
 * A session whose passkey touch ended an hour ago, or lasts ten more minutes.
 *
 * @param fresh - Whether the touch is fresh.
 * @returns The route's answer.
 */
function session(fresh: boolean): FakeRoute {
  return {
    status: 200,
    body: { step_up_until: new Date(Date.now() + (fresh ? 10 : -60) * 60_000).toISOString() },
  };
}

/**
 * A ceremony that sends the step-up requests, as the real one does.
 *
 * @returns The ceremony.
 */
function ceremony(): () => Promise<void> {
  return vi.fn(async () => {
    await send("POST", "/api/v1/sign-in/step-up/options", {});
    await send("POST", "/api/v1/sign-in/step-up", {});
  });
}

const ROUTES: Record<string, FakeRoute> = {
  "POST /api/v1/sign-in/step-up/options": { status: 200, body: {} },
  "POST /api/v1/sign-in/step-up": { status: 204 },
  "PATCH /api/v1/settings/sign-in": { status: 200, body: SETTINGS },
  "POST /api/v1/invites": { status: 201, body: INVITE },
  "POST /api/v1/principals/kim lee/recovery-link": { status: 201, body: LINK },
};

const WRITES: readonly [string, () => Promise<unknown>, string][] = [
  [
    "saving the session lengths",
    () => saveSignInSettings({ idle_timeout_minutes: 60, absolute_lifetime_minutes: 1440 }),
    "PATCH /api/v1/settings/sign-in",
  ],
  ["issuing an invite", () => issueInvite("kim", " "), "POST /api/v1/invites"],
  [
    "issuing a recovery link",
    () => issueRecoveryLink("kim lee"),
    "POST /api/v1/principals/kim%20lee/recovery-link",
  ],
];

describe("signInAdmin", () => {
  it.each(WRITES)(
    "asks for the touch before each guarded write: %s",
    async (_label, write, sent) => {
      const server = fakeServer({ "GET /api/v1/sign-in/session": session(false), ...ROUTES });
      setStepUp(ceremony());

      await write();

      expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
        "GET /api/v1/sign-in/session",
        "POST /api/v1/sign-in/step-up/options",
        "POST /api/v1/sign-in/step-up",
        sent,
      ]);
    },
  );

  it.each(WRITES)("sends at once while the touch is fresh: %s", async (_label, write, sent) => {
    const server = fakeServer({ "GET /api/v1/sign-in/session": session(true), ...ROUTES });
    const touch = ceremony();
    setStepUp(touch);

    await write();

    expect(touch).not.toHaveBeenCalled();
    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/sign-in/session",
      sent,
    ]);
  });

  it.each(WRITES)("a cancelled touch sends nothing: %s", async (_label, write) => {
    const server = fakeServer({ "GET /api/v1/sign-in/session": session(false), ...ROUTES });
    const refused = new DOMException("The touch was cancelled.", "NotAllowedError");
    setStepUp(() => Promise.reject(refused));

    await expect(write()).rejects.toBe(refused);
    expect(server.sent.map((request) => request.method)).toEqual(["GET"]);
  });

  it("sends the lengths and answers the settings as saved", async () => {
    const server = fakeServer({ "GET /api/v1/sign-in/session": session(true), ...ROUTES });

    expect(
      await saveSignInSettings({ idle_timeout_minutes: 60, absolute_lifetime_minutes: 1440 }),
    ).toEqual(SETTINGS);
    expect(server.sent.at(-1)?.body).toEqual({
      idle_timeout_minutes: 60,
      absolute_lifetime_minutes: 1440,
    });
  });

  it("sends an invite's display name, or null when it is blank", async () => {
    const server = fakeServer({ "GET /api/v1/sign-in/session": session(true), ...ROUTES });

    expect(await issueInvite("kim", "  Kim Lee ")).toEqual(INVITE);
    await issueInvite("kim", "  ");

    const posts = server.sent.filter((request) => request.method === "POST");
    expect(posts.map((request) => request.body)).toEqual([
      { name: "kim", display_name: "Kim Lee" },
      { name: "kim", display_name: null },
    ]);
  });

  it("encodes the person's name", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": session(true),
      ...ROUTES,
      "POST /api/v1/principals/a/b/recovery-link": { status: 201, body: LINK },
    });

    expect(await issueRecoveryLink("kim lee")).toEqual(LINK);
    await issueRecoveryLink("a/b");

    expect(
      server.sent.filter((request) => request.method === "POST").map((request) => request.path),
    ).toEqual([
      "/api/v1/principals/kim%20lee/recovery-link",
      "/api/v1/principals/a%2Fb/recovery-link",
    ]);
  });
});
