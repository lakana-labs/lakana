import { describe, expect, it } from "vitest";

import { ENVIRONMENTS } from "../test/jobs";
import { fakeServer } from "../test/server";
import { listEnvironments } from "./environments";

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
