/**
 * The strings of the actions on tables (undrop, and the dialogs of the table page), registered
 * with i18next when the module runs.
 *
 * @remarks
 * They load with the chunk of the first screen that needs them, never with the first visit.
 * Each module that writes one of them imports this module.
 * @packageDocumentation
 */
import i18next from "i18next";

import actions from "./actions.en.json";

i18next.addResourceBundle("en", "translation", { table: { actions } }, true, false);
