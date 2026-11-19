import { describe, expect, it } from "vitest";

import {
  changeStorage,
  createEnvironment,
  deleteEnvironment,
  deleteOrphans,
  findOrphans,
  getEnvironment,
  getStorage,
  getUsage,
  listDeletedEnvironments,
  listEnvironments,
  listEnvironmentsFull,
  purgeEnvironmentRows,
  restoreEnvironment,
  setEnvironmentRetention,
  setProtected,
} from "./environments";
import { mySettingsFacts } from "./settingsFacts";

import { ENVIRONMENTS } from "../test/jobs";
import { fakeServer } from "../test/server";

/** A session whose passkey touch is fresh for ten more minutes. */
const FRESH = {
  status: 200,
  body: { step_up_until: new Date(Date.now() + 10 * 60_000).toISOString() },
};

const DEV = ENVIRONMENTS[0];
const ORPHANS = { environment: "dev", older_than_days: 30, locations: [], complete: true };

const calls = (server: ReturnType<typeof fakeServer>): string[] =>
  server.sent.map((request) => `${request.method} ${request.path}`);

describe("the environments' calls", () => {
  it("asks for the touch before create, protect, storage, purge rows, a purging delete and deleting leftovers", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": FRESH,
      "POST /api/v1/environments": { status: 201, body: DEV },
      "PATCH /api/v1/environments/dev": { status: 200, body: DEV },
      "PATCH /api/v1/environments/dev/storage": { status: 200, body: DEV },
      "POST /api/v1/environments/dev/purge-rows": { status: 204 },
      "DELETE /api/v1/environments/dev": { status: 200, body: DEV },
      "POST /api/v1/environments/dev/orphaned-locations/delete": { status: 200, body: ORPHANS },
    });

    await createEnvironment({
      name: "dev",
      storage: { scheme: "file", root: "file:///srv/lakana/dev", options: {} },
      is_protected: false,
    });
    await setProtected("dev", true);
    await changeStorage("dev", { secret_ref: "corp-s3" });
    await purgeEnvironmentRows("dev");
    await deleteEnvironment("dev", true);
    await deleteOrphans("dev", 45);

    expect(calls(server)).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/environments",
      "GET /api/v1/sign-in/session",
      "PATCH /api/v1/environments/dev",
      "GET /api/v1/sign-in/session",
      "PATCH /api/v1/environments/dev/storage",
      "GET /api/v1/sign-in/session",
      "POST /api/v1/environments/dev/purge-rows",
      "GET /api/v1/sign-in/session",
      "DELETE /api/v1/environments/dev?purge=true",
      "GET /api/v1/sign-in/session",
      "POST /api/v1/environments/dev/orphaned-locations/delete",
    ]);
    expect(server.sent[3]?.body).toEqual({ is_protected: true });
    expect(server.sent[5]?.body).toEqual({ secret_ref: "corp-s3" });
    expect(server.sent[11]?.body).toEqual({ older_than_days: 45 });
  });

  it("deletes without purge, restores, sets retention and finds leftovers without a touch", async () => {
    const server = fakeServer({
      "DELETE /api/v1/environments/dev": { status: 200, body: DEV },
      "POST /api/v1/environments/dev/restore": { status: 200, body: DEV },
      "PUT /api/v1/environments/dev/retention": { status: 200, body: DEV },
      "GET /api/v1/environments/dev/orphaned-locations": { status: 200, body: ORPHANS },
    });

    await deleteEnvironment("dev", false);
    await restoreEnvironment("dev");
    await setEnvironmentRetention("dev", null);
    expect(await findOrphans("dev", 45)).toEqual(ORPHANS);

    expect(calls(server)).toEqual([
      "DELETE /api/v1/environments/dev",
      "POST /api/v1/environments/dev/restore",
      "PUT /api/v1/environments/dev/retention",
      "GET /api/v1/environments/dev/orphaned-locations?older_than_days=45",
    ]);
    expect(server.sent[2]?.body).toEqual({ days: null });
  });

  it("sends an option's value only in the body", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": FRESH,
      "POST /api/v1/environments": { status: 201, body: DEV },
      "PATCH /api/v1/environments/dev/storage": { status: 200, body: DEV },
    });

    await createEnvironment({
      name: "dev",
      storage: { scheme: "s3", root: "s3://corp-lake/dev", options: { key: "AKIA-sentinel" } },
      is_protected: true,
    });
    await changeStorage("dev", { options: { key: "AKIA-sentinel" } });

    expect(server.sent.some((request) => request.path.includes("AKIA"))).toBe(false);
    expect(server.sent[1]?.body).toEqual({
      name: "dev",
      storage: { scheme: "s3", root: "s3://corp-lake/dev", options: { key: "AKIA-sentinel" } },
      is_protected: true,
    });
    expect(server.sent[3]?.body).toEqual({ options: { key: "AKIA-sentinel" } });
  });

  it("encodes the name in the path, and reads usage in the background", async () => {
    const server = fakeServer({
      "GET /api/v1/environments/dev old": { status: 200, body: DEV },
      "GET /api/v1/environments/dev old/usage": { status: 200, body: { total_bytes: 1 } },
      "GET /api/v1/environments/dev old/storage": {
        status: 200,
        body: { root: "s3://corp-lake/dev", secret_ref: null, option_keys: ["endpoint"] },
      },
    });

    await getEnvironment("dev old");
    await getUsage("dev old");
    expect((await getStorage("dev old")).option_keys).toEqual(["endpoint"]);

    expect(calls(server)).toEqual([
      "GET /api/v1/environments/dev%20old",
      "GET /api/v1/environments/dev%20old/usage",
      "GET /api/v1/environments/dev%20old/storage",
    ]);
    expect(server.sent[1]?.headers["x-lakana-background"]).toBe("1");
  });

  it("lists the live environments whole, and the deleted ones apart", async () => {
    const server = fakeServer({
      "GET /api/v1/environments": (request) => ({
        status: 200,
        body: request.path.endsWith("?deleted=true") ? [] : ENVIRONMENTS,
      }),
    });

    expect(await listEnvironmentsFull()).toEqual(ENVIRONMENTS);
    expect(await listDeletedEnvironments()).toEqual([]);
    expect(calls(server)).toEqual([
      "GET /api/v1/environments",
      "GET /api/v1/environments?deleted=true",
    ]);
  });

  it("reads the managed environments, and none from a body without the field", async () => {
    const server = fakeServer({
      "GET /api/v1/whoami": { status: 200, body: { managed_environments: ["dev"] } },
    });
    expect((await mySettingsFacts()).managedEnvironments).toEqual(["dev"]);
    server.route({ "GET /api/v1/whoami": { status: 200, body: { name: "kim" } } });
    expect((await mySettingsFacts()).managedEnvironments).toEqual([]);
  });
});

describe("the environments call", () => {
  it("reads the environments the person may read as name and protected", async () => {
    const server = fakeServer({
      "GET /api/v1/environments": { status: 200, body: ENVIRONMENTS },
    });

    expect(await listEnvironments()).toEqual([
      { name: "dev", protected: false },
      { name: "prod", protected: true },
    ]);
    expect(server.sent[0]?.path).toBe("/api/v1/environments");
    expect(server.sent[0]?.headers["x-lakana-background"]).toBe("1");
  });
});
