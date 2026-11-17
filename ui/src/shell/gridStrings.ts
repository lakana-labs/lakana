/**
 * The strings of the result grid, registered with i18next when the module runs.
 *
 * @remarks
 * They load with the chunk of whichever page shows a grid, never with the first visit. Each
 * module that writes one of them imports this module, so they are in place before the grid
 * renders and no string shows untranslated.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./grid.en.json";

i18next.addResourceBundle("en", "translation", { grid: strings }, true, false);
