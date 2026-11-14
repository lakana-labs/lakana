/**
 * What the Overview says about a table's health, in sentences. The server's own words and kinds
 * are used as they are; where it gives no sentence there is one neutral sentence per kind, which
 * claims no direction: a rise or a drop is never worked out here.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";

import type { TableHealthOut } from "../../api";
import "./tableStrings";

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

/**
 * A value as a finite number, or null for anything else.
 *
 * @param value - The value.
 * @returns The number, or null.
 */
function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * A length of time in the plainest unit, rounded down like the server's sentences: minutes under
 * two hours, hours under two days, else days.
 *
 * @param seconds - The time.
 * @param t - The translation function.
 * @param number - The person's number format.
 * @returns The words, such as "54 minutes".
 */
function duration(seconds: number, t: TFunction, number: Intl.NumberFormat): string {
  const whole = Math.max(Math.floor(seconds), 0);
  const [count, unit] =
    whole < 7_200
      ? [Math.floor(whole / 60), "minutes"]
      : whole < 172_800
        ? [Math.floor(whole / 3_600), "hours"]
        : [Math.floor(whole / 86_400), "days"];
  return t(`table.healthTab.signals.duration.${unit}`, { count, shown: number.format(count) });
}

/**
 * A share from 0 to 1 as a percentage number, at most one decimal.
 *
 * @param share - The share.
 * @param language - The person's language.
 * @returns The number, without the sign.
 */
function percent(share: number, language: string): string {
  return new Intl.NumberFormat(language, { maximumFractionDigits: 1 }).format(share * 100);
}

/**
 * A range bound as the person reads it, in their language: a date for days since 1970, a time
 * in UTC for microseconds since 1970, else the number. Dates are written in words, which break
 * only between words.
 *
 * @param kind - The range's kind: `date`, `timestamp`, `integer` or `number`.
 * @param value - The bound.
 * @param language - The person's language.
 * @returns The text.
 */
function bound(kind: unknown, value: number, language: string): string {
  if (kind === "date") {
    return new Intl.DateTimeFormat(language, { dateStyle: "medium", timeZone: "UTC" }).format(
      value * 86_400_000,
    );
  }
  if (kind === "timestamp") {
    const time = new Intl.DateTimeFormat(language, {
      dateStyle: "medium",
      timeStyle: "short",
      timeZone: "UTC",
    }).format(Math.floor(value / 1_000));
    return `${time} UTC`;
  }
  return new Intl.NumberFormat(language).format(value);
}

/**
 * An observed value or a band as `key value` pairs, each value as the server sent it, leaving
 * out the absent ones: for a band, and a signal or shape the interface has no words for.
 *
 * @param values - The object.
 * @param t - The translation function.
 * @returns The pairs, or "None" when every value is absent.
 */
function valuePairs(values: Readonly<Record<string, unknown>>, t: TFunction): string {
  const shown = Object.entries(values)
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([key, value]) => `${key} ${typeof value === "string" ? value : JSON.stringify(value)}`);
  return shown.length === 0 ? t("table.healthTab.signals.nothing") : shown.join(", ");
}

/**
 * What a signal observed, in words: freshness as the time since the last write, volume as the
 * rows added and in total, empty values as how many of how many, a range from its lowest to its
 * highest value and the columns by how many there are and what changed. Any other signal or shape
 * reads as the server's pairs without the absent ones; "null" is never shown.
 *
 * @param signal - The signal's kind.
 * @param observed - What it observed.
 * @param t - The translation function.
 * @param language - The person's language, for numbers.
 * @returns The words.
 */
