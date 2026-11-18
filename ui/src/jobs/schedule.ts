/**
 * A cron expression in words, for the five shapes the how-to uses.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";

import "./jobStrings";

/**
 * One cron field that is a plain number within a range.
 *
 * @param field - The field.
 * @param min - The smallest value.
 * @param max - The largest value.
 * @returns The number; null for anything else.
 */
function number(field: string | undefined, min: number, max: number): number | null {
  if (field === undefined || !/^\d{1,2}$/.test(field)) {
    return null;
  }
  const value = Number(field);
  return value >= min && value <= max ? value : null;
}

const two = (n: number): string => String(n).padStart(2, "0");

/**
 * Words a schedule: every minute, hourly at :MM, daily HH:MM, weekly on a weekday HH:MM, or
 * monthly on day D HH:MM, always followed by the zone. Any other cron is returned as its text,
 * which is never wrong, and marked `verbatim` so the caller shows it in monospace.
 *
 * @param cron - The five-field cron expression.
 * @param timezone - The schedule's zone.
 * @param t - The translation function.
 * @returns The text, and whether it is the cron as it is.
 */
export function describeSchedule(
  cron: string,
  timezone: string,
  t: TFunction,
): { text: string; verbatim: boolean } {
  const fields = cron.trim().split(/\s+/);
  const [minute, hour, day, month, weekday] = fields;
  const verbatim = {
    text: t("jobs.schedule.inZone", { what: cron, zone: timezone }),
    verbatim: true,
  };
  if (fields.length !== 5 || month !== "*") {
    return verbatim;
  }
  const m = number(minute, 0, 59);
  const h = number(hour, 0, 23);
  const words = (what: string): { text: string; verbatim: boolean } => ({
    text: t("jobs.schedule.inZone", { what, zone: timezone }),
    verbatim: false,
  });
  if (m === null) {
    return day === "*" && weekday === "*" && minute === "*" && hour === "*"
      ? words(t("jobs.schedule.everyMinute"))
      : verbatim;
  }
  if (hour === "*" && day === "*" && weekday === "*") {
    return words(t("jobs.schedule.hourly", { minute: two(m) }));
  }
  if (h === null) {
    return verbatim;
  }
  const time = `${two(h)}:${two(m)}`;
  const dayOfMonth = number(day, 1, 31);
  const dayOfWeek = number(weekday, 0, 6);
  if (day === "*" && weekday === "*") {
    return words(t("jobs.schedule.daily", { time }));
  }
  if (day === "*" && dayOfWeek !== null) {
    return words(
      t("jobs.schedule.weekly", { day: t(`jobs.schedule.weekdays.${String(dayOfWeek)}`), time }),
    );
  }
  if (weekday === "*" && dayOfMonth !== null) {
    return words(t("jobs.schedule.monthly", { day: dayOfMonth, time }));
  }
  return verbatim;
}
