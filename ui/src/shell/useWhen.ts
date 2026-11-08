/**
 * Dates and times as the person's language writes them.
 *
 * @packageDocumentation
 */
import { useTranslation } from "react-i18next";

/**
 * A function that writes an ISO timestamp as a short date and time.
 *
 * @returns The function.
 */
export function useWhen(): (iso: string) => string {
  const { i18n } = useTranslation();
  const format = new Intl.DateTimeFormat(i18n.language, {
    dateStyle: "medium",
    timeStyle: "short",
  });
  return (iso) => format.format(new Date(iso));
}
