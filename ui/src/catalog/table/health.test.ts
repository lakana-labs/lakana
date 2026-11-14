import i18next from "i18next";
import { describe, expect, it } from "vitest";

import { checkResult, health, signalResult } from "../../test/table";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { bandWords, effectWord, firedLines, judged, observedWords } from "./health";

const t = i18next.t.bind(i18next);

/**
 * The sentences of the lines a health answer fires.
 *
 * @param overrides - The checks and signals.
 * @returns The sentences, in order.
 */
function sentences(overrides: Parameters<typeof health>[0]): string[] {
  return firedLines(health(overrides), t).map((line) => line.sentence);
}

describe("firedLines", () => {
  it("has no line for a check that passed or a signal that did not fire", () => {
    expect(firedLines(health({}), t)).toEqual([]);
  });

  it("says what a failed check found and that the table was set back", () => {
    const failed = checkResult({
      outcome: "failed",
      failing_rows: 12,
      rollback_outcome: "rolled_back",
    });

    expect(firedLines(health({ checks: [failed] }), t)).toEqual([
      {
        kind: "check",
        severity: "failing",
        sentence:
          "Check unique_sale_id failed: 12 rows broke it. The table was set back to its last good snapshot.",
      },
    ]);
  });

  it("says one row in the singular, and that the table was not published", () => {
    expect(
      sentences({
        checks: [
          checkResult({ outcome: "failed", failing_rows: 1, rollback_outcome: "unpublished" }),
        ],
      }),
    ).toEqual(["Check unique_sale_id failed: 1 row broke it. The table was not published."]);
  });

  it("says a failed check without a row count", () => {
    expect(sentences({ checks: [checkResult({ outcome: "failed", failing_rows: null })] })).toEqual(
      ["Check unique_sale_id failed."],
    );
  });

  it("says nothing of the table for a rollback that changed nothing it can name", () => {
    expect(
      sentences({
        checks: [
          checkResult({ outcome: "failed", failing_rows: 2, rollback_outcome: "nothing_written" }),
        ],
      }),
    ).toEqual(["Check unique_sale_id failed: 2 rows broke it."]);
  });

  it("says an errored check could not run, with its error", () => {
    expect(
      sentences({
        checks: [checkResult({ outcome: "errored", error: "Column amount does not exist." })],
      }),
    ).toEqual(["Check unique_sale_id could not run: Column amount does not exist."]);
    expect(sentences({ checks: [checkResult({ outcome: "errored", error: null })] })).toEqual([
      "Check unique_sale_id could not run.",
    ]);
  });

  it("says a warned check, with and without a row count", () => {
    expect(sentences({ checks: [checkResult({ outcome: "warned", failing_rows: 3 })] })).toEqual([
      "Check unique_sale_id warned: 3 rows broke it.",
    ]);
    expect(sentences({ checks: [checkResult({ outcome: "warned", failing_rows: null })] })).toEqual(
      ["Check unique_sale_id warned."],
    );
  });

  it("uses a signal's alert, as the server worded it", () => {
    expect(
      sentences({
        signals: [
          signalResult({
            state: "fired",
            signal: "volume",
            alert: "Rows dropped by 40 % since Monday.",
          }),
        ],
      }),
    ).toEqual(["Rows dropped by 40 % since Monday."]);
  });

  it.each([
    ["freshness", null, "The table was not written when expected."],
    ["volume", null, "The number of rows written is outside its usual band."],
    ["null_rate", "amount", "The share of empty values in amount is outside its usual band."],
    ["range", "amount", "Values in amount are outside their usual band."],
    ["schema", null, "The table's columns changed."],
    ["drift", null, "The drift signal fired."],
  ])(
    "says a fired %s signal without an alert in a neutral sentence",
    (signal, column, sentence) => {
      expect(sentences({ signals: [signalResult({ state: "fired", signal, column })] })).toEqual([
        sentence,
      ]);
    },
  );

  it("never claims a direction when the values are hidden", () => {
    const lines = sentences({
      signals: [
        signalResult({
          state: "fired",
          signal: "volume",
          alert: "Rows dropped by 40 %.",
          values_hidden: true,
        }),
      ],
    });

    expect(lines).toEqual([
      "The number of rows written is outside its usual band. Its values are hidden for you.",
    ]);
  });

  it("lists blocking signals, failed and errored checks, other signals and warned checks, in that order", () => {
    const lines = firedLines(
      health({
        checks: [
          checkResult({ name: "w", outcome: "warned" }),
          checkResult({ name: "e", outcome: "errored" }),
          checkResult({ name: "f", outcome: "failed" }),
          checkResult({ name: "p", outcome: "passed" }),
        ],
        signals: [
          signalResult({ signal: "schema", state: "fired", effect: "alert" }),
          signalResult({ signal: "freshness", state: "fired", effect: "block" }),
          signalResult({ signal: "volume", state: "ok", effect: "block" }),
        ],
      }),
      t,
    );

    expect(lines.map((line) => [line.kind, line.severity, line.sentence])).toEqual([
      ["signal", "failing", "The table was not written when expected."],
      ["check", "failing", "Check f failed."],
      ["check", "failing", "Check e could not run."],
      ["signal", "warning", "The table's columns changed."],
      ["check", "warning", "Check w warned."],
    ]);
  });
});

