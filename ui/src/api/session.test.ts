import { describe, expect, it } from "vitest";

import { ApiError, CSRF_STORAGE_KEY, send, setCsrfToken } from "./client";
import {
  dismissNotice,
  getSession,
  listNotices,
  myAccess,
  signOut,
  signOutEverywhere,
} from "./session";

import { ADMIN_SESSION, fakeServer, whoami } from "../test/server";

describe("session", () => {
  it("reads the session without receiving or changing the CSRF token", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
    });
    setCsrfToken("csrf-1");

    const session = await getSession();

    expect(session).not.toHaveProperty("csrf_token");
    expect(server.sent[0]?.headers["x-lakana-csrf"]).toBe("csrf-1");
    expect(localStorage.getItem(CSRF_STORAGE_KEY)).toBe("csrf-1");
  });

  it("forgets the CSRF token after signing out", async () => {
    const server = fakeServer({
      "DELETE /api/v1/sign-in/session": { status: 204 },
      "POST /api/v1/things": { status: 200, body: {} },
    });
    setCsrfToken("csrf-1");

    await signOut();
    await send("POST", "/api/v1/things", {});

    expect(server.sent[0]?.headers["x-lakana-csrf"]).toBe("csrf-1");
    expect(server.sent[1]?.headers).not.toHaveProperty("x-lakana-csrf");
    expect(localStorage.getItem(CSRF_STORAGE_KEY)).toBeNull();
  });

  it("forgets the CSRF token even when signing out fails", async () => {
    const server = fakeServer({
      "DELETE /api/v1/sign-in/session": { status: 500, body: { detail: "x" } },
      "POST /api/v1/things": { status: 200, body: {} },
    });
    setCsrfToken("csrf-1");

    await expect(signOut()).rejects.toBeInstanceOf(ApiError);
    await send("POST", "/api/v1/things", {});

    expect(localStorage.getItem(CSRF_STORAGE_KEY)).toBeNull();
    expect(server.sent[1]?.headers).not.toHaveProperty("x-lakana-csrf");
  });

  it("signs out everywhere through the person's sessions", async () => {
    const server = fakeServer({
      "DELETE /api/v1/principals/a b/sessions": { status: 204 },
    });
    setCsrfToken("csrf-1");

    await signOutEverywhere("a b");

    expect(localStorage.getItem(CSRF_STORAGE_KEY)).toBeNull();
    expect(server.sent[0]).toMatchObject({
      method: "DELETE",
      path: "/api/v1/principals/a%20b/sessions",
      headers: { "x-lakana-csrf": "csrf-1" },
    });
  });

  it.each([
    [true, true, true, { name: "kim", admin: true, noAccessYet: false, mayDeploy: true }],
    [false, true, true, { name: "kim", admin: false, noAccessYet: false, mayDeploy: true }],
    [false, true, false, { name: "kim", admin: false, noAccessYet: false, mayDeploy: false }],
    [false, false, false, { name: "kim", admin: false, noAccessYet: true, mayDeploy: false }],
  ])(
    "reads admin %s, access %s and deploy %s from whoami",
    async (admin, has, deploy, expected) => {
      const server = fakeServer({
        "GET /api/v1/whoami": { status: 200, body: whoami("kim", admin, has, deploy) },
      });

      await expect(myAccess()).resolves.toEqual(expected);
      expect(server.sent.map((request) => request.path)).toEqual(["/api/v1/whoami"]);
    },
  );

  it("lists notices in the background and dismisses one", async () => {
    const notice = {
      id: "n-1",
      kind: "recovery_used",
      subject: "kim",
      created_at: "2026-11-08T10:00:00Z",
    };
    const server = fakeServer({
      "GET /api/v1/sign-in/notices": { status: 200, body: [notice] },
      "DELETE /api/v1/sign-in/notices/n-1": { status: 204 },
    });

    await expect(listNotices()).resolves.toEqual([notice]);
    await dismissNotice("n-1");

    expect(server.sent[0]?.headers["x-lakana-background"]).toBe("1");
    expect(server.sent[1]).toMatchObject({ method: "DELETE", path: "/api/v1/sign-in/notices/n-1" });
  });
});
