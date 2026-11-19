/**
 * The strings of Settings › Configuration, registered with i18next when the module runs.
 *
 * @remarks
 * They load with Configuration, never with the first visit.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./configuration.en.json";

i18next.addResourceBundle(
  "en",
  "translation",
  { settings: { configuration: strings } },
  true,
  false,
);
