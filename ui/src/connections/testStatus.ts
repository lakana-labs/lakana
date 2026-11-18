/**
 * What a connection's last test says, as a glyph, words and a time.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";

import type { ConnectionOut } from "../api";
import { relativeTime } from "../catalog/format";
import type { StatusKind } from "../status/status";
import { OUTCOMES } from "./kinds";
import { NO_WORKER_AFTER_MS } from "./useOpenTests";
import "./connectionStrings";

/** The status of a connection's last test, ready to draw. */
export interface TestStatusView {
  /** Which case it is. */
  readonly kind: "never" | "queued" | "running" | "stale" | "ok" | "failed";
  /** The glyph that goes with the words; none for a test never run or no longer current. */
  readonly glyph: StatusKind | null;
  /** The whole line: the word, then " · " and the time when there is one. */
  readonly word: string;
  /** What the translation puts before the time; the whole line when there is no time. */
  readonly lead: string;
  /** What the translation puts after the time; empty when there is none. */
  readonly trail: string;
  /** The time the line ends with ("2 hours ago", or "just now"); null for an open test. */
  readonly when: string | null;
}

/** Stands where the time goes while a line is split around it; a private-use character. */
const TIME_MARK = "\uE000";

/**
 * Words a line that holds the time, and splits it around the time.
 *
 * @param t - The translation function.
 * @param key - The line's string.
 * @param when - The time's words.
 * @param extra - The line's other values.
 * @returns The line, what comes before the time and what comes after it.
 */
function timed(
  t: TFunction,
  key: string,
  when: string,
  extra: Record<string, string> = {},
): Pick<TestStatusView, "word" | "lead" | "trail"> {
  const [lead = "", trail = ""] = t(key, { ...extra, when: TIME_MARK }).split(TIME_MARK);
  return { word: t(key, { ...extra, when }), lead, trail };
}

/**
 * Words the last test of a connection.
 *
 * @remarks
 * A test that finished before the connection's last change says so and carries no glyph, so it
 * is never shown as a current ✓. An outcome this build does not know is "failed".
 * @param connection - The connection.
 * @param now - The present, in milliseconds since the epoch.
 * @param language - The interface language.
 * @param t - The translation function.
 * @param queuedSince - When the screen first saw the test queued, for "no worker yet".
 * @returns The status.
 */
export function testStatus(
  connection: ConnectionOut,
  now: number,
  language: string,
  t: TFunction,
  queuedSince?: number,
): TestStatusView {
  const last = connection.last_test;
  if (last === null) {
    return {
      kind: "never",
      glyph: null,
      word: t("connections.status.never"),
      lead: t("connections.status.never"),
      trail: "",
      when: null,
    };
  }
  if (last.state === "queued") {
    const waiting = queuedSince !== undefined && now - queuedSince >= NO_WORKER_AFTER_MS;
    const word = t(waiting ? "connections.status.queuedNoWorker" : "connections.status.queued");
    return { kind: "queued", glyph: "queued", word, lead: word, trail: "", when: null };
  }
  if (last.state === "running") {
    const word = t("connections.status.running");
    return { kind: "running", glyph: "running", word, lead: word, trail: "", when: null };
  }
  const finished = last.finished_at ?? connection.updated_at;
  const when = relativeTime(finished, now, language) ?? t("connections.status.justNow");
  if (Date.parse(finished) < Date.parse(connection.updated_at)) {
    return { kind: "stale", glyph: null, ...timed(t, "connections.status.stale", when), when };
  }
  if (last.outcome === "ok") {
    return { kind: "ok", glyph: "succeeded", ...timed(t, "connections.status.ok", when), when };
  }
  const known = last.outcome !== null && OUTCOMES.includes(last.outcome);
  const word = t(`connections.outcomes.${known ? last.outcome : "failed"}.word`);
  return {
    kind: "failed",
    glyph: "failed",
    ...timed(t, "connections.status.failed", when, { word }),
    when,
  };
}
