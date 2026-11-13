/**
 * What the Overview says about a table's health, in sentences. The server's own words and kinds
 * are used as they are; where it gives no sentence there is one neutral sentence per kind, which
 * claims no direction: a rise or a drop is never worked out here.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";

import type { TableHealthOut } from "../../api";

/** One line of the health block. */
export interface FiredLine {
  /** Whether a signal or a check produced it. */
  readonly kind: "signal" | "check";
  /** `failing` for what blocks or fails, `warning` for the rest. */
  readonly severity: "failing" | "warning";
  /** The sentence. */
  readonly sentence: string;
}

type Check = TableHealthOut["checks"][number];
type Signal = TableHealthOut["signals"][number];

const SIGNALS = ["freshness", "volume", "null_rate", "range", "schema"];

/**
 * The neutral sentence for a signal's kind.
 *
 * @param signal - The signal.
 * @param t - The translation function.
 * @returns The sentence.
 */
function neutral(signal: Signal, t: TFunction): string {
  return SIGNALS.includes(signal.signal)
    ? t(`table.healthBlock.signal.${signal.signal}`, { column: signal.column ?? "" })
    : t("table.healthBlock.signal.unknown", { signal: signal.signal });
}

/**
 * The sentence of a fired signal: the server's alert, unless its values are hidden from the
 * reader or it gave none.
 *
 * @param signal - The signal.
 * @param t - The translation function.
 * @returns The sentence.
 */
function signalSentence(signal: Signal, t: TFunction): string {
  if (signal.values_hidden) {
    return `${neutral(signal, t)} ${t("table.healthBlock.valuesHidden")}`;
  }
  return signal.alert ?? neutral(signal, t);
}

/**
 * The sentence of a check that did not pass: its name, how many rows broke it when that is
 * known, and for a failed one what happened to the table.
 *
 * @param check - The check's newest result.
 * @param t - The translation function.
 * @returns The sentence.
 */
function checkSentence(check: Check, t: TFunction): string {
  const words = { name: check.name, count: check.failing_rows ?? 0 };
  if (check.outcome === "errored") {
    return check.error === null
      ? t("table.healthBlock.erroredPlain", words)
      : t("table.healthBlock.errored", { ...words, error: check.error });
  }
  const verb = check.outcome === "failed" ? "failed" : "warned";
  const found =
    check.failing_rows === null
      ? t(`table.healthBlock.${verb}Uncounted`, words)
      : t(`table.healthBlock.${verb}`, words);
  const back =
    check.outcome === "failed" &&
    (check.rollback_outcome === "rolled_back" || check.rollback_outcome === "unpublished")
      ? t(`table.healthBlock.${check.rollback_outcome}`)
      : null;
  return back === null ? found : `${found} ${back}`;
}

/**
 * The lines that say what is wrong: each check that did not pass and each signal that fired,
 * blocking signals first, then failed and errored checks, the other signals, and warned checks.
 *
 * @param health - The health the server answered.
 * @param t - The translation function.
 * @returns The lines, worst first.
 */
export function firedLines(health: TableHealthOut, t: TFunction): readonly FiredLine[] {
  const fired = health.signals.filter((signal) => signal.state === "fired");
  const signalLine = (signal: Signal, severity: FiredLine["severity"]): FiredLine => ({
    kind: "signal",
    severity,
    sentence: signalSentence(signal, t),
  });
  const checkLine = (check: Check, severity: FiredLine["severity"]): FiredLine => ({
    kind: "check",
    severity,
    sentence: checkSentence(check, t),
  });
  const of = (outcome: string): Check[] =>
    health.checks.filter((check) => check.outcome === outcome);
  return [
    ...fired.filter((signal) => signal.effect === "block").map((s) => signalLine(s, "failing")),
    ...of("failed").map((check) => checkLine(check, "failing")),
    ...of("errored").map((check) => checkLine(check, "failing")),
    ...fired.filter((signal) => signal.effect !== "block").map((s) => signalLine(s, "warning")),
    ...of("warned").map((check) => checkLine(check, "warning")),
  ];
}

/**
 * Whether the table has been judged at all: it has a check, a check the reader may not see, or a
 * signal with a verdict.
 *
 * @param health - The health the server answered.
 * @returns False for a table with no checks and no signal that could judge it.
 */
export function judged(health: TableHealthOut): boolean {
  return (
    health.checks.length > 0 ||
    health.hidden_checks > 0 ||
    health.signals.some((signal) => signal.state === "ok" || signal.state === "fired")
  );
}
