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
  return `${number} ${BYTE_UNITS[unit] ?? "B"}`;
}

const STEPS: readonly (readonly [Intl.RelativeTimeFormatUnit, number])[] = [
  ["year", 365 * 86_400_000],
  ["month", 30 * 86_400_000],
  ["day", 86_400_000],
  ["hour", 3_600_000],
  ["minute", 60_000],
];

/**
 * How long ago a time was, in the largest whole unit.
 *
 * @param iso - The time, ISO 8601.
 * @param now - The present, in milliseconds since the epoch.
 * @param language - The interface language.
 * @returns The text such as `2 hours ago`; null under a minute (the caller shows "just now"),
 *   for a time in the future and for one that cannot be read.
 */
export function relativeTime(iso: string, now: number, language: string): string | null {
  const elapsed = now - Date.parse(iso);
  if (!(elapsed >= 60_000)) {
    return null;
  }
  const format = new Intl.RelativeTimeFormat(language, { numeric: "always" });
  for (const [unit, size] of STEPS) {
    if (elapsed >= size) {
      return format.format(-Math.floor(elapsed / size), unit);
    }
  }
  return null;
}
