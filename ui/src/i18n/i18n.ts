/**
 * Internationalisation: every string of the interface goes through i18next.
 *
 * @packageDocumentation
 */
import i18next, { type i18n } from "i18next";
import { initReactI18next } from "react-i18next";

import en from "./en.json";

/**
 * Starts i18next with the English strings bundled in the build.
 *
 * @remarks
 * No backend and no language detector: the interface reads no network and guesses nothing.
 * @returns The initialised i18next instance.
 */
export function initI18n(): i18n {
  void i18next.use(initReactI18next).init({
    resources: { en: { translation: en } },
    lng: "en",
    fallbackLng: "en",
    initAsync: false,
    interpolation: { escapeValue: false },
  });
  return i18next;
}
