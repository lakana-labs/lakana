import { describe, expect, it } from "vitest";

import { setStepUp } from "./client";
import {
  createOwnToken,
  listAllTokens,
  listOwnTokens,
  listTaskTokens,
  revokeTaskToken,
  revokeToken,
} from "./tokens";

import { fakeServer } from "../test/server";

/** A session whose passkey touch is fresh for ten more minutes. */
const FRESH = {
  status: 200,
  body: { step_up_until: new Date(Date.now() + 10 * 60_000).toISOString() },
};

const calls = (server: ReturnType<typeof fakeServer>): string[] =>
  server.sent.map((request) => `${request.method} ${request.path}`);

describe("tokens", () => {
  it("creates one's own token with the passkey asked first", async () => {
    const issued = { key_id: "k1", name: "laptop", token: "lak_x" };
    const server = fakeServer({
      "GET /api/v1/sign-in/session": FRESH,
      "POST /api/v1/principals/kim/tokens": { status: 201, body: issued },
    });
    setStepUp(() => Promise.resolve());

    await expect(createOwnToken("kim", "laptop")).resolves.toEqual(issued);
    expect(calls(server)).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/principals/kim/tokens",
    ]);
    expect(server.sent[1]?.body).toEqual({ name: "laptop" });
  });

  it("revokes without asking for a touch", async () => {
    const server = fakeServer({
      "DELETE /api/v1/principals/maria/tokens/k1": { status: 204 },
      "DELETE /api/v1/task-tokens/t1": { status: 204 },
    });

    await revokeToken("maria", "k1");
    await revokeTaskToken("t1");

    expect(calls(server)).toEqual([
      "DELETE /api/v1/principals/maria/tokens/k1",
      "DELETE /api/v1/task-tokens/t1",
    ]);
  });

  it("pages both lists", async () => {
    const server = fakeServer({
      "GET /api/v1/tokens": { status: 200, body: [], headers: { "X-Next-Cursor": "c2" } },
      "GET /api/v1/task-tokens": { status: 200, body: [] },
    });

    await expect(listAllTokens(null, null)).resolves.toEqual({ items: [], nextCursor: "c2" });
    await listAllTokens("maria", "c2");
    await expect(listTaskTokens(null, null)).resolves.toEqual({ items: [], nextCursor: null });
    await listTaskTokens("dev", "c3");

    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/tokens?limit=50",
      "/api/v1/tokens?owner=maria&limit=50&cursor=c2",
      "/api/v1/task-tokens?limit=50",
      "/api/v1/task-tokens?environment=dev&limit=50&cursor=c3",
    ]);
  });

  it("encodes the names in the path", async () => {
    const server = fakeServer({
      "GET /api/v1/principals/kim lee/tokens": { status: 200, body: [] },
      "DELETE /api/v1/principals/kim lee/tokens/a/b": { status: 204 },
      "DELETE /api/v1/task-tokens/a/b": { status: 204 },
    });

    await expect(listOwnTokens("kim lee")).resolves.toEqual([]);
    await revokeToken("kim lee", "a/b");
    await revokeTaskToken("a/b");

    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/principals/kim%20lee/tokens",
      "/api/v1/principals/kim%20lee/tokens/a%2Fb",
      "/api/v1/task-tokens/a%2Fb",
    ]);
  });
});
