/**
 * Numbers in messages, written for the language of the message.
 *
 * @packageDocumentation
 */
import type { FormatterModule } from "i18next";

/** The formatters made so far, by language and format, since a page may write thousands. */
const made = new Map<string, Intl.NumberFormat>();

/**
 * The options of a format such as `number(minimumFractionDigits: 1; maximumFractionDigits: 1)`.
 *
 * @param options - The text between the parentheses, if any.
 * @returns The options for `Intl.NumberFormat`.
 */
function parseOptions(options: string | undefined): Intl.NumberFormatOptions {
  const parsed: Record<string, string | number> = {};
  for (const option of (options ?? "").split(";")) {
    const [name = "", value = ""] = option.split(":").map((part) => part.trim());
    if (name !== "") parsed[name] = /^\d+$/.test(value) ? Number(value) : value;
  }
  return parsed;
}

/**
 * The formatter for a language and a format, made once.
 *
 * @param language - The language of the message.
 * @param format - The format: `number` or `number(…)`, or none.
 * @returns The formatter.
 * @throws {Error}
 * For a format other than `number`: the catalogues use no other (`i18n.test.ts` checks them).
 */
function numberFormat(language: string | undefined, format: string): Intl.NumberFormat {
  const key = `${language ?? ""}\u0000${format}`;
  let formatter = made.get(key);
  if (formatter === undefined) {
    const match = /^(\w+)\s*(?:\((.*)\))?$/s.exec(format);
    if (format !== "" && match?.[1] !== "number") {
      throw new Error(`No format "${format}" in messages: only "number" is known.`);
    }
    formatter = new Intl.NumberFormat(language, parseOptions(match?.[2]));
    made.set(key, formatter);
  }
  return formatter;
}

/**
 * The i18next formatter: every number interpolated into a message goes through
 * `Intl.NumberFormat` for the message's language, so 12400 reads "12,400" in English and "12.400"
 * in German; `{{n, number(…)}}` passes options such as the fraction digits.
 *
 * @remarks
 * With `interpolation.alwaysFormat`, i18next calls it for every value. A value that is not a
 * number is left as it is, so an identifier that is a number (a version, a line, a port) is
 * passed as a string to keep its digits as they are.
 */
export const localeNumbers: FormatterModule = {
  type: "formatter",
  init() {
    // Nothing to set up: the language comes with each value.
  },
  add() {
    throw new Error("Messages format numbers only.");
  },
  addCached() {
    throw new Error("Messages format numbers only.");
  },
  format(value: unknown, format: string | undefined, language: string | undefined): string {
    const spec = format?.trim() ?? "";
    // i18next takes `undefined` for a value not passed; anything else that is not a number it
    // turns into a string itself.
    if (spec === "" && typeof value !== "number") return value as string;
    return numberFormat(language, spec).format(value as number);
  },
};
