import { describe, expect, it } from "vitest";

import { resultFileName } from "./resultFileName";

const ID = "3f9c0b1c-4f50-4a61-b072-8394a5b6c7d8";
const DONE = "2026-11-16T09:00:00Z";

describe("resultFileName", () => {
  it.each([
    ["Monthly revenue", DONE, "csv", "Monthly-revenue-2026-11-16.csv"],
    [null, DONE, "csv", `query-${ID}-2026-11-16.csv`],
    ["a/b\\c", DONE, "xlsx", "a-b-c-2026-11-16.xlsx"],
    ["\u200B", DONE, "csv", `query-${ID}-2026-11-16.csv`],
    ["  ", DONE, "csv", `query-${ID}-2026-11-16.csv`],
    ["..", DONE, "csv", `query-${ID}-2026-11-16.csv`],
    ["Sales  by\tregion", DONE, "csv", "Sales-by-region-2026-11-16.csv"],
  ] as const)("names %j from %s as %s", (base, finishedAt, ext, expected) => {
    expect(resultFileName(base, ID, finishedAt, ext)).toBe(expected);
  });

  it("takes the date of now when the run has no end", () => {
    expect(resultFileName("Sales", ID, null, "csv", new Date(2026, 10, 17, 12))).toBe(
      "Sales-2026-11-17.csv",
    );
  });

  it("takes the date in local time", () => {
    const finished = new Date(2026, 10, 16, 23, 30);
    expect(resultFileName("Sales", ID, finished.toISOString(), "csv")).toBe("Sales-2026-11-16.csv");
  });
});
