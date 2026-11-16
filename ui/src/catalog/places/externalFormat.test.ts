import { describe, expect, it } from "vitest";

import { formatFromLocation } from "./externalFormat";

describe("formatFromLocation", () => {
  it.each([
    ["raw/a/*.csv", "csv"],
    ["raw/a/*.CSV", "csv"],
    ["raw/a/*.json", "json"],
    ["raw/a/*.jsonl", "json"],
    ["raw/a/*.parquet", "parquet"],
    ["raw/a/*", null],
    ["raw/a/x.csv.gz", null],
  ])("reads %s as %s", (location, format) => {
    expect(formatFromLocation(location)).toBe(format);
  });
});
