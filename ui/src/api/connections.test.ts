import { describe, expect, it, vi } from "vitest";

import {
  createConnection,
  deleteConnection,
  getConnection,
  listConnectionEnvironments,
  listConnections,
  requestConnectionTest,
  testAllConnections,
  updateConnection,
} from "./connections";

import { ENVIRONMENTS_ROUTE, aConnection, connectionRoutes } from "../test/connections";
import { ProviderRedirect } from "./oidc";

import { fakeServer, withoutSessionReads } from "../test/server";

const ENVIRONMENTS = [{ name: "prod", actions: ["create", "test"] as const }];

describe("connections api", () => {
  it("lists across environments, adding environment and cursor only when set", async () => {
    const server = fakeServer(connectionRoutes([aConnection()], ENVIRONMENTS));

    const page = await listConnections(null, null);
    await listConnections("prod", "abc", 20, { background: true });

    expect(page).toEqual({ items: [aConnection()], nextCursor: null });
    expect(server.sent.map((request) => [request.method, request.path])).toEqual([
      ["GET", "/api/v1/connections?limit=100"],
      ["GET", "/api/v1/connections?limit=20&environment=prod&cursor=abc"],
    ]);
    expect(await listConnectionEnvironments()).toEqual(ENVIRONMENTS);
    expect(server.sent.at(-1)?.path).toBe("/api/v1/connections/environments");
    expect(ENVIRONMENTS_ROUTE).toContain("environments");
  });

  it("encodes the environment and the name", async () => {
    const server = fakeServer({
      "GET /api/v1/environments/a b/connections/crm/db": { status: 200, body: aConnection() },
      "DELETE /api/v1/environments/a b/connections/crm/db": { status: 204 },
    });

    await getConnection("a b", "crm/db", { background: true });
    await deleteConnection("a b", "crm/db");

    expect(server.sent.map((request) => [request.method, request.path])).toEqual([
      ["GET", "/api/v1/environments/a%20b/connections/crm%2Fdb"],
      ["DELETE", "/api/v1/environments/a%20b/connections/crm%2Fdb"],
    ]);
  });

  it("creates and updates under the environment, sending the body as given", async () => {
    const server = fakeServer({
      "POST /api/v1/environments/prod/connections": { status: 201, body: aConnection() },
      "PATCH /api/v1/environments/prod/connections/crm": { status: 200, body: aConnection() },
    });
    const create = {
      name: "crm",
      kind: "postgresql" as const,
      address: { host: "h" },
      settings: {},
      secret_values: { password: "s3cret" },
    };
    const update = { settings: { sslmode: null }, clear: ["secret"] };

    await createConnection("prod", create);
    await updateConnection("prod", "crm", update);

    expect(
      withoutSessionReads(server).map((request) => [request.method, request.path, request.body]),
    ).toEqual([
      ["POST", "/api/v1/environments/prod/connections", create],
      ["PATCH", "/api/v1/environments/prod/connections/crm", update],
    ]);
  });

  it("reads a reader's last test without the detail fields", async () => {
    fakeServer({
      "GET /api/v1/environments/prod/connections/crm": {
        status: 200,
        body: aConnection({
          actions: [],
          last_test: { state: "done", outcome: "ok", finished_at: "2026-11-02T09:30:02Z" },
        }),
      },
    });

    const row = await getConnection("prod", "crm");

    expect(row.last_test).not.toBeNull();
    expect("message" in (row.last_test ?? {})).toBe(false);
  });

  it("test all follows next_cursor until null and returns every item", async () => {
    const item = (name: string) => ({ connection: name, test_id: `id-${name}`, queued: true });
    const server = fakeServer({
      "POST /api/v1/environments/prod/connection-tests": (request) =>
        request.path.includes("cursor=c2")
          ? { status: 202, body: { items: [item("c")], next_cursor: null } }
          : request.path.includes("cursor=c1")
            ? { status: 202, body: { items: [item("b")], next_cursor: "c2" } }
            : { status: 202, body: { items: [item("a")], next_cursor: "c1" } },
    });

    const all = await testAllConnections("prod");

    expect(all.map((each) => each.connection)).toEqual(["a", "b", "c"]);
    expect(server.sent.map((request) => request.method)).toEqual(["POST", "POST", "POST"]);
  });

  it("a test posts to the connection's tests", async () => {
    const body = {
      id: "t1",
      state: "queued",
      outcome: null,
      message: null,
      created_at: "2026-11-02T09:30:00Z",
      finished_at: null,
    };
    const server = fakeServer({
      "POST /api/v1/environments/prod/connections/crm/tests": { status: 202, body },
    });

    expect(await requestConnectionTest("prod", "crm")).toEqual(body);
    expect(server.sent[0]?.path).toBe("/api/v1/environments/prod/connections/crm/tests");
  });

  it("a linked person's connection create with a stale touch goes through the provider and sends nothing", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": {
        status: 200,
        body: { step_up_until: null, provider_linked: true },
      },
      "POST /api/v1/sign-in/oidc/step-up/start": {
        status: 200,
        body: { authorization_url: "https://id.example.org/authorize" },
      },
      "POST /api/v1/environments/prod/connections": { status: 201, body: aConnection() },
    });
    const assign = vi.fn();
    vi.stubGlobal("location", {
      assign,
      pathname: "/connections/new",
      search: "?environment=prod",
    });

    await expect(
      createConnection("prod", {
        name: "crm",
        kind: "postgresql",
        address: { host: "h" },
        settings: {},
        secret_values: { password: "s3cret" },
      }),
    ).rejects.toBeInstanceOf(ProviderRedirect);

    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/sign-in/oidc/step-up/start",
    ]);
    expect(assign).toHaveBeenCalledWith("https://id.example.org/authorize");
    sessionStorage.clear();
  });
});
