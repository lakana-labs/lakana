/**
 * Numbers, sizes and times as the catalog list writes them, in the interface's language.
 *
 * @packageDocumentation
 */

/**
 * A row count: grouped below a million, shortened from a million (`1.2M`).
 *
 * @param value - The rows.
 * @param language - The interface language.
 * @returns The text.
 */
export function formatRows(value: number, language: string): string {
  if (value >= 1_000_000) {
    return new Intl.NumberFormat(language, {
      notation: "compact",
      maximumFractionDigits: 1,
    }).format(value);
  }
  return new Intl.NumberFormat(language).format(value);
}

const BYTE_UNITS = ["B", "kB", "MB", "GB", "TB", "PB"] as const;

/**
 * A size in decimal units (`98.3 kB`).
 *
 * @param value - The bytes.
 * @param language - The interface language.
 * @returns The text.
 */
export function formatBytes(value: number, language: string): string {
  let scaled = value;
  let unit = 0;
  while (
    (unit === 0 ? scaled : Math.round(scaled * 10) / 10) >= 1000 &&
    unit < BYTE_UNITS.length - 1
  ) {
    scaled /= 1000;
    unit += 1;
  }
  const number = new Intl.NumberFormat(language, {
    maximumFractionDigits: unit === 0 ? 0 : 1,
  }).format(scaled);
  return `${number} ${BYTE_UNITS[unit] as string}`;
}

const STEPS: readonly (readonly [Intl.RelativeTimeFormatUnit, number])[] = [
  ["year", 365 * 86_400_000],
  ["month", 30 * 86_400_000],
  ["day", 86_400_000],
  ["hour", 3_600_000],
  ["minute", 60_000],
];

/**
 * How long ago a time was, in the largest whole unit; with `ahead`, also how long until a time to
 * come.
 *
 * @param iso - The time, ISO 8601.
 * @param now - The present, in milliseconds since the epoch.
 * @param language - The interface language.
 * @param ahead - Word a time to come, such as an expiry (`in 26 days`). Leave it false for a time
 *   something happened: one a little ahead of the browser's clock is then a clock difference, and
 *   gives null like a time within the last minute.
 * @returns The text such as `2 hours ago` or `in 26 days`, in the person's language; null within
 *   a minute of now (the caller shows "just now"), for a time to come unless `ahead`, and for a
 *   time that cannot be read.
 */
export function relativeTime(
  iso: string,
  now: number,
  language: string,
  ahead = false,
): string | null {
  const elapsed = now - Date.parse(iso);
  // The smallest step is a minute, so a time within a minute of now, a time to come that was not
  // asked for, or one that is unreadable (NaN) matches none.
  const distance = ahead ? Math.abs(elapsed) : elapsed;
  const step = STEPS.find(([, size]) => distance >= size);
  if (step === undefined) {
    return null;
  }
  const whole = Math.floor(distance / step[1]);
  return new Intl.RelativeTimeFormat(language, { numeric: "always" }).format(
    elapsed > 0 ? -whole : whole,
    step[0],
  );
}
