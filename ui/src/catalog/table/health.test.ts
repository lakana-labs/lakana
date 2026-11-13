import i18next from "i18next";
import { describe, expect, it } from "vitest";

import { checkResult, health, signalResult } from "../../test/table";
import { firedLines, judged } from "./health";

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
