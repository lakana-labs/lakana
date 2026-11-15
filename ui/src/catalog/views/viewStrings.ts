/**
 * The strings of the Catalog's views other than All, registered with i18next when the module
 * runs.
 *
 * @remarks
 * They load with the views' chunk, never with the first visit. Each module of the views that
 * writes one of them imports this module.
 * @packageDocumentation
 */
import i18next from "i18next";

import views from "./views.en.json";

i18next.addResourceBundle("en", "translation", { catalog: { views } }, true, false);