describe("judged", () => {
  it("is false without checks and signals", () => {
    expect(judged(health({ checks: [], signals: [] }))).toBe(false);
  });

  it("is false when every signal lacks the history or the metrics to judge", () => {
    expect(
      judged(
        health({
          checks: [],
          signals: [signalResult({ state: "no_history" }), signalResult({ state: "off" })],
        }),
      ),
    ).toBe(false);
  });

  it("is true with a check, a signal that was judged, or a check the reader may not see", () => {
    expect(judged(health({}))).toBe(true);
    expect(judged(health({ checks: [], signals: [signalResult({ state: "fired" })] }))).toBe(true);
    expect(judged(health({ checks: [], signals: [], hidden_checks: 1 }))).toBe(true);
  });
});

describe("observedWords", () => {
  /**
   * The words for one observed value, in English.
   *
   * @param signal - The signal's kind.
   * @param observed - What it observed.
   * @returns The words.
   */
  const words = (signal: string, observed: Record<string, unknown>): string =>
    observedWords(signal, observed, t, "en");

  it("says how long ago the last write was, in the plainest unit", () => {
    const at = "2026-11-14T11:57:50+00:00";
    const fresh = { last_commit_at: at, source: null, job: null };
    expect(words("freshness", { ...fresh, seconds: 3285 })).toBe("54 minutes since the last write");
    expect(words("freshness", { ...fresh, seconds: 60 })).toBe("1 minute since the last write");
    expect(words("freshness", { ...fresh, seconds: 90_000 })).toBe("25 hours since the last write");
    expect(words("freshness", { ...fresh, seconds: 400_000 })).toBe("4 days since the last write");
  });

  it("says how many rows were added and how many there are", () => {
    expect(words("volume", { added: 3, total: 3 })).toBe("3 rows added, 3 in total");
    expect(words("volume", { added: 1, total: 12_000 })).toBe("1 row added, 12,000 in total");
  });

  it("says how many values are empty", () => {
    expect(words("null_rate", { nulls: 0, values: 3, rate: 0 })).toBe("0 of 3 values empty");
    expect(words("null_rate", { nulls: 1, values: 1, rate: 1 })).toBe("1 of 1 value empty");
  });

  it("says a range from its lowest to its highest value, dates and times as such", () => {
    expect(words("range", { lower: -3.5, upper: 41.25, kind: "number" })).toBe(
      "from -3.5 to 41.25",
    );
    expect(words("range", { lower: 0, upper: 20_406, kind: "date" })).toBe(
      "from Jan 1, 1970 to Nov 14, 2025",
    );
    expect(words("range", { lower: 0, upper: 1_763_121_600_000_000, kind: "timestamp" })).toBe(
      "from Jan 1, 1970, 12:00 AM UTC to Nov 14, 2025, 12:00 PM UTC",
    );
  });

  it("says how many columns there are and what changed", () => {
    const columns = [
      ["a", "int"],
      ["b", "text"],
    ];
    expect(words("schema", { columns, added: [], dropped: [], retyped: [] })).toBe("2 columns");
    expect(
      words("schema", {
        columns,
        added: [["b", "text"]],
        dropped: ["c"],
        retyped: [["a", "x", "int"]],
      }),
    ).toBe("2 columns: 1 added, 1 dropped, 1 changed type");
  });

  it("falls back to the server's pairs without the absent ones, and never says null", () => {
    expect(words("freshness", { seconds: null, source: null })).toBe("None");
    expect(words("volume", { rows: 120, note: null })).toBe("rows 120");
    expect(words("range", { lower: 1, upper: null })).toBe("lower 1");
    expect(words("skew", { reason: "paused", job: null, list: ["a"] })).toBe(
      'reason paused, list ["a"]',
    );
  });
});

