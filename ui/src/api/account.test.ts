import { describe, expect, it } from "vitest";

import { endSessions, listPasskeys, listSessions, removePasskey } from "./account";

import { fakeServer } from "../test/server";

describe("account", () => {
  it("lists and removes a person's passkeys", async () => {
    const server = fakeServer({
      "GET /api/v1/principals/a b/passkeys": { status: 200, body: [] },
      "DELETE /api/v1/principals/a b/passkeys/p 1": { status: 204 },
    });

    expect(await listPasskeys("a b")).toEqual([]);
    await removePasskey("a b", "p 1");

    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/principals/a%20b/passkeys",
      "DELETE /api/v1/principals/a%20b/passkeys/p%201",
    ]);
  });

  it("lists and ends a person's sessions", async () => {
    const server = fakeServer({
      "GET /api/v1/principals/a b/sessions": { status: 200, body: [] },
      "DELETE /api/v1/principals/a b/sessions": { status: 204 },
    });

    expect(await listSessions("a b")).toEqual([]);
    await endSessions("a b");

    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/principals/a%20b/sessions",
      "DELETE /api/v1/principals/a%20b/sessions",
    ]);
  });
});
