import { describe, expect, it } from "vitest";

import { catalogPath, splitDotted, TABLE_TABS, tablePath } from "./paths";
import { DEFAULT_VIEW } from "./view";

describe("catalog paths", () => {
  it("links the list with what differs from the defaults", () => {
    expect(catalogPath(DEFAULT_VIEW)).toBe("/catalog");
    expect(catalogPath({ ...DEFAULT_VIEW, environment: "prod", namespace: "gold" })).toBe(
      "/catalog?env=prod&ns=gold",
    );
  });

  it("links a table by environment, dotted namespace and name", () => {
    expect(tablePath("prod", "gold.sales", "fact_sales")).toBe(
      "/catalog/prod/gold.sales/fact_sales",
    );
  });

  it("encodes what a path segment cannot hold", () => {
    expect(tablePath("prod", "gold", "a/b")).toBe("/catalog/prod/gold/a%2Fb");
    expect(tablePath("prod", "gold", "50%")).toBe("/catalog/prod/gold/50%25");
  });

  it("links each tab of a table", () => {
    expect(tablePath("prod", "gold.sales", "fact_sales", "columns")).toBe(
      "/catalog/prod/gold.sales/fact_sales/columns",
    );
    expect(tablePath("prod", "gold.sales", "fact_sales", "preview")).toBe(
      "/catalog/prod/gold.sales/fact_sales/preview",
    );
    expect(tablePath("prod", "gold.sales", "fact_sales", "overview")).toBe(
      "/catalog/prod/gold.sales/fact_sales",
    );
  });

  it("has Lineage, Health and Access after Preview", () => {
    expect(TABLE_TABS).toEqual(["overview", "columns", "preview", "lineage", "health", "access"]);
    expect(tablePath("prod", "gold.sales", "fact_sales", "lineage")).toBe(
      "/catalog/prod/gold.sales/fact_sales/lineage",
    );
    expect(tablePath("prod", "gold.sales", "fact_sales", "health")).toBe(
      "/catalog/prod/gold.sales/fact_sales/health",
    );
    expect(tablePath("prod", "gold.sales", "fact_sales", "access")).toBe(
      "/catalog/prod/gold.sales/fact_sales/access",
    );
  });

  it("splits a dotted reference at its last dot", () => {
    expect(splitDotted("gold.sales.fact_sales")).toEqual({
      namespace: "gold.sales",
      name: "fact_sales",
    });
    expect(splitDotted("gold.station_ranking")).toEqual({
      namespace: "gold",
      name: "station_ranking",
    });
    expect(splitDotted("x")).toBeNull();
    expect(splitDotted(".x")).toBeNull();
    expect(splitDotted("x.")).toBeNull();
  });
});
