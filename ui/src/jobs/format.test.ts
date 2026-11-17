import i18next from "i18next";
import { describe, expect, it } from "vitest";

import { formatDuration } from "./format";

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
