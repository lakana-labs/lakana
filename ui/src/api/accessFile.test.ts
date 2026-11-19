import { describe, expect, it } from "vitest";

import { exportAccess, getAccessStatus, listManagedScopes } from "./accessFile";

import { fakeServer } from "../test/server";

describe("getAccessStatus", () => {
  it("reads the managed scopes", async () => {
    const scope = {
      scope: "prod",
      file_name: "access.yaml",
      file_sha256: "ab12",
      source_commit: null,
      applied_by: "admin",
      applied_at: "2026-11-18T09:00:00Z",
    };
    const server = fakeServer({
      "GET /api/v1/access/status": { status: 200, body: { scopes: [scope], overrides: [] } },
    });

    await expect(getAccessStatus()).resolves.toEqual({ scopes: [scope], overrides: [] });
    expect(server.sent[0]?.path).toBe("/api/v1/access/status");
  });
});

describe("listManagedScopes", () => {
  it("reads the managed scopes", async () => {
    const brief = { scope: "prod", file_name: "access.yaml", applied_at: "2026-11-18T09:00:00Z" };
    const server = fakeServer({
      "GET /api/v1/access/managed-scopes": { status: 200, body: [brief] },
    });

    await expect(listManagedScopes()).resolves.toEqual([brief]);
    expect(server.sent[0]?.path).toBe("/api/v1/access/managed-scopes");
  });
});

describe("exportAccess", () => {
  it("exports the scopes it is given", async () => {
    const server = fakeServer({
      "GET /api/v1/access/export": { status: 200, body: { text: "version: 1\n" } },
    });

    await expect(exportAccess(["dev", "prod"])).resolves.toBe("version: 1\n");
    expect(server.sent[0]?.path).toBe("/api/v1/access/export?scope=dev&scope=prod");
  });
});
