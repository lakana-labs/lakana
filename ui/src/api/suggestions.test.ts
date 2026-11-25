import { describe, expect, it } from "vitest";

import { fakeServer } from "../test/server";
import { suggestGroups, suggestPrincipals, suggestTables } from "./suggestions";

describe("suggestions", () => {
  it("asks for the first principals, groups and tables, with the text only when there is one", async () => {
    const server = fakeServer({
      "GET /api/v1/principals": {
        status: 200,
        body: [{ name: "maria", type: "USER", is_active: true, picture: null }],
        headers: { "X-Next-Cursor": "c2" },
      },
      "GET /api/v1/groups": { status: 200, body: [{ name: "analysts" }] },
      "GET /api/v1/tables": { status: 200, body: { items: [], next_cursor: null, capped: false } },
    });
    const signal = new AbortController().signal;

    const people = await suggestPrincipals("mar", 20, signal);
    await suggestPrincipals("", 20, signal);
    const groups = await suggestGroups("ana", 20, signal);
    await suggestGroups("", 20, signal);
    await suggestTables("dev", "station", 20, signal);
    await suggestTables("dev", "", 20, signal);

    expect(people).toEqual({
      people: [{ name: "maria", type: "USER", is_active: true, picture: null }],
      more: true,
    });
    expect(groups).toEqual({ names: ["analysts"], more: false });
    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/principals?limit=20&q=mar",
      "/api/v1/principals?limit=20",
      "/api/v1/groups?limit=20&q=ana",
      "/api/v1/groups?limit=20",
      "/api/v1/tables?environment=dev&sort=name&limit=20&name=station",
      "/api/v1/tables?environment=dev&sort=name&limit=20",
    ]);
    expect(server.sent.every((request) => request.signal === signal)).toBe(true);
  });
});
