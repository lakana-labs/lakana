import { describe, expect, it } from "vitest";

import {
  createInvite,
  createRecoveryLink,
  isAdministrator,
  listInvites,
  listPeople,
  listRecoveryLinks,
  withdrawInvite,
  withdrawRecoveryLink,
} from "./access";

import { fakeServer } from "../test/server";

const INVITE = {
  id: "i-1",
  name: "kim",
  display_name: "Kim Lee",
  url: "http://localhost:8000/sign-in#l=a&s=b",
  expires_at: "2026-11-15T10:00:00Z",
};

/**
 * The answer of the permissions route with the given vias on its one action.
 *
 * @param roles - The role of each via; null for a single action.
 * @returns The body.
 */
function permissions(roles: readonly (string | null)[]): unknown {
  return {
    principal: "kim",
    active: true,
    scope: { kind: "INSTANCE", environment: null, namespace: null, table: null },
    permissions: [
      {
        action: "MANAGE_PRINCIPAL",
        allowed: roles.length > 0,
        via: roles.map((role) => ({ kind: role === null ? "grant" : "role", role })),
        reason: null,
      },
    ],
    note: null,
  };
}

describe("access", () => {
  it("creates an invite with its display name, or without one", async () => {
    const server = fakeServer({ "POST /api/v1/invites": { status: 201, body: INVITE } });

    expect(await createInvite("kim", "Kim Lee")).toEqual(INVITE);
    await createInvite("kim", "  ");

    expect(server.sent[0]?.body).toEqual({ name: "kim", display_name: "Kim Lee" });
    expect(server.sent[1]?.body).toEqual({ name: "kim", display_name: null });
  });

  it("lists and withdraws invites", async () => {
    const server = fakeServer({
      "GET /api/v1/invites": { status: 200, body: [INVITE] },
      "DELETE /api/v1/invites/i 1": { status: 204 },
    });

    expect(await listInvites()).toEqual([INVITE]);
    await withdrawInvite("i 1");

    expect(server.sent[1]).toMatchObject({ method: "DELETE", path: "/api/v1/invites/i%201" });
  });

  it("issues, lists and withdraws recovery links", async () => {
    const server = fakeServer({
      "POST /api/v1/principals/a b/recovery-link": { status: 201, body: INVITE },
      "GET /api/v1/recovery-links": { status: 200, body: [] },
      "DELETE /api/v1/recovery-links/r 1": { status: 204 },
    });

    await createRecoveryLink("a b");
    expect(await listRecoveryLinks()).toEqual([]);
    await withdrawRecoveryLink("r 1");

    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "POST /api/v1/principals/a%20b/recovery-link",
      "GET /api/v1/recovery-links",
      "DELETE /api/v1/recovery-links/r%201",
    ]);
  });

  it("lists people, filtered by the words searched for", async () => {
    const server = fakeServer({ "GET /api/v1/principals": { status: 200, body: [] } });

    await listPeople("");
    await listPeople("a&b");

    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/principals?limit=200",
      "/api/v1/principals?limit=200&q=a%26b",
    ]);
  });

  it.each([
    [["ADMIN"], true],
    [[null, "VIEWER"], false],
    [[], false],
  ] as const)("finds out whether a person is an administrator (%j)", async (roles, admin) => {
    const server = fakeServer({
      "GET /api/v1/principals/a b/permissions": { status: 200, body: permissions(roles) },
    });

    expect(await isAdministrator("a b")).toBe(admin);
    expect(server.sent[0]?.path).toBe("/api/v1/principals/a%20b/permissions");
  });
});
