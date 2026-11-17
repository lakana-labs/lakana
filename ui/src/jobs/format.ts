/**
 * How the Jobs screen writes a duration.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";

import "./jobStrings";

/**
 * A duration in words: one decimal under 10 s ("1.2 s"), whole seconds under a minute, minutes
 * under an hour ("14 min"), else hours and minutes ("1 h 30 min").
 *
 * @param seconds - The duration.
 * @param t - The translation function.
 * @returns The text.
 */
export function formatDuration(seconds: number, t: TFunction): string {
  if (seconds < 10) {
    return t("jobs.duration.tenths", { n: (Math.round(seconds * 10) / 10).toFixed(1) });
  }
  if (seconds < 60) {
    return t("jobs.duration.seconds", { n: Math.round(seconds) });
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    return t("jobs.duration.minutes", { n: minutes });
  }
  return t("jobs.duration.hours", { h: Math.floor(minutes / 60), m: minutes % 60 });
}
