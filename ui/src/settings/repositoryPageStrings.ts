/**
 * The strings of a repository's page, registered with i18next when the module
 * runs.
 *
 * @remarks
 * They load with the page, never with the list, and the dialogs bring theirs
 * (`repositoryDialogStrings.ts`); the list's strings (`repositoryStrings.ts`),
 * which the page uses too, come with them.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./repositoryPage.en.json";
import "./repositoryStrings";

i18next.addResourceBundle(
  "en",
  "translation",
  { settings: { repositoryPages: strings } },
  true,
  false,
);
