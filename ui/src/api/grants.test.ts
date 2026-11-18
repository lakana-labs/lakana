import { describe, expect, it } from "vitest";

import { setStepUp } from "./client";
import {
  ACTIONS,
  createGrant,
  type GrantFilter,
  grantQuery,
  listGrants,
  listHeldGrants,
  NO_FILTER,
  revokeGrant,
  ROLES,
} from "./grants";

import { fakeServer } from "../test/server";

/** A session whose passkey touch is fresh for ten more minutes. */
const FRESH = {
  status: 200,
  body: { step_up_until: new Date(Date.now() + 10 * 60_000).toISOString() },
};

describe("grants", () => {
  it.each<[string, GrantFilter, string]>([
    ["no filter", NO_FILTER, "limit=50"],
    [
      "a group, a role and a table",
      {
        who: { kind: "group", name: "analysts" },
        what: { kind: "role", role: "ANALYST" },
        where: { kind: "TABLE", environment: "dev", namespace: "sales", table: "orders" },
      },
      "group_name=analysts&role=ANALYST&environment=dev&namespace=sales&table=orders&limit=50",
    ],
    [
      "a person, an action and a namespace",
      {
        who: { kind: "principal", name: "maria" },
        what: { kind: "action", action: "READ_TABLE" },
        where: { kind: "NAMESPACE", environment: "dev", namespace: "a.b" },
      },
      "principal_name=maria&action=READ_TABLE&environment=dev&namespace=a.b&limit=50",
    ],
    [
      "an environment",
      { ...NO_FILTER, where: { kind: "ENVIRONMENT", environment: "dev" } },
      "environment=dev&limit=50",
    ],
    ["the instance", { ...NO_FILTER, where: { kind: "INSTANCE" } }, "kind=INSTANCE&limit=50"],
  ])("writes each filter as the server names it: %s", (_label, filter, query) => {
    expect(grantQuery(filter).toString()).toBe(query);
  });

  it("sends the cursor and reads the next one", async () => {
    const server = fakeServer({
      "GET /api/v1/grants": { status: 200, body: [], headers: { "X-Next-Cursor": "c3" } },
    });

    await expect(listGrants(NO_FILTER, "c2")).resolves.toEqual({ items: [], nextCursor: "c3" });
    await listGrants(NO_FILTER, null);
    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/grants?limit=50&cursor=c2",
      "/api/v1/grants?limit=50",
    ]);
  });

  it("lists each role and action once", () => {
    // The server's order and set are compared in scripts/tests/test_ui_grant_vocabulary.py.
    expect(new Set(ROLES).size).toBe(ROLES.length);
    expect(new Set(ACTIONS).size).toBe(ACTIONS.length);
    expect(ROLES).toContain("ADMIN");
    expect(ACTIONS).toContain("READ_TABLE");
  });

  it("creates and revokes with the passkey asked first", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": FRESH,
      "POST /api/v1/grants": { status: 201, body: { id: "g-1" } },
      "DELETE /api/v1/grants/g-1": { status: 204 },
    });
    setStepUp(() => Promise.resolve());
    const body = {
      group: "analysts",
      role: "ANALYST",
      kind: "ENVIRONMENT",
      environment: "dev",
    } as const;

    await createGrant(body);
    await revokeGrant("g-1");

    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/grants",
      "GET /api/v1/sign-in/session",
      "DELETE /api/v1/grants/g-1",
    ]);
    expect(server.sent[1]?.body).toEqual(body);
    expect(server.sent.some((request) => request.path.includes("override_managed"))).toBe(false);
  });

  it("lists the grants a person holds", async () => {
    const server = fakeServer({
      "GET /api/v1/principals/kim lee/grants": { status: 200, body: [] },
    });

    await expect(listHeldGrants("kim lee")).resolves.toEqual([]);
    expect(server.sent[0]?.path).toBe("/api/v1/principals/kim%20lee/grants");
  });
});
