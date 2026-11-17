import i18next from "i18next";
import { describe, expect, it } from "vitest";

import type { ConnectionLastTestOut } from "../api";
import { aConnection } from "../test/connections";
import { testStatus } from "./testStatus";

const NOW = Date.parse("2026-11-17T12:00:00Z");
const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/**
 * A finished test of `crm`, `ago` before the fixed present.
 *
 * @param over - The fields of the test that differ.
 * @param ago - How long before the present it finished, in milliseconds.
 * @param updated - How long before the present the connection was last changed.
 * @returns The status.
 */
function status(over: Partial<ConnectionLastTestOut>, ago = 2 * HOUR, updated = 10 * DAY) {
  const finished = new Date(NOW - ago).toISOString();
  return testStatus(
    aConnection({
      updated_at: new Date(NOW - updated).toISOString(),
      last_test: { state: "done", outcome: "ok", finished_at: finished, ...over },
    }),
    NOW,
    "en",
    i18next.t,
  );
}

describe("testStatus", () => {
  it("never tested has no glyph", () => {
    expect(testStatus(aConnection({ last_test: null }), NOW, "en", i18next.t)).toEqual({
      kind: "never",
      glyph: null,
      word: "never tested",
      lead: "never tested",
      trail: "",
      when: null,
    });
  });

  it.each([
    ["queued", "queued", "queued"],
    ["running", "running", "testing…"],
  ] as const)("queued and running: %s", (state, glyph, word) => {
    expect(
      testStatus(
        aConnection({ last_test: { state, outcome: null, finished_at: null } }),
        NOW,
        "en",
        i18next.t,
      ),
    ).toEqual({ kind: state, glyph, word, lead: word, trail: "", when: null });
  });

  it("queued for two minutes says no worker yet", () => {
    const queued = aConnection({
      last_test: { state: "queued", outcome: null, finished_at: null },
    });

    expect(testStatus(queued, NOW, "en", i18next.t, NOW - 119_000).word).toBe("queued");
    expect(testStatus(queued, NOW, "en", i18next.t, NOW - 120_000).word).toBe(
      "queued · no worker yet",
    );
    expect(testStatus(queued, NOW, "en", i18next.t, NOW - 120_000).glyph).toBe("queued");
  });

  it("ok says OK and when", () => {
    expect(status({})).toEqual({
      kind: "ok",
      glyph: "succeeded",
      word: "OK · 2 hours ago",
      lead: "OK · ",
      trail: "",
      when: "2 hours ago",
    });
  });

  it("credentials says its word", () => {
    expect(status({ outcome: "credentials" }, DAY)).toEqual({
      kind: "failed",
      glyph: "failed",
      word: "credentials · 1 day ago",
      lead: "credentials · ",
      trail: "",
      when: "1 day ago",
    });
  });

  it("stale has no glyph", () => {
    expect(status({}, 3 * DAY, 2 * DAY)).toEqual({
      kind: "stale",
      glyph: null,
      word: "tested before the last change · 3 days ago",
      lead: "tested before the last change · ",
      trail: "",
      when: "3 days ago",
    });
    expect(status({ outcome: "credentials" }, 3 * DAY, 2 * DAY).glyph).toBeNull();
  });

  it("an unknown outcome is failed", () => {
    const newer = { outcome: "quota" } as unknown as Partial<ConnectionLastTestOut>;

    expect(status(newer)).toMatchObject({
      kind: "failed",
      glyph: "failed",
      word: "failed · 2 hours ago",
    });
    expect(status({ outcome: null, finished_at: null })).toMatchObject({
      kind: "failed",
      word: expect.stringMatching(/^failed · /) as string,
    });
  });

  it("a minute ago or less is just now", () => {
    expect(status({}, 30_000)).toMatchObject({ word: "OK · just now", when: "just now" });
    expect(status({}, 0).when).toBe("just now");
  });

  it("splits the line around the time wherever a translation puts it", () => {
    i18next.addResource("en", "translation", "connections.status.ok", "{{when}}: fine");
    try {
      expect(status({})).toMatchObject({ word: "2 hours ago: fine", lead: "", trail: ": fine" });
    } finally {
      i18next.addResource("en", "translation", "connections.status.ok", "OK · {{when}}");
    }
  });
});
