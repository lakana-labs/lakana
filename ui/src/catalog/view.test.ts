import { describe, expect, it } from "vitest";

import {
  DEFAULT_VIEW,
  defaultOrder,
  placed,
  Q_MAX,
  readView,
  sortedBy,
  viewQuery,
  viewSearch,
  type CatalogView,
} from "./view";

function read(search: string): CatalogView {
  return readView(new URLSearchParams(search));
}

describe("catalog view", () => {
  it("reads an empty address as name ascending in all tables", () => {
    expect(read("")).toEqual(DEFAULT_VIEW);
    expect(DEFAULT_VIEW).toEqual({
      environment: null,
      namespace: null,
      q: "",
      sort: "name",
      order: "asc",
    });
  });

  it("falls back to the defaults for an unknown sort or order", () => {
    expect(read("sort=bogus&order=up")).toMatchObject({ sort: "name", order: "asc" });
    expect(read("sort=rows")).toMatchObject({ sort: "rows", order: "desc" });
    expect(read("sort=rows&order=up")).toMatchObject({ sort: "rows", order: "desc" });
    expect(read("sort=rows&order=asc")).toMatchObject({ sort: "rows", order: "asc" });
  });

  it("ignores a namespace without an environment", () => {
    expect(read("ns=gold").namespace).toBeNull();
    expect(read("env=prod&ns=gold")).toMatchObject({ environment: "prod", namespace: "gold" });
  });

  it("trims the search, drops NUL characters and cuts it at 200", () => {
    expect(read(`q=${encodeURIComponent(" a\u0000b ")}`).q).toBe("ab");
    expect(read(`q=${"x".repeat(300)}`).q).toHaveLength(Q_MAX);
    expect(Q_MAX).toBe(200);
  });

  it("writes only what differs from the defaults", () => {
    expect(viewSearch({ ...DEFAULT_VIEW, sort: "rows", order: "desc" })).toBe("?sort=rows");
    expect(viewSearch({ ...DEFAULT_VIEW, sort: "rows", order: "asc" })).toBe(
      "?sort=rows&order=asc",
    );
    expect(viewSearch({ ...DEFAULT_VIEW, order: "desc" })).toBe("?order=desc");
    expect(viewSearch(DEFAULT_VIEW)).toBe("");
    expect(
      viewSearch({
        environment: "prod",
        namespace: "gold.sales",
        q: "store",
        sort: "size",
        order: "asc",
      }),
    ).toBe("?env=prod&ns=gold.sales&q=store&sort=size&order=asc");
  });

  it("round-trips every view through the address", () => {
    const views: CatalogView[] = [
      DEFAULT_VIEW,
      { environment: "prod", namespace: null, q: "", sort: "name", order: "desc" },
      {
        environment: "prod",
        namespace: "gold.sales",
        q: "a b&c=d",
        sort: "last_read",
        order: "asc",
      },
      { environment: "dev", namespace: "gold", q: "ü %", sort: "health", order: "desc" },
    ];
    for (const view of views) {
      expect(read(viewSearch(view))).toEqual(view);
    }
  });

  it("gives each sort key its first direction", () => {
    expect(defaultOrder("name")).toBe("asc");
    expect(defaultOrder("owner")).toBe("asc");
    expect(defaultOrder("rows")).toBe("desc");
    expect(defaultOrder("last_write")).toBe("desc");
  });

  it("flips the order on the sorted column and applies the default on another", () => {
    expect(sortedBy(DEFAULT_VIEW, "name")).toMatchObject({ sort: "name", order: "desc" });
    expect(sortedBy(sortedBy(DEFAULT_VIEW, "name"), "name")).toMatchObject({ order: "asc" });
    expect(sortedBy(DEFAULT_VIEW, "rows")).toMatchObject({ sort: "rows", order: "desc" });
  });

  it("keeps the search and the sort when the place changes", () => {
    const view: CatalogView = {
      environment: "dev",
      namespace: "bronze",
      q: "x",
      sort: "rows",
      order: "asc",
    };

    expect(placed(view, "prod", "gold")).toEqual({
      environment: "prod",
      namespace: "gold",
      q: "x",
      sort: "rows",
      order: "asc",
    });
    expect(placed(view, null, "gold")).toMatchObject({ environment: null, namespace: null });
  });

  it("turns a view and a cursor into the query", () => {
    expect(viewQuery({ ...DEFAULT_VIEW, environment: "prod" }, "c1")).toEqual({
      environment: "prod",
      namespace: null,
      q: "",
      sort: "name",
      order: "asc",
      cursor: "c1",
    });
  });
});
