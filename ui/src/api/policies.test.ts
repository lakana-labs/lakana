import { describe, expect, it } from "vitest";

import { setStepUp } from "./client";
import {
  createColumnMask,
  createRowFilter,
  deleteColumnMask,
  deleteRowFilter,
  listColumnMaskPage,
  listRowFilterPage,
  NO_POLICY_FILTER,
  type PolicyFilter,
  policyQuery,
} from "./policies";

import { fakeServer } from "../test/server";

/** A session whose passkey touch is fresh for ten more minutes. */
const FRESH = {
  status: 200,
  body: { step_up_until: new Date(Date.now() + 10 * 60_000).toISOString() },
};

describe("policies", () => {
  it.each<[string, PolicyFilter, string]>([
    ["no filter", NO_POLICY_FILTER, "limit=50"],
    [
      "a group and a table",
      {
        who: { kind: "group", name: "analysts" },
        environment: "dev",
        namespace: "gold",
        table: "station_ranking",
      },
      "group_name=analysts&environment=dev&namespace=gold&table=station_ranking&limit=50",
    ],
    [
      "a person and an environment",
      { ...NO_POLICY_FILTER, who: { kind: "principal", name: "maria" }, environment: "prod" },
      "principal_name=maria&environment=prod&limit=50",
    ],
  ])("writes each filter as the server names it: %s", (_label, filter, query) => {
    expect(policyQuery(filter, 50).toString()).toBe(query);
  });

  it("sends the cursor and reads the next one, for each kind", async () => {
    const server = fakeServer({
      "GET /api/v1/row-filters": { status: 200, body: [], headers: { "X-Next-Cursor": "c3" } },
      "GET /api/v1/column-masks": { status: 200, body: [] },
    });

    await expect(listRowFilterPage(NO_POLICY_FILTER, "c2")).resolves.toEqual({
      items: [],
      nextCursor: "c3",
    });
    await expect(listColumnMaskPage(NO_POLICY_FILTER, null, 200)).resolves.toEqual({
      items: [],
      nextCursor: null,
    });
    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/row-filters?limit=50&cursor=c2",
      "/api/v1/column-masks?limit=200",
    ]);
  });

  it("creates and deletes with the passkey asked first", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": FRESH,
      "POST /api/v1/row-filters": { status: 201, body: { id: "f-1" } },
      "POST /api/v1/column-masks": { status: 201, body: { id: "m-1" } },
      "DELETE /api/v1/row-filters/f-1": { status: 204 },
      "DELETE /api/v1/column-masks/m-1": { status: 204 },
    });
    setStepUp(() => Promise.resolve());
    const filter = {
      principal: null,
      group: "analysts",
      environment: "dev",
      namespace: "gold",
      table: "station_ranking",
      expression: "region = 'EU'",
    };
    const mask = { ...filter, column: "email", expression: "'***'" };

    await createRowFilter(filter);
    await createColumnMask(mask);
    await deleteRowFilter("f-1");
    await deleteColumnMask("m-1");

    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/row-filters",
      "GET /api/v1/sign-in/session",
      "POST /api/v1/column-masks",
      "GET /api/v1/sign-in/session",
      "DELETE /api/v1/row-filters/f-1",
      "GET /api/v1/sign-in/session",
      "DELETE /api/v1/column-masks/m-1",
    ]);
    expect(server.sent[1]?.body).toEqual(filter);
    expect(server.sent[3]?.body).toEqual(mask);
  });

  it("encodes the id in the path", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": FRESH,
      "DELETE /api/v1/row-filters/a/b": { status: 204 },
    });
    setStepUp(() => Promise.resolve());

    await deleteRowFilter("a/b");

    expect(server.sent[1]?.path).toBe("/api/v1/row-filters/a%2Fb");
  });
});