describe("bandWords", () => {
  /**
   * The words for one band, in English.
   *
   * @param signal - The signal's kind.
   * @param band - The usual band.
   * @param observed - What the signal observed, for a range's kind.
   * @returns The words.
   */
  const words = (
    signal: string,
    band: Record<string, unknown>,
    observed: Record<string, unknown> | null = null,
  ): string => bandWords(signal, band, observed, t, "en");

  it("says how long a write may take, like Observed", () => {
    expect(words("freshness", { high: 86_400 })).toBe("up to 24 hours");
    expect(words("freshness", { high: 600 })).toBe("up to 10 minutes");
    expect(words("freshness", { high: 432_000 })).toBe("up to 5 days");
  });

  it("says the usual rows added", () => {
    expect(words("volume", { low: 900, high: 1_100, median: 1_000 })).toBe(
      "900 to 1,100 rows added",
    );
    expect(words("volume", { low: 0, high: 1, median: 1 })).toBe("0 to 1 row added");
  });

  it("says the usual share of empty values", () => {
    expect(words("null_rate", { low: 0, high: 0.05, median: 0 })).toBe("up to 5 % empty");
    expect(words("null_rate", { low: 0.012, high: 0.2, median: 0.1 })).toBe("1.2 % to 20 % empty");
  });

  it("says a range's band in the column's own kind, dates as dates", () => {
    const observed = { lower: 0, upper: 1, kind: "timestamp" };
    expect(
      words(
        "range",
        { min: 0, max: 1, low: 1_762_041_600_000_000, high: 1_763_208_000_000_000 },
        observed,
      ),
    ).toBe("from Nov 2, 2025, 12:00 AM UTC to Nov 15, 2025, 12:00 PM UTC");
    expect(words("range", { low: -2.5, high: 40 }, { kind: "number" })).toBe("from -2.5 to 40");
    expect(words("range", { low: 1, high: 9 })).toBe("from 1 to 9");
  });

  it("falls back to the pairs without the absent ones", () => {
    expect(words("skew", { high: 3, low: null })).toBe("high 3");
    expect(words("volume", { high: 3 })).toBe("high 3");
    expect(words("freshness", { low: 1 })).toBe("low 1");
    expect(words("null_rate", { high: "x" })).toBe("high x");
  });
});

describe("effectWord", () => {
  it("has a word for every effect the server can send", () => {
    // The server's list, read from its source, so a new effect fails here before it shows raw.
    const source = readFileSync(
      resolve(
        import.meta.dirname,
        "../../../../packages/lakana-core/src/lakana_core/catalog/health_signals.py",
      ),
      "utf8",
    );
    const listed = /^EFFECTS = \(([^)]*)\)/m.exec(source)?.[1] ?? "";
    const effects = [...listed.matchAll(/"([^"]+)"/g)].map((match) => match[1] as string);
    expect(effects).toEqual(["warn", "block", "off"]);
    expect(effects.map((effect) => effectWord(effect, t))).toEqual([
      "Warns",
      "Blocks publication",
      "Off",
    ]);
  });

  it("has no word for a value the server never sends", () => {
    expect(effectWord("alert", t)).toBeNull();
  });
});
