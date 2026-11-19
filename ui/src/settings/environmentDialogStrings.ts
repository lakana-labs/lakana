/**
 * The strings of the dialogs of Settings › Environments, registered with i18next when the module
 * runs.
 *
 * @remarks
 * They load with the dialogs, when one is first opened.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./environmentDialogs.en.json";

i18next.addResourceBundle(
  "en",
  "translation",
  { settings: { environmentDialogs: strings } },
  true,
  false,
);
