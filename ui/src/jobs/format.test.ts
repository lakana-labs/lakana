import i18next from "i18next";
import { describe, expect, it } from "vitest";

import { formatDuration, secondsBetween } from "./format";

describe("formatDuration", () => {
  it.each([
    [1.24, "1.2 s"],
    [42, "42 s"],
    [840, "14 min"],
    [5_400, "1 h 30 min"],
  ])("writes %s seconds as %s", (seconds, text) => {
    expect(formatDuration(seconds, i18next.t.bind(i18next))).toBe(text);
  });
});

describe("formatDuration in another language", () => {
  it("writes the tenths with the language's decimal separator", () => {
    expect(formatDuration(1.24, i18next.getFixedT("de"))).toBe("1,2 s");
    expect(formatDuration(3, i18next.getFixedT("de"))).toBe("3,0 s");
  });
});

describe("secondsBetween", () => {
  it("counts the seconds between two times, or until now", () => {
    expect(secondsBetween("2026-11-17T01:00:02Z", "2026-11-17T01:00:40Z")).toBe(38);
    const started = new Date(Date.now() - 5_000).toISOString();
    expect(secondsBetween(started)).toBeGreaterThanOrEqual(5);
  });
});
