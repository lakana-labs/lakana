import { describe, expect, it } from "vitest";

import { getAttention, getGetStarted, getWork } from "./home";

import { fakeServer } from "../test/server";

const ATTENTION = {
  items: [
    {
      kind: "table_late",
      environment: "prod",
      title: "gold.station_ranking",
      ref: "gold.station_ranking",
      at: "2026-11-10T08:00:00Z",
    },
  ],
  truncated: false,
};
const WORK = { repositories: [], jobs: [] };
const GET_STARTED = {
  audience: "reader",
  local: false,
  steps: [{ id: "open_table", done: false }],
  hidden: false,
  complete: false,
};

describe("home", () => {
  it("polls what needs the person's attention in the background", async () => {
    const server = fakeServer({ "GET /api/v1/home/attention": { status: 200, body: ATTENTION } });

    expect(await getAttention()).toEqual(ATTENTION);
    expect(server.sent[0]?.headers["x-lakana-background"]).toBe("1");
  });

  it("reads the person's work and Get started list", async () => {
    const server = fakeServer({
      "GET /api/v1/home/work": { status: 200, body: WORK },
      "GET /api/v1/home/get-started": { status: 200, body: GET_STARTED },
    });

    expect(await getWork()).toEqual(WORK);
    expect(await getGetStarted()).toEqual(GET_STARTED);
    expect(server.sent.map((request) => request.headers["x-lakana-background"])).toEqual([
      undefined,
      undefined,
    ]);
  });
});
