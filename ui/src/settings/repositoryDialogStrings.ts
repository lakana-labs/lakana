/**
 * The strings of a repository's dialogs, registered with i18next when the module runs.
 *
 * @remarks
 * They load with the dialogs, when one is first opened.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./repositoryDialogs.en.json";

i18next.addResourceBundle(
  "en",
  "translation",
  { settings: { repositoryPages: strings } },
  true,
  false,
);
