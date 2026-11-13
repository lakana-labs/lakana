import { describe, expect, it } from "vitest";

import { catalogPath, tablePath } from "./paths";
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
});
