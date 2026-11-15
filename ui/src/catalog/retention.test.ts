import i18next from "i18next";
import { describe, expect, it } from "vitest";

import { retention } from "../test/manage";
import { retentionWords } from "./retention";

const t = i18next.t.bind(i18next);

describe("retentionWords", () => {
  it.each([
    [
      { days: 90, source: "environment", set_by: "bob" } as const,
      "90 days after its last activity, set for the environment by bob",
    ],
    [
      { days: 30, source: "table", set_by: "ana" } as const,
      "30 days after its last activity, set for the table by ana",
    ],
    [
      { days: 1, source: "table", set_by: "ana" } as const,
      "1 day after its last activity, set for the table by ana",
    ],
    [
      { days: 14, source: "environment", set_by: null } as const,
      "14 days after its last activity, set for the environment",
    ],
    [
      { days: 14, source: "table", set_by: null } as const,
      "14 days after its last activity, set for the table",
    ],
    [
      { days: null, on: "2026-12-01", source: "table", set_by: "ana" } as const,
      "on December 1, 2026, set for the table by ana",
    ],
    [
      { days: null, never: true, source: "table", set_by: "ana" } as const,
      "never, set for the table by ana",
    ],
    [{ source: null, days: null, expires_at: null } as const, "No retention: it never expires."],
    [
      { kept: "project_asset", source: null, days: null } as const,
      "A deployed project declares this table, so it does not expire while it is deployed.",
    ],
    [{ kept: "protected" } as const, "Tables in a protected environment never expire."],
    [
      { kept: "protected", days: 5, source: "table", set_by: "ana" } as const,
      "Tables in a protected environment never expire.",
    ],
    [
      {
        kept: "project_asset",
        days: 30,
        source: "table",
        set_by: "ana",
        expires_at: null,
      } as const,
      "A deployed project declares this table, so it does not expire while it is deployed. After that: 30 days after its last activity, set for the table by ana.",
    ],
    [
      {
        kept: "project_asset",
        days: null,
        never: true,
        source: "environment",
        set_by: null,
        expires_at: null,
      } as const,
      "A deployed project declares this table, so it does not expire while it is deployed. After that: never, set for the environment.",
    ],
  ])("says %j as %s", (overrides, sentence) => {
    expect(retentionWords(t, retention(overrides))).toBe(sentence);
  });
});

describe("retentionWords and a name", () => {
  it("marks an invisible character in who set it", () => {
    expect(retentionWords(t, retention({ set_by: "a‮b" }))).toContain("by aU+202Eb");
  });
});
