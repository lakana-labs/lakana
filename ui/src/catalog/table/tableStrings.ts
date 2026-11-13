/**
 * The strings of a table's page, registered with i18next when the module runs.
 *
 * @remarks
 * They load with the page's chunk, not with the first visit. Each module of the page that
 * writes one of them imports this module, so they are in place before the page renders and no
 * string shows untranslated.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./table.en.json";

i18next.addResourceBundle("en", "translation", { table: strings }, true, false);
