import { describe, expect, it } from "vitest";

import { getScheduler, getStatus } from "./status";

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

  it("reads the scheduler in the background", async () => {
    const scheduler = {
      holder: "lakana-server-1",
      heartbeat_at: "2026-11-18T09:00:00Z",
      active_runs: 2,
      pending: 1,
      ready: 0,
      running: 3,
    };
    const server = fakeServer({ "GET /api/v1/scheduler": { status: 200, body: scheduler } });

    expect(await getScheduler()).toEqual(scheduler);
    expect(server.sent[0]?.headers["x-lakana-background"]).toBe("1");
  });
});
