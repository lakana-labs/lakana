import { describe, expect, it } from "vitest";

import { createExternalTable, createNamespace, deleteNamespace } from "./namespaces";

import { manageRoutes } from "../test/manage";
import { fakeServer, withoutSessionReads } from "../test/server";

describe("namespaces api", () => {
  it("creates with the path, empty properties and the reason in the body", async () => {
    const server = fakeServer(manageRoutes());

    const created = await createNamespace("prod", "gold.finance", null);
    await createNamespace("prod", "gold.finance", "Wrong data in prod");

    expect(created).toEqual({ path: "gold.finance", properties: {} });
    expect(
      withoutSessionReads(server).map((request) => [request.method, request.path, request.body]),
    ).toEqual([
      [
        "POST",
        "/api/v1/environments/prod/namespaces",
        { path: "gold.finance", properties: {}, break_glass_reason: null },
      ],
      [
        "POST",
        "/api/v1/environments/prod/namespaces",
        { path: "gold.finance", properties: {}, break_glass_reason: "Wrong data in prod" },
      ],
    ]);
  });

  it("deletes without a query, and with break_glass_reason in the query when set", async () => {
    const server = fakeServer(manageRoutes());

    await deleteNamespace("prod", "silver", null);
    await deleteNamespace("prod", "silver", "fix it now please");

    expect(withoutSessionReads(server).map((request) => [request.method, request.path])).toEqual([
      ["DELETE", "/api/v1/environments/prod/namespaces/silver"],
      [
        "DELETE",
        "/api/v1/environments/prod/namespaces/silver?break_glass_reason=fix+it+now+please",
      ],
    ]);
  });

  it("registers an external table with snake_case fields under its namespace", async () => {
    const server = fakeServer(manageRoutes());

    const registered = await createExternalTable("prod", "bronze", {
      name: "orders",
      locationPattern: "raw/orders/*.csv",
      format: "csv",
      breakGlassReason: null,
    });
    await createExternalTable("prod", "bronze", {
      name: "orders",
      locationPattern: "raw/orders/*.csv",
      format: "csv",
      breakGlassReason: "Quarter close needs the orders",
    });

    expect(registered).toEqual({
      name: "orders",
      namespace: "bronze",
      location_pattern: "raw/orders/*.csv",
      format: "csv",
      schema: { order_id: "int64", amount: "double" },
    });
    expect(
      withoutSessionReads(server).map((request) => [request.method, request.path, request.body]),
    ).toEqual([
      [
        "POST",
        "/api/v1/environments/prod/namespaces/bronze/tables/external",
        {
          name: "orders",
          location_pattern: "raw/orders/*.csv",
          format: "csv",
          break_glass_reason: null,
        },
      ],
      [
        "POST",
        "/api/v1/environments/prod/namespaces/bronze/tables/external",
        {
          name: "orders",
          location_pattern: "raw/orders/*.csv",
          format: "csv",
          break_glass_reason: "Quarter close needs the orders",
        },
      ],
    ]);
  });

  it("encodes the environment and the path", async () => {
    const server = fakeServer({
      "POST /api/v1/environments/a b/namespaces": {
        status: 201,
        body: { path: "a b", properties: {} },
      },
      "DELETE /api/v1/environments/a b/namespaces/a b": { status: 204 },
    });

    await createNamespace("a b", "a b", null);
    await deleteNamespace("a b", "a b", null);

    expect(withoutSessionReads(server).map((request) => request.path)).toEqual([
      "/api/v1/environments/a%20b/namespaces",
      "/api/v1/environments/a%20b/namespaces/a%20b",
    ]);
  });
});
