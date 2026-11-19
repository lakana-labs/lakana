/**
 * The strings of Settings › Environments' list and of an environment's storage and retention,
 * registered with i18next when the module runs.
 *
 * @remarks
 * They load with Environments, never with the first visit. Each module that writes one of them
 * imports this module; an environment's page and the dialogs add their own.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./environments.en.json";

i18next.addResourceBundle(
  "en",
  "translation",
  { settings: { environmentPages: strings } },
  true,
  false,
);
