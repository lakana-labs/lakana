import { describe, expect, it } from "vitest";

import { send } from "./client";
import {
  dismissNotice,
  getSession,
  listNotices,
  myAccess,
  signOut,
  signOutEverywhere,
} from "./session";

import { ADMIN_SESSION, fakeServer, permissions } from "../test/server";

describe("session", () => {
  it("keeps one CSRF token across two loads of the session", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
      "DELETE /api/v1/sign-in/session": { status: 204 },
    });

    const first = await getSession();
    const second = await getSession();
    await signOut();

    expect(first.csrf_token).toBe("csrf-1");
    expect(second.csrf_token).toBe(first.csrf_token);
    expect(server.sent[2]?.headers["x-lakana-csrf"]).toBe("csrf-1");
  });

  it("forgets the CSRF token after signing out", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
      "DELETE /api/v1/sign-in/session": { status: 204 },
      "POST /api/v1/things": { status: 200, body: {} },
    });
    await getSession();

    await signOut();
    await send("POST", "/api/v1/things", {});

    expect(server.sent[2]?.headers).not.toHaveProperty("x-lakana-csrf");
  });

  it("signs out everywhere through the person's sessions", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": { status: 200, body: ADMIN_SESSION },
      "DELETE /api/v1/principals/a b/sessions": { status: 204 },
    });
    await getSession();

    await signOutEverywhere("a b");

    expect(server.sent[1]).toMatchObject({
      method: "DELETE",
      path: "/api/v1/principals/a%20b/sessions",
      headers: { "x-lakana-csrf": "csrf-1" },
    });
  });

  it("finds an administrator by the ADMIN-only grant right", async () => {
    const server = fakeServer({
      "GET /api/v1/principals/admin/permissions": { status: 200, body: permissions(true) },
    });

    await expect(myAccess("admin")).resolves.toEqual({ admin: true, noAccessYet: false });
    expect(server.sent).toHaveLength(1);
  });

  it("finds a person with some right on the instance", async () => {
    fakeServer({
      "GET /api/v1/principals/kim/permissions": { status: 200, body: permissions(false, true) },
    });

    await expect(myAccess("kim")).resolves.toEqual({ admin: false, noAccessYet: false });
  });

  it.each([
    [[], true],
    [[{ name: "dev" }], false],
  ])(
    "a person without instance rights and environments %j has no access yet: %s",
    async (environments, empty) => {
      fakeServer({
        "GET /api/v1/principals/kim/permissions": { status: 200, body: permissions(false) },
        "GET /api/v1/environments": { status: 200, body: environments },
      });

      await expect(myAccess("kim")).resolves.toEqual({ admin: false, noAccessYet: empty });
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
