import { describe, expect, it } from "vitest";

import { changeInterfaceState, getInfo, getInterfaceState, listRecent, recordOpened } from "./me";

import { fakeServer } from "../test/server";

const STATE = { tour_seen: false, get_started_hidden: false, update_note_seen: null };

describe("me", () => {
  it("reads and changes the person's interface state", async () => {
    const server = fakeServer({
      "GET /api/v1/principals/kim lee/interface-state": { status: 200, body: STATE },
      "PATCH /api/v1/principals/kim lee/interface-state": {
        status: 200,
        body: { ...STATE, get_started_hidden: true },
      },
    });

    expect(await getInterfaceState("kim lee")).toEqual(STATE);
    expect(await changeInterfaceState("kim lee", { get_started_hidden: true })).toEqual({
      ...STATE,
      get_started_hidden: true,
    });
    expect(server.sent[1]).toMatchObject({
      method: "PATCH",
      path: "/api/v1/principals/kim%20lee/interface-state",
      body: { get_started_hidden: true },
    });
  });

  it("lists what the person opened recently", async () => {
    const recent = [
      {
        kind: "table",
        environment: "dev",
        ref: "gold.station_ranking",
        opened_at: "2026-11-10T08:00:00Z",
      },
    ];
    fakeServer({ "GET /api/v1/principals/kim/recent": { status: 200, body: recent } });

    expect(await listRecent("kim")).toEqual(recent);
  });

  it("reads the version of the running server", async () => {
    fakeServer({
      "GET /api/v1/info": {
        status: 200,
        body: { version: "0.23.0", artifact_limits: {}, upload_max_bytes: 1 },
      },
    });

    expect(await getInfo()).toEqual({ version: "0.23.0" });
  });
});

describe("recordOpened", () => {
  it("tells the server that the person opened a table", async () => {
    const server = fakeServer({ "POST /api/v1/principals/kim lee/recent": { status: 204 } });

    await recordOpened("kim lee", "table", "prod", "gold.sales.fact_sales");

    expect(server.sent[0]).toMatchObject({
      method: "POST",
      path: "/api/v1/principals/kim%20lee/recent",
      body: { kind: "table", environment: "prod", ref: "gold.sales.fact_sales" },
    });
  });

  it("tells the server that the person opened a query run", async () => {
    const server = fakeServer({ "POST /api/v1/principals/kim lee/recent": { status: 204 } });

    await recordOpened("kim lee", "query", "prod", "0b1c");

    expect(server.sent[0]?.body).toEqual({ kind: "query", environment: "prod", ref: "0b1c" });
  });

  it("records an opened job and run", async () => {
    const server = fakeServer({ "POST /api/v1/principals/kim lee/recent": { status: 204 } });

    await recordOpened("kim lee", "job", "prod", "analytics/nightly_sales");
    await recordOpened("kim lee", "run", "prod", "5d6e");

    expect(server.sent.map((request) => request.body)).toEqual([
      { kind: "job", environment: "prod", ref: "analytics/nightly_sales" },
      { kind: "run", environment: "prod", ref: "5d6e" },
    ]);
  });
});
