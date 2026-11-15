/**
 * The strings of the Catalog's place menu (New namespace, New external table, Delete namespace)
 * and the note after a namespace was created or deleted, registered with i18next when the module
 * runs.
 *
 * @remarks
 * They load with the menu's chunk, never with the first visit. Each module that writes one of
 * them imports this module.
 * @packageDocumentation
 */
import i18next from "i18next";

import places from "./places.en.json";

i18next.addResourceBundle("en", "translation", { catalog: { places } }, true, false);
