/**
 * The strings of Settings › Repositories' list and of a repository's credential, registered
 * with i18next when the module runs.
 *
 * @remarks
 * They load with Repositories, never with the first visit. Each module that writes one of them
 * imports this module; a repository's page adds its own (`repositoryPageStrings.ts`).
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./repositories.en.json";

i18next.addResourceBundle(
  "en",
  "translation",
  { settings: { repositoryPages: strings } },
  true,
  false,
);
