/**
 * The strings of an artifact's Versions tab (its versions, their files and Download), registered
 * with i18next when the module runs.
 *
 * @remarks
 * They load with the tab's chunk, never with the first visit or the table's page. Each module of
 * the tab that writes one of them imports this module, so they are in place before the tab
 * renders and no string shows untranslated.
 * @packageDocumentation
 */
import i18next from "i18next";

import artifact from "./artifact.en.json";

i18next.addResourceBundle("en", "translation", { table: { artifact } }, true, false);
