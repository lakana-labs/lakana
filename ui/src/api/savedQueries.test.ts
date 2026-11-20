import { describe, expect, it } from "vitest";

import {
  createSavedQuery,
  deleteSavedQuery,
  getSavedQuery,
  listSavedQueries,
  updateSavedQuery,
  type SavedQueryIn,
  type SavedQueryVisibility,
} from "./savedQueries";

import { aListedSavedQuery, aSavedQuery, queryRoutes } from "../test/queries";
import { fakeServer } from "../test/server";

const SAVED = aSavedQuery();
const SHARED: SavedQueryVisibility = "everyone";
const BODY: SavedQueryIn = { name: "Daily sales", sql: "select 1", visibility: SHARED };

describe("saved queries api", () => {
  it("lists across environments with the filter and cursor", async () => {
    const server = fakeServer(queryRoutes());

    expect(await listSavedQueries(null, null)).toEqual({
      items: [aListedSavedQuery()],
      nextCursor: null,
    });
    await listSavedQueries("pro d", "c1");

    expect(server.sent[0]?.path).toBe("/api/v1/saved-queries?limit=50");
    expect(server.sent[1]?.path).toBe("/api/v1/saved-queries?limit=50&environment=pro+d&cursor=c1");
  });

  it("reads one saved query under its environment", async () => {
    const server = fakeServer(queryRoutes());

    expect(await getSavedQuery("prod", SAVED.id)).toEqual(SAVED);
    expect(server.sent[0]?.path).toBe(`/api/v1/environments/prod/saved-queries/${SAVED.id}`);
  });

  it("creates, updates and deletes under the environment", async () => {
    const base = "/api/v1/environments/a b/saved-queries";
    const server = fakeServer({
      [`POST ${base}`]: { status: 201, body: SAVED },
      [`PUT ${base}/${SAVED.id}`]: { status: 200, body: SAVED },
      [`DELETE ${base}/${SAVED.id}`]: { status: 204 },
    });

    await createSavedQuery("a b", BODY);
    await updateSavedQuery("a b", SAVED.id, BODY);
    await deleteSavedQuery("a b", SAVED.id);

    expect(server.sent.map((each) => [each.method, each.path, each.body])).toEqual([
      ["POST", "/api/v1/environments/a%20b/saved-queries", BODY],
      ["PUT", `/api/v1/environments/a%20b/saved-queries/${SAVED.id}`, BODY],
      ["DELETE", `/api/v1/environments/a%20b/saved-queries/${SAVED.id}`, undefined],
    ]);
  });

  it("reads a saved query without sql as one without the field", async () => {
    const hidden: Record<string, unknown> = { ...SAVED };
    delete hidden["sql"];
    fakeServer({
      [`GET /api/v1/environments/prod/saved-queries/${SAVED.id}`]: { status: 200, body: hidden },
    });

    const read = await getSavedQuery("prod", SAVED.id);

    expect("sql" in read).toBe(false);
  });
});
