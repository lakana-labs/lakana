/**
 * The strings of an environment's page, registered with i18next when the module runs.
 *
 * @remarks
 * They load with the page, which loads on its own below Settings › Environments.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./environmentPage.en.json";

i18next.addResourceBundle(
  "en",
  "translation",
  { settings: { environmentPage: strings } },
  true,
  false,
);
