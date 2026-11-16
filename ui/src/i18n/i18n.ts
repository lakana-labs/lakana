/**
 * Internationalisation: every string of the interface goes through i18next.
 *
 * @packageDocumentation
 */
import i18next, { type i18n } from "i18next";
import { initReactI18next } from "react-i18next";

import { markedText } from "../shell/VisibleText";
import en from "./en.json";

/**
 * Starts i18next with the English strings bundled in the build.
 *
 * @remarks
 * No backend and no language detector: the interface reads no network and guesses nothing.
 * Every interpolated value is "escaped" by {@link markedText}: a name the server sent shows a
 * hidden or format character as a mark such as `U+202E` in any sentence, label or title made with
 * `t()`, without a call at each site. It changes nothing else (numbers, dates, our own words), is
 * idempotent, and leaves markup alone, which React renders as text.
 * @returns The initialised i18next instance.
 */
export function initI18n(): i18n {
  void i18next.use(initReactI18next).init({
    resources: { en: { translation: en } },
    lng: "en",
    fallbackLng: "en",
    initAsync: false,
    interpolation: { escapeValue: true, escape: markedText },
  });
  return i18next;
}