export function observedWords(
  signal: string,
  observed: Readonly<Record<string, unknown>>,
  t: TFunction,
  language: string,
): string {
  const number = new Intl.NumberFormat(language);
  const words = "table.healthTab.signals.observedWords";
  if (signal === "freshness") {
    const seconds = finite(observed["seconds"]);
    if (seconds !== null) {
      return t(`${words}.freshness`, { duration: duration(seconds, t, number) });
    }
  }
  if (signal === "volume") {
    const added = finite(observed["added"]);
    const total = finite(observed["total"]);
    if (added !== null && total !== null) {
      return t(`${words}.volume`, {
        count: added,
        added: number.format(added),
        total: number.format(total),
      });
    }
  }
  if (signal === "null_rate") {
    const nulls = finite(observed["nulls"]);
    const values = finite(observed["values"]);
    if (nulls !== null && values !== null) {
      return t(`${words}.nullRate`, {
        count: values,
        nulls: number.format(nulls),
        values: number.format(values),
      });
    }
  }
  if (signal === "range") {
    const lower = finite(observed["lower"]);
    const upper = finite(observed["upper"]);
    if (lower !== null && upper !== null) {
      const kind = observed["kind"];
      return t(`${words}.range`, {
        lower: bound(kind, lower, language),
        upper: bound(kind, upper, language),
      });
    }
  }
  if (signal === "schema" && Array.isArray(observed["columns"])) {
    const columns = t(`${words}.columns`, {
      count: observed["columns"].length,
      shown: number.format(observed["columns"].length),
    });
    const changes = (["added", "dropped", "retyped"] as const)
      .map((change) => {
        const list = observed[change];
        return Array.isArray(list) && list.length > 0
          ? t(`${words}.${change}`, { count: list.length, shown: number.format(list.length) })
          : null;
      })
      .filter((change) => change !== null);
    return changes.length === 0 ? columns : `${columns}: ${changes.join(", ")}`;
  }
  return valuePairs(observed, t);
}

/**
 * A signal's usual band in words, matching {@link observedWords}: freshness as the longest
 * time between writes, volume as the rows usually added, empty values as their usual share, and a
 * range from its lowest to its highest bound in the column's own kind (from what it observed).
 * Any other signal or shape reads as the server's pairs without the absent ones.
 *
 * @param signal - The signal's kind.
 * @param band - The usual band.
 * @param observed - What the signal observed, for a range's kind; null when unknown.
 * @param t - The translation function.
 * @param language - The person's language, for numbers and dates.
 * @returns The words.
 */
export function bandWords(
  signal: string,
  band: Readonly<Record<string, unknown>>,
  observed: Readonly<Record<string, unknown>> | null,
  t: TFunction,
  language: string,
): string {
  const number = new Intl.NumberFormat(language);
  const words = "table.healthTab.signals.bandWords";
  const low = finite(band["low"]);
  const high = finite(band["high"]);
  if (signal === "freshness" && high !== null) {
    return t(`${words}.freshness`, { duration: duration(high, t, number) });
  }
  if (low !== null && high !== null) {
    if (signal === "volume") {
      return t(`${words}.volume`, {
        count: high,
        low: number.format(low),
        high: number.format(high),
      });
    }
    if (signal === "null_rate") {
      return low === 0
        ? t(`${words}.nullRateUpTo`, { high: percent(high, language) })
        : t(`${words}.nullRate`, { low: percent(low, language), high: percent(high, language) });
    }
    if (signal === "range") {
      const kind = observed?.["kind"];
      return t("table.healthTab.signals.observedWords.range", {
        lower: bound(kind, low, language),
        upper: bound(kind, high, language),
      });
    }
  }
  return valuePairs(band, t);
}

/** The word for each effect the server sends (`EFFECTS` in lakana-core's health_signals.py). */
const EFFECT_WORDS: Readonly<Record<string, string>> = {
  warn: "table.healthTab.signals.effects.warn",
  block: "table.healthTab.signals.effects.block",
  off: "table.healthTab.signals.effects.off",
};

/**
 * The word for a signal's effect.
 *
 * @param effect - The effect the server sent.
 * @param t - The translation function.
 * @returns The word, or null for a value the server never sends.
 */
export function effectWord(effect: string, t: TFunction): string | null {
  const key = EFFECT_WORDS[effect];
  return key === undefined ? null : t(key);
}
