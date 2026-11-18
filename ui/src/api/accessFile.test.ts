import { describe, expect, it } from "vitest";

import { getAccessStatus } from "./accessFile";

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
