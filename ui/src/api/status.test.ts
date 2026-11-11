import { describe, expect, it } from "vitest";

import { getStatus } from "./status";

import { fakeServer } from "../test/server";

const STATUS = {
  state: "healthy",
  causes: [],
  workers_alive: 1,
  workers_total: 1,
  detail: null,
};

describe("status", () => {
  it("reads the instance status in the background", async () => {
    const server = fakeServer({ "GET /api/v1/status": { status: 200, body: STATUS } });

    expect(await getStatus()).toEqual(STATUS);
    expect(server.sent[0]?.headers["x-lakana-background"]).toBe("1");
  });
});
