import { describe, expect, it, vi } from "vitest";

import { pinTimeZone } from "../test/timeZone";
import { resultFileName } from "./resultFileName";

const ID = "3f9c0b1c-4f50-4a61-b072-8394a5b6c7d8";
const DONE = "2026-11-16T09:00:00Z";

// The name carries the local date: 09:00 UTC is the 16th here, but the 15th at UTC−10 and further
// west, where the expected names would not hold.
pinTimeZone("Europe/Berlin");

describe("resultFileName", () => {
  it("takes the day in the person's time zone", () => {
    vi.stubEnv("TZ", "Pacific/Honolulu");
    expect(resultFileName("Sales", ID, DONE, "csv")).toBe("Sales-2026-11-15.csv");
    vi.stubEnv("TZ", "Pacific/Kiritimati");
    expect(resultFileName("Sales", ID, "2026-11-16T10:30:00Z", "csv")).toBe("Sales-2026-11-17.csv");
  });

  it.each([
    ["Monthly revenue", DONE, "csv", "Monthly-revenue-2026-11-16.csv"],
    // A no-break space is a space here, not a character to drop (#609 marks it only in names).
    ["Monthly\u00A0revenue", DONE, "csv", "Monthly-revenue-2026-11-16.csv"],
    [
      "Monthly\u202Frevenue \u2764\uFE0F",
      DONE,
      "csv",
      "Monthly-revenue-\u2764\uFE0F-2026-11-16.csv",
    ],
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
